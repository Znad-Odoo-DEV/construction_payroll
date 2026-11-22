# -*- coding: utf-8 -*-
import calendar
from datetime import date, timedelta

from odoo import api, fields, models
from odoo.exceptions import UserError

from .utils import MONTH_ABBR, ongoing_project_stage_id


class ConstructionStaffAttendance(models.Model):
    _name = 'construction.staff.attendance'
    _description = "Staff Attendance (Monthly)"
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'year desc, month desc, id desc'

    name = fields.Char(string="Reference", compute='_compute_name', store=True, readonly=False)
    company_id = fields.Many2one(
        'res.company', string="Company", required=True, tracking=True,
        default=lambda self: self.env.company,
    )
    month = fields.Selection(
        [(str(i), calendar.month_name[i]) for i in range(1, 13)],
        string="Month", required=True, tracking=True,
    )
    year = fields.Integer(string="Year", required=True, tracking=True,
                          default=lambda self: fields.Date.context_today(self).year)
    state = fields.Selection(
        [('created', 'Created'), ('generated', 'Generated'),
         ('submitted', 'Submitted'), ('approved', 'Approved')],
        string="Status", default='created', tracking=True, index=True,
    )
    # Periods the daily attendance was pulled for (set by the Generate wizard).
    # Current period (base) + previous-month period (whose absences are settled
    # as a deduction attachment).
    start_date = fields.Date(string="From Date", readonly=True)
    last_date = fields.Date(string="To Date", readonly=True)
    last_month_start = fields.Date(string="Last Month From", readonly=True)
    last_month_end = fields.Date(string="Last Month To", readonly=True)
    line_ids = fields.One2many('construction.staff.attendance.line', 'staff_attendance_id', string="Employees")
    only_employee_ids = fields.Many2many(
        'hr.employee', 'construction_staff_attendance_only_employee_rel',
        'staff_attendance_id', 'employee_id', string="Only These Employees",
        help="Empty on a monthly sheet, which lists the company's office staff. "
             "Filled by Generate One Payslip: the sheet then lists these "
             "employees and nobody else, and its payslip carries the sheet's "
             "own period.")

    @api.depends('company_id', 'month', 'year')
    def _compute_name(self):
        for rec in self:
            code = rec.company_id.construction_company_code or (rec.company_id.name or '')[:3].upper()
            month_name = calendar.month_name[int(rec.month)] if rec.month else ''
            rec.name = "%s Staff Attendance - %s - %s" % (code, month_name, rec.year or '')

    def _days_in_month(self):
        self.ensure_one()
        return calendar.monthrange(self.year, int(self.month))[1]

    def _get_ongoing_projects(self):
        """On-going construction projects, used when a staff member has no
        explicit project distribution.

        The stage that counts as on-going can be set with the system parameter
        ``construction_payroll.ongoing_project_stage_id``. Without it, every
        active project of the company that sits in an open (not folded) stage
        counts.
        """
        Project = self.env['project.project']
        stage_id = ongoing_project_stage_id(self.env)
        domain = [('active', '=', True), ('company_id', 'in', [False, self.company_id.id])]
        if stage_id:
            domain.append(('stage_id', '=', stage_id))
        else:
            domain.append(('stage_id.fold', '=', False))
        return Project.search(domain)

    def _prev_month(self):
        """(month, year) of the month before the sheet's month."""
        self.ensure_one()
        m, y = int(self.month), self.year
        return (12, y - 1) if m == 1 else (m - 1, y)

    def _period_start(self):
        self.ensure_one()
        return self.start_date or date(self.year, int(self.month), 1)

    def _period_end(self):
        self.ensure_one()
        # Default: the first 20 days of the month; editable in the wizard.
        return self.last_date or date(self.year, int(self.month),
                                      min(20, self._days_in_month()))

    def _last_month_start(self):
        self.ensure_one()
        pm, py = self._prev_month()
        return self.last_month_start or date(py, pm, 21)

    def _last_month_end(self):
        self.ensure_one()
        pm, py = self._prev_month()
        return self.last_month_end or date(py, pm, calendar.monthrange(py, pm)[1])

    def action_generate(self):
        """Open the period wizard; the actual generation runs from there so the
        user can choose which ranges the daily attendance is pulled for."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': self.env._("Select Attendance Period"),
            'res_model': 'construction.staff.attendance.generate.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_staff_attendance_id': self.id,
                'default_start_date': self._period_start(),
                'default_last_date': self._period_end(),
                'default_last_month_start': self._last_month_start(),
                'default_last_month_end': self._last_month_end(),
            },
        }

    def action_submit(self):
        for rec in self:
            pending = rec.line_ids.filtered(lambda l: l.advance_state == 'pending')
            if pending:
                raise UserError(self.env._(
                    "Approve or reject the advance for every employee before "
                    "submitting:\n\n%s",
                    "\n".join("- %s" % l.employee_id.display_name for l in pending)))
        self.write({'state': 'submitted'})

    def action_approve(self):
        self.write({'state': 'approved'})

    def action_reset(self):
        self.write({'state': 'created'})

    def _get_staff_employees(self):
        """Office/engineer staff of the company: the employees flagged
        construction_is_office_staff."""
        self.ensure_one()
        Emp = self.env['hr.employee'].sudo()
        if self.only_employee_ids:
            # A sheet made for named people lists them and nobody else.
            return self.only_employee_ids.sudo().filtered(
                lambda e: e.construction_is_office_staff and e.company_id == self.company_id)
        return Emp.search([('construction_is_office_staff', '=', True),
                           ('company_id', '=', self.company_id.id)])

    def _generate_lines(self, start_date=None, last_date=None,
                        last_month_start=None, last_month_end=None):
        self.ensure_one()
        # A one-employee sheet settles a previous period only when it was
        # given one. The monthly default (the 21st to the end of last month)
        # would charge a leaver's separate payslip for days that another
        # payslip already settled.
        settle_last = not self.only_employee_ids or bool(
            last_month_start and last_month_end)
        start_date = start_date or self._period_start()
        last_date = last_date or self._period_end()
        last_month_start = last_month_start or self._last_month_start()
        last_month_end = last_month_end or self._last_month_end()
        self.line_ids.unlink()
        employees = self._get_staff_employees()
        ongoing = self._get_ongoing_projects()
        # Days remaining after the period to complete the salary month, paid in
        # advance once approved. Dynamic: 20th of a 31-day month advances 11.
        advance = max(self._days_in_month() - last_date.day, 0)
        # What last month's payslip did about the days being settled - one
        # search for the roster. See construction.payslip._construction_advance_paid_map.
        advance_paid = {}
        if settle_last:
            pm, py = self._prev_month()
            advance_paid = self.env['construction.payslip']._construction_advance_paid_map(
                employees, MONTH_ABBR[pm], py, last_month_start)

        vals_list = []
        for emp in employees:
            day_cmds, attended, absence = self._pull_attendance(emp, start_date, last_date)
            # Last month's days: an absence on them is deducted where the
            # advance paid them, the days worked are added where it did not.
            lm_day_cmds, last_attended, last_absence = self._pull_attendance(
                emp, last_month_start, last_month_end) if settle_last else ([], 0, 0)
            last_state = (advance_paid.get(emp.id, ('unknown', 0))[0]
                          if settle_last else False)
            distribution = self._build_distribution(emp, ongoing)
            vals_list.append((0, 0, {
                'employee_id': emp.id,
                # The month's salary, photographed from the contract at
                # generation - a raise in March must not rewrite February's
                # sheet, and the figures stay editable for a one-off month.
                'basic_salary': emp.wage or 0.0,
                'house_allowance': emp.l10n_ae_housing_allowance or 0.0,
                'transport_allowance': emp.l10n_ae_transportation_allowance or 0.0,
                'other_allowance': emp.l10n_ae_other_allowances or 0.0,
                'last_month_absence': last_absence,
                'last_month_advance': last_state,
                'last_month_worked': last_attended,
                'attended_days': attended,
                'advance_days': advance,
                'total_absence_days': absence,
                'advance_state': 'pending',
                'distribution_ids': distribution,
                'day_ids': lm_day_cmds + day_cmds,
            }))
        self.line_ids = vals_list

    def _pull_attendance(self, employee, start_date, last_date):
        """Snapshot the employee's daily biometric attendance from the
        construction_attendance module for the period. Returns (day commands, attended
        days, absence days). Attended/absence exclude off days (Friday); those
        are shown in the table but counted as neither.

        Read dynamically so this module keeps no hard dependency on
        construction_attendance.
        """
        self.ensure_one()
        Line = self.env.get('construction.attendance.line')
        if Line is None:
            return [], 0, 0

        # Match the biometric line to the employee. The line names the
        # hr.employee outright now, on employee_id, and hr_employee_id is the
        # same record resolved from the badge for the lines that lost it.
        match = ['|', ('employee_id', '=', employee.id),
                 ('hr_employee_id', '=', employee.id)]
        # Read a little past the period so a Friday at the edge can still see
        # its Thursday / Saturday / Sunday neighbours.
        lines = Line.search(
            [('date', '>=', start_date - timedelta(days=2)),
             ('date', '<=', last_date + timedelta(days=2))] + match,
            order='date')

        # Per-day status, used to test the Friday bridge below.
        by_date = {}
        for l in lines:
            if not l.date:
                continue
            if l.attendance_type == 'Off Day':
                by_date[l.date] = 'off'
            elif l.absent:
                by_date[l.date] = 'absent'
            else:
                by_date[l.date] = 'present'

        def _present(d):
            return by_date.get(d) == 'present'

        # Staff are monthly-salaried, so only an absence on a counted day
        # reduces the paid attendance. The weekly off day (Friday) is paid ONLY
        # when it is bridged: Thursday (the day before) attended AND either
        # Saturday or Sunday attended; otherwise the Friday counts as absence.
        day_cmds, absence = [], 0
        for l in lines:
            if not l.date or not (start_date <= l.date <= last_date):
                continue  # neighbour-only line, outside the period
            off = l.attendance_type == 'Off Day'
            if off:
                bridged = _present(l.date - timedelta(days=1)) and (
                    _present(l.date + timedelta(days=1))
                    or _present(l.date + timedelta(days=2)))
                status = 'Off Day' if bridged else 'Absent'
                if not bridged:
                    absence += 1
            else:
                status = 'Absent' if l.absent else 'Present'
                if l.absent:
                    absence += 1
            day_cmds.append((0, 0, {
                'date': l.date,
                'day_name': l.date.strftime('%A'),
                'status': status,
                'check_in': l.first_punch,
                'check_out': l.last_punch,
                'project_id': l.project_id.id or False,
                'is_off_day': off,
            }))
        period_days = (last_date - start_date).days + 1
        attended = max(period_days - absence, 0)
        return day_cmds, attended, absence

    def _build_distribution(self, employee, ongoing_projects):
        """Explicit staff distribution if present, otherwise an equal split
        over all on-going construction projects."""
        commands = []
        if employee.construction_staff_project_ids:
            for sp in employee.construction_staff_project_ids:
                commands.append((0, 0, {
                    'project_id': sp.project_id.id,
                    'percentage': sp.percentage,
                }))
        elif ongoing_projects:
            share = round(100.0 / len(ongoing_projects), 2)
            for project in ongoing_projects:
                commands.append((0, 0, {
                    'project_id': project.id,
                    'percentage': share,
                }))
        return commands


class ConstructionStaffAttendanceLine(models.Model):
    _name = 'construction.staff.attendance.line'
    _description = "Staff Attendance Line"
    _order = 'staff_attendance_id, employee_id'

    staff_attendance_id = fields.Many2one(
        'construction.staff.attendance', string="Staff Attendance",
        required=True, ondelete='cascade', index=True,
    )
    employee_id = fields.Many2one('hr.employee', string="Employee", required=True, index=True)
    # groups=False on both: a related field inherits the target's groups, and
    # registration_number is behind hr.group_hr_user. These copies are on the
    # staff sheet, which the staff attendance groups read as they always did.
    employee_code = fields.Char(related='employee_id.registration_number', string="Employee ID", store=True, groups=False)
    job_position = fields.Char(related='employee_id.job_title', string="Position", store=True, groups=False)
    company_id = fields.Many2one(related='staff_attendance_id.company_id', store=True)
    currency_id = fields.Many2one(related='employee_id.currency_id', store=True)

    # Salary snapshot, copied from the contract when the line is made
    # (_generate_lines) and editable for a per-month override. Stored on the
    # line, not read through the employee: the month is priced on what the
    # contract said then, and a later raise leaves an approved sheet alone.
    basic_salary = fields.Monetary(string="Basic Salary")
    house_allowance = fields.Monetary(string="House Allowance")
    transport_allowance = fields.Monetary(string="Travelling Allowance")
    other_allowance = fields.Monetary(string="Other Allowance")
    gross_salary = fields.Monetary(
        string="Total / Gross Salary", compute='_compute_gross_salary', store=True,
        help="Basic + House + Travelling + Other allowances.",
    )

    # Days present inside the selected period, and the days that complete the
    # month (paid in advance once approved).
    attended_days = fields.Integer(string="Attended Days (Period)")
    advance_days = fields.Integer(string="Advance Days")
    total_absence_days = fields.Integer(string="Total Absence Days")
    # Absences during the previous-month period, deducted as an attachment -
    # where last month's payslip paid the advance. Where it did not, the days
    # worked are added instead; where its period reached into these days,
    # nothing is settled.
    last_month_absence = fields.Integer(string="Absence (Last Month)")
    last_month_advance = fields.Selection(
        [
            ('granted', "Paid last month"),
            ('not_granted', "Not paid last month"),
            ('covered', "Paid as attendance last month"),
            ('unknown', "No payslip found for last month"),
        ],
        string="Last Month's Advance", readonly=True,
        help="Read from last month's payslip. Paid: the absence on those days "
             "is deducted here. Not paid: nothing is deducted, and the days "
             "attended then are added here instead. Paid as attendance: last "
             "month's period reached into these days, nothing to settle. No "
             "payslip found: settled as if the advance had been paid.",
    )
    last_month_worked = fields.Integer(
        string="Worked Days (Last Month)",
        help="Days attended in the last-month window. Paid here when no "
             "advance paid them last month.")
    advance_state = fields.Selection(
        [('pending', 'Pending'), ('approved', 'Approved'), ('rejected', 'Rejected')],
        string="Advance Decision", default='pending', copy=False,
    )
    advance_granted = fields.Boolean(
        string="Advance Granted", compute='_compute_advance_granted', store=True)
    total_attended_days = fields.Integer(
        string="Total Attended Days", compute='_compute_total_attended', store=True)

    @api.depends('basic_salary', 'house_allowance',
                 'transport_allowance', 'other_allowance')
    def _compute_gross_salary(self):
        for line in self:
            line.gross_salary = (
                (line.basic_salary or 0.0) + (line.house_allowance or 0.0)
                + (line.transport_allowance or 0.0) + (line.other_allowance or 0.0)
            )

    # The same confidentiality hr.employee keeps: the HR Admin group may
    # see the staff sheet but not what office staff are paid, and these five
    # are copies of exactly that.
    _SALARY_FIELDS = (
        'basic_salary', 'house_allowance', 'transport_allowance',
        'other_allowance', 'gross_salary',
    )

    def read(self, fields=None, load='_classic_read'):
        records = super().read(fields=fields, load=load)
        if not self.env['hr.employee']._salary_restricted_user():
            return records
        # Every staff line is an office employee, so the whole result is
        # stripped.
        for row in records:
            for fname in self._SALARY_FIELDS:
                if fname in row:
                    row[fname] = 0.0
        return records

    distribution_ids = fields.One2many(
        'construction.staff.attendance.distribution', 'line_id', string="Projects Distribution",
    )
    day_ids = fields.One2many(
        'construction.staff.attendance.day', 'line_id', string="Daily Attendance",
    )

    @api.depends('advance_state')
    def _compute_advance_granted(self):
        for rec in self:
            rec.advance_granted = rec.advance_state == 'approved'

    @api.depends('attended_days', 'advance_days', 'advance_granted')
    def _compute_total_attended(self):
        for rec in self:
            rec.total_attended_days = rec.attended_days + (
                rec.advance_days if rec.advance_granted else 0)

    def action_approve_advance(self):
        self.write({'advance_state': 'approved'})
        return True

    def action_reject_advance(self):
        self.write({'advance_state': 'rejected'})
        return True

    def _sync_last_month_absence_deduction(self):
        """Keep the last-month attachment in step with the line.

        Where last month's payslip paid the advance, it is a deduction of
        ``last_month_absence`` days; where it paid none, an addition of
        ``last_month_worked`` days; where its period reached into these days,
        nothing. Value = days x (gross / days-in-previous-month), so June (30)
        divides by 30 and July (31) by 31.

        The attachment is created once a payslip exists and updated in place
        afterwards - fixing a day count, or the direction, never needs a
        delete/recreate. A paid one is never touched."""
        self.ensure_one()
        Att = self.env['construction.attachment']
        att = Att.search([('staff_line_id', '=', self.id)], limit=1)
        if att and att.state == 'paid':
            return
        sa = self.staff_attendance_id
        pm, py = sa._prev_month()
        prev_days = calendar.monthrange(py, pm)[1]
        if self.last_month_advance == 'not_granted':
            days = self.last_month_worked or 0
            kind = self.env.ref('construction_payroll.attachment_type_salary_addition')
            name = self.env._("Addition for last month - %s day(s) worked, no advance was paid", days)
        elif self.last_month_advance == 'covered':
            days = 0
            kind = self.env.ref('construction_payroll.attachment_type_salary_deduction')
            name = self.env._("Last month was paid in full by its own payslip - nothing to settle")
        else:
            days = self.last_month_absence or 0
            kind = self.env.ref('construction_payroll.attachment_type_salary_deduction')
            name = self.env._("Deduction for last month - %s absence day(s)", days)
        value = days * (self.gross_salary / prev_days) if (days and prev_days) else 0.0
        payslip = self.env['construction.payslip'].search([
            ('staff_attendance_id', '=', sa.id),
            ('employee_id', '=', self.employee_id.id),
        ], limit=1)
        if payslip:
            # A payslip generated and deleted leaves its sheet line behind, and
            # that line's row with it: freed, so the next payslip of the month
            # collects it beside its own and the same absence is deducted once
            # per attempt. Those leftovers belong to no payslip of their own.
            Att.search([
                ('employee_id', '=', self.employee_id.id),
                ('month', '=', payslip.month), ('year', '=', payslip.year),
                ('staff_line_id', '!=', False), ('staff_line_id', '!=', self.id),
                ('state', '!=', 'paid'),
                '|', ('payslip_id', '=', False), ('payslip_id', '=', payslip.id),
            ]).unlink()
        if att:
            att.write({'value': value, 'name': name, 'type_id': kind.id})
            return
        if days <= 0:
            return
        if not payslip:
            return
        Att.create({
            'name': name,
            'employee_id': self.employee_id.id,
            'type_id': kind.id,
            'month': payslip.month,
            'year': payslip.year,
            'value': value,
            'state': 'attached',
            'payslip_id': payslip.id,
            'staff_line_id': self.id,
        })

    def write(self, vals):
        res = super().write(vals)
        if any(name in vals for name in
               ('last_month_absence', 'last_month_worked', 'last_month_advance')):
            for line in self:
                line._sync_last_month_absence_deduction()
        return res


class ConstructionStaffAttendanceDistribution(models.Model):
    _name = 'construction.staff.attendance.distribution'
    _description = "Staff Attendance Project Distribution"
    _order = 'line_id, id'

    line_id = fields.Many2one(
        'construction.staff.attendance.line', string="Staff Line",
        required=True, ondelete='cascade', index=True,
    )
    project_id = fields.Many2one('project.project', string="Project", required=True)
    percentage = fields.Float(string="Percentage")


class ConstructionStaffAttendanceDay(models.Model):
    _name = 'construction.staff.attendance.day'
    _description = "Staff Daily Attendance"
    _order = 'line_id, date'

    line_id = fields.Many2one(
        'construction.staff.attendance.line', string="Staff Line",
        required=True, ondelete='cascade', index=True,
    )
    date = fields.Date(string="Date")
    day_name = fields.Char(string="Day")
    status = fields.Char(string="Status")
    check_in = fields.Datetime(string="Check In")
    check_out = fields.Datetime(string="Check Out")
    project_id = fields.Many2one('project.project', string="Project")
    is_off_day = fields.Boolean(string="Off Day")


class ConstructionStaffAttendanceGenerateWizard(models.TransientModel):
    _name = 'construction.staff.attendance.generate.wizard'
    _description = "Generate Staff Attendance"

    staff_attendance_id = fields.Many2one(
        'construction.staff.attendance', string="Staff Attendance", required=True, ondelete='cascade')
    start_date = fields.Date(string="From Date", required=True)
    last_date = fields.Date(string="To Date", required=True)
    # Not DB-required: the column is added to an existing transient table that
    # may already hold rows, so a NOT NULL constraint would fail to apply on
    # upgrade. The view marks them required and action_generate validates them.
    last_month_start = fields.Date(string="Last Month From")
    last_month_end = fields.Date(string="Last Month To")

    def action_generate(self):
        self.ensure_one()
        if not (self.last_month_start and self.last_month_end):
            raise UserError(self.env._("Set both Last Month dates."))
        if self.start_date > self.last_date:
            raise UserError(self.env._("From Date must be before To Date."))
        if self.last_month_start > self.last_month_end:
            raise UserError(self.env._("Last Month From must be before Last Month To."))
        sheet = self.staff_attendance_id
        sheet._generate_lines(self.start_date, self.last_date,
                              self.last_month_start, self.last_month_end)
        sheet.write({
            'start_date': self.start_date,
            'last_date': self.last_date,
            'last_month_start': self.last_month_start,
            'last_month_end': self.last_month_end,
            'state': 'generated',
        })
        return {'type': 'ir.actions.act_window_close'}
