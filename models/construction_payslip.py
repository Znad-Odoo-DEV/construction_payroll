# -*- coding: utf-8 -*-
import logging

from odoo import api, fields, models
from odoo.exceptions import UserError

from .construction_project_chart import render_project_chart

_logger = logging.getLogger(__name__)


class ConstructionPayslip(models.Model):
    _name = 'construction.payslip'
    _description = "Payslip"
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'to_date desc, id desc'

    name = fields.Char(string="Description", compute='_compute_name', store=True, readonly=False)
    employee_id = fields.Many2one(
        'hr.employee', string="Employee", required=True, index=True, tracking=True,
    )
    company_id = fields.Many2one(related='employee_id.company_id', store=True, index=True)
    currency_id = fields.Many2one(related='employee_id.currency_id', store=True)
    # groups=False: see construction.advance.employee_code.
    employee_code = fields.Char(related='employee_id.registration_number', string="Employee ID", store=True, groups=False)
    # Photo taken live from hr.employee rather than copied, so a changed picture
    # shows on past payslips too and no image is duplicated per slip.
    employee_image = fields.Image(
        related='employee_id.image_1920', string="Photo", readonly=True)
    # Small version, used as the form's preview so the read does not carry the
    # full-size image.
    employee_avatar = fields.Image(
        related='employee_id.avatar_128', string="Photo (small)", readonly=True)

    # Source documents
    attendance_sheet_id = fields.Many2one('construction.attendance.sheet', string="Attendance Sheet")
    staff_attendance_id = fields.Many2one('construction.staff.attendance', string="Staff Attendance")
    summary_id = fields.Many2one('construction.attendance.summary', string="Attendance Summary")
    batch_id = fields.Many2one('construction.salary.batch', string="Batch", index=True, tracking=True)

    # Period
    from_date = fields.Date(string="From Date")
    to_date = fields.Date(string="To Date")
    days = fields.Integer(string="Days in Month")
    month = fields.Selection(
        [
            ('JAN', 'JAN'), ('FEB', 'FEB'), ('MAR', 'MAR'), ('APR', 'APR'),
            ('MAY', 'MAY'), ('JUN', 'JUN'), ('JUL', 'JUL'), ('AUG', 'AUG'),
            ('SEP', 'SEP'), ('OCT', 'OCT'), ('NOV', 'NOV'), ('DEC', 'DEC'),
        ],
        string="Month",
    )
    year = fields.Char(string="Year")

    # Routing
    is_cash = fields.Boolean(string="Cash", help="Paid in cash: the employee had no WPS code when the payslip was made.")
    is_staff = fields.Boolean(string="Staff")

    # Contract snapshot (frozen at generation so later master changes do not
    # retroactively alter historical payslips).
    designation = fields.Char(string="Designation")
    basic_salary = fields.Monetary(string="Basic Salary", tracking=True)
    house_allowance = fields.Monetary(string="House Allowance", tracking=True)
    transport_allowance = fields.Monetary(string="Travelling Allowance", tracking=True)
    other_allowance = fields.Monetary(string="Other Allowances", tracking=True)
    # Computed from the four parts, so editing Basic or House on the payslip
    # moves the day rate, the salary and the net with it.
    #
    # readonly=False keeps the override: a gross typed by hand stands until one
    # of the four parts moves under it.
    gross_salary = fields.Monetary(
        string="Total Gross Salary", tracking=True,
        compute='_compute_gross_salary', store=True, readonly=False,
        help="Basic + House + Travelling + Other, from this payslip's own "
             "figures. Each payslip carries its own, so a later change to the "
             "employee master never reaches a payslip already issued.")
    ot_rate_regular = fields.Float(string="OT Rate - Regular Days", tracking=True)
    ot_rate_off = fields.Float(string="OT Rate - Off Days", tracking=True)

    # Work entries, added up from the daily lines below rather than copied
    # once at generation, so a corrected punch reaches the salary. Still
    # writable: a figure somebody types by hand stands until the days change
    # under it.
    total_attendance = fields.Float(
        string="Total Attendance for This Month", tracking=True,
        compute='_compute_total_attendance', store=True, readonly=False)
    overtime_reg = fields.Float(
        string="Overtime hrs - Regular Days", tracking=True,
        compute='_compute_overtime_hours', store=True, readonly=False)
    overtime_off = fields.Float(
        string="Overtime hrs - Off Days", tracking=True,
        compute='_compute_overtime_hours', store=True, readonly=False)

    # The advance days already inside total_attendance, named so a payslip can
    # be read without opening the summary behind it. total_attendance is one
    # number - days earned plus days advanced - and nothing on the payslip said
    # which part was which.
    advance_days = fields.Float(
        string="Advance Days", compute='_compute_advance', store=True,
        help="Days after the period, paid in advance, that this payslip is "
             "carrying. Zero until the sheet grants them.")
    advance_amount = fields.Monetary(
        string="Advance Amount", compute='_compute_advance', store=True,
        help="The advance days priced at this payslip's own rate per day.")

    # Computed money
    rate_per_day = fields.Monetary(
        string="Rate per Day", compute='_compute_rate_per_day', store=True,
    )
    # Tracked as well: an hours correction is only readable in the chatter next
    # to the money it moved.
    total_salary = fields.Monetary(
        string="Total Salary of this Month", compute='_compute_total_salary',
        store=True, tracking=True,
    )
    overtime_salary = fields.Monetary(
        string="Total Overtime Salary", compute='_compute_overtime_salary',
        store=True, tracking=True,
    )
    # Regular / off overtime priced separately, so analytics can sum each side
    # across employees whose hourly rates differ.
    overtime_reg_amount = fields.Monetary(
        string="Overtime Amount - Regular", compute='_compute_overtime_salary', store=True,
    )
    overtime_off_amount = fields.Monetary(
        string="Overtime Amount - Off", compute='_compute_overtime_salary', store=True,
    )
    salary_adjustment = fields.Monetary(
        string="Salary Adjustment", compute='_compute_salary_adjustment', store=True,
        tracking=True,
    )
    net_amount = fields.Monetary(
        string="Net Amount", compute='_compute_net_amount', store=True, tracking=True,
    )

    # Lines
    project_ids = fields.One2many('construction.payslip.project', 'payslip_id', string="Projects")
    attachment_ids = fields.One2many('construction.attachment', 'payslip_id', string="Salary Attachments")
    day_ids = fields.One2many('construction.payslip.day', 'payslip_id', string="Daily Attendance")

    @api.model
    def _copy_days(self, day_records):
        """Build day commands from a summary/staff daily recordset so the same
        attendance detail can be shown on the payslip."""
        return [(0, 0, {
            'date': d.date,
            'day_name': d.day_name,
            'status': d.status,
            'check_in': d.check_in,
            'check_out': d.check_out,
            'overtime': d.overtime if 'overtime' in d._fields else 0.0,
            'day_ratio': d.day_ratio if 'day_ratio' in d._fields else 0.0,
            'worked_hours': d.worked_hours if 'worked_hours' in d._fields else 0.0,
            'missing_checkout': (d.missing_checkout
                                 if 'missing_checkout' in d._fields else False),
            'is_off_day': d.is_off_day if 'is_off_day' in d._fields else False,
            'bio_check_in': d.bio_check_in if 'bio_check_in' in d._fields else False,
            'bio_check_out': d.bio_check_out if 'bio_check_out' in d._fields else False,
            'project_id': d.project_id.id or False,
        }) for d in day_records]

    state = fields.Selection(
        [('new', 'New'), ('checked', 'Checked'), ('attached', 'Attached'), ('paid', 'Paid')],
        string="Status", default='new', tracking=True, index=True,
    )
    # Auto-generated summary of the period's deductions/advance, written at
    # generation time. Shown in light purple so it reads as machine-written;
    # sanitize is off because the content is built by us, not entered by a user.
    auto_note = fields.Html(string="Auto Notes", readonly=True, sanitize=False)
    # Free notes typed by the payroll officer (plain black text).
    note = fields.Text(string="Notes & Remarks", tracking=True)

    @api.depends('basic_salary', 'house_allowance', 'transport_allowance',
                 'other_allowance')
    def _compute_gross_salary(self):
        """The four parts on THIS payslip, added up.

        Deliberately reading the payslip's own columns and not the employee's:
        the whole point of copying the contract onto the payslip at generation
        is that raising somebody's salary in March does not rewrite what they
        were paid in January. Every payslip answers for itself.
        """
        for slip in self:
            slip.gross_salary = (
                (slip.basic_salary or 0.0)
                + (slip.house_allowance or 0.0)
                + (slip.transport_allowance or 0.0)
                + (slip.other_allowance or 0.0)
            )

    @api.depends('employee_id', 'month', 'year')
    def _compute_name(self):
        for slip in self:
            slip.name = "Pay Slip for %s-%s for %s" % (
                slip.month or '', slip.year or '', slip.employee_id.name or '',
            )

    project_chart = fields.Html(
        string="Worked Days", compute='_compute_project_chart',
        sanitize=False, readonly=True,
    )
    project_chart_allocated = fields.Html(
        string="Including Advanced Days", compute='_compute_project_chart',
        sanitize=False, readonly=True,
    )

    @api.depends('project_ids.total_amount', 'project_ids.share',
                 'project_ids.allocated_total_amount',
                 'project_ids.allocated_share')
    def _compute_project_chart(self):
        for slip in self:
            label = lambda l: l.project_id.display_name
            slip.project_chart = render_project_chart(
                slip.project_ids, label_of=label,
                amount_of=lambda l: l.total_amount,
                share_of=lambda l: l.share,
                currency=slip.currency_id,
            )
            slip.project_chart_allocated = render_project_chart(
                slip.project_ids, label_of=label,
                amount_of=lambda l: l.allocated_total_amount,
                share_of=lambda l: l.allocated_share,
                currency=slip.currency_id,
            )

    def _period_days(self):
        """The daily lines inside the attendance sheet's own period.

        day_ids reach back into last month - the sheet reads that far so the
        previous cycle can be settled through its own attachments - and only
        this period is paid on this payslip.
        """
        self.ensure_one()
        sheet = self.attendance_sheet_id
        if not (sheet.start_date and sheet.last_date):
            return self.day_ids
        return self.day_ids.filtered(
            lambda d: d.date and sheet.start_date <= d.date <= sheet.last_date)

    def _keeps_stored_figures(self):
        """True when the work entries must not be re-derived.

        A slip with no attendance sheet was made by hand, and a paid slip is history:
        the money left the building and the figures behind it stand.
        """
        self.ensure_one()
        return bool(
            self.state == 'paid' or not self.attendance_sheet_id)

    @api.depends('day_ids.day_ratio', 'day_ids.date', 'state',
                 'attendance_sheet_id.start_date', 'attendance_sheet_id.last_date',
                 'summary_id.penalty_this_month', 'summary_id.advance_days',
                 'summary_id.advance_granted')
    def _compute_total_attendance(self):
        """Days earned inside the period, less the holiday penalty, plus the
        advanced days - which have no daily line of their own, being days that
        have not happened yet."""
        for slip in self:
            if slip._keeps_stored_figures():
                slip.total_attendance = slip.total_attendance
                continue
            summary = slip.summary_id
            earned = sum(slip._period_days().mapped('day_ratio'))
            advance = summary.advance_days if summary.advance_granted else 0
            slip.total_attendance = max(
                earned - (summary.penalty_this_month or 0.0), 0.0) + advance

    @api.depends('summary_id.advance_days', 'summary_id.advance_granted',
                 'rate_per_day')
    def _compute_advance(self):
        """The advance the sheet actually granted, and what it is worth.

        advance_granted is the sheet's decision, not the number of days: an
        employee under the review threshold keeps advance_days and is granted
        nothing until somebody approves it. Reading advance_days alone would
        show days this payslip is not being paid for.
        """
        for slip in self:
            summary = slip.summary_id
            days = summary.advance_days if (summary and summary.advance_granted) else 0
            slip.advance_days = days
            slip.advance_amount = days * (slip.rate_per_day or 0.0)

    @api.depends('day_ids.overtime', 'day_ids.is_off_day', 'day_ids.date',
                 'state',
                 'attendance_sheet_id.start_date', 'attendance_sheet_id.last_date')
    def _compute_overtime_hours(self):
        """Split the period's overtime the way the attendance engine classified
        each day: the weekly off day and public holidays pay the higher rate."""
        for slip in self:
            if slip._keeps_stored_figures():
                slip.overtime_reg = slip.overtime_reg
                slip.overtime_off = slip.overtime_off
                continue
            days = slip._period_days()
            slip.overtime_reg = sum(d.overtime for d in days if not d.is_off_day)
            slip.overtime_off = sum(d.overtime for d in days if d.is_off_day)

    @api.depends('gross_salary', 'days')
    def _compute_rate_per_day(self):
        for slip in self:
            slip.rate_per_day = slip.gross_salary / slip.days if slip.days else 0.0

    @api.depends('total_attendance', 'days', 'gross_salary', 'rate_per_day')
    def _compute_total_salary(self):
        for slip in self:
            if slip.days and slip.total_attendance >= slip.days:
                slip.total_salary = slip.gross_salary
            else:
                slip.total_salary = slip.rate_per_day * slip.total_attendance

    @api.depends('overtime_reg', 'ot_rate_regular', 'overtime_off', 'ot_rate_off')
    def _compute_overtime_salary(self):
        for slip in self:
            slip.overtime_reg_amount = slip.overtime_reg * slip.ot_rate_regular
            slip.overtime_off_amount = slip.overtime_off * slip.ot_rate_off
            slip.overtime_salary = (
                slip.overtime_reg_amount + slip.overtime_off_amount
            )

    @api.depends('attachment_ids.signed_value')
    def _compute_salary_adjustment(self):
        for slip in self:
            slip.salary_adjustment = sum(slip.attachment_ids.mapped('signed_value'))

    @api.depends('total_salary', 'overtime_salary', 'salary_adjustment')
    def _compute_net_amount(self):
        for slip in self:
            slip.net_amount = slip.total_salary + slip.overtime_salary + slip.salary_adjustment

    # ------------------------------------------------------------------
    # Workflow
    # ------------------------------------------------------------------
    def action_check(self):
        self.write({'state': 'checked'})

    def action_attach(self):
        for slip in self:
            slip._attach_pending_attachments()
        self.write({'state': 'attached'})

    def action_set_paid(self):
        self.write({'state': 'paid'})
        self.mapped('attachment_ids').write({'state': 'paid'})

    def action_reset(self):
        # A paid payslip reset is a payment taken back: its lines stop being
        # paid, so an advance installment is open again and a Salary Adjustment
        # no longer counts the amount, until the payslip is paid again.
        self.filtered(lambda s: s.state == 'paid').mapped('attachment_ids').filtered(
            lambda a: a.state == 'paid'
        ).with_context(construction_payroll_engine=True).write({'state': 'attached'})
        self.write({'state': 'new'})

    def action_open_advance_wizard(self):
        """Decide the advance days on this payslip by hand.

        The sheet holds an advance whenever attendance falls under half the
        period. That is a rule about arithmetic and not about the employee: the
        reason for the absence is in Time Off, or in what the payroll officer
        knows and the database does not. So the held advance waits here for a
        person, and the wizard puts the evidence - the ratio, the days, the
        leave on record and its state - beside the box being filled in.
        """
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': self.env._("Advance Days"),
            'res_model': 'construction.payslip.advance.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_payslip_id': self.id},
        }

    # ------------------------------------------------------------------
    # What last month's payslip paid for the days after its period
    # ------------------------------------------------------------------
    @api.model
    def _construction_advance_paid_map(self, employees, month, year, window_start=None):
        """{employee id: (state, days)} - what the payslip of ``month``/``year``
        did about the days after its period, for each of ``employees``.

        'granted'     the advance was paid, ``days`` of it: an absence on those
                      days is money already out, and is deducted.
        'not_granted' a payslip exists and paid no advance - rejected under the
                      review threshold, withheld from the whole sheet, or the
                      person was not eligible: nothing paid those days, so a
                      day worked on them is owed and an absence is nobody's
                      debt.
        'covered'     the payslip's own period reached into the window
                      (``window_start`` or later): the days were paid as
                      attendance, absences already taken, nothing to settle.
        'unknown'     no payslip for that month, or one that carries no sheet
                      line: the caller keeps its fallback rule.

        One search for the whole roster. The newest payslip of the month that
        carries a sheet line wins; a payslip without one does not
        outrank one that has one.
        """
        result = {}
        if not employees:
            return result
        StaffLine = self.env['construction.staff.attendance.line'].sudo()
        slips = self.sudo().search([
            ('employee_id', 'in', employees.ids),
            ('month', '=', month), ('year', '=', str(year)),
        ], order='id desc')
        for slip in slips:
            emp_id = slip.employee_id.id
            if result.get(emp_id, ('unknown', 0))[0] != 'unknown':
                continue
            state, days, period_end = 'unknown', 0, False
            summary = slip.summary_id
            if summary:
                days = summary.advance_days if summary.advance_granted else 0
                state = 'granted' if days > 0 else 'not_granted'
                period_end = slip.attendance_sheet_id.last_date
            elif slip.staff_attendance_id:
                line = StaffLine.search([
                    ('staff_attendance_id', '=', slip.staff_attendance_id.id),
                    ('employee_id', '=', emp_id)], limit=1)
                if line:
                    days = line.advance_days if line.advance_granted else 0
                    state = 'granted' if days > 0 else 'not_granted'
                    period_end = slip.staff_attendance_id.last_date
            if (state == 'not_granted' and window_start and period_end
                    and period_end >= window_start):
                state, days = 'covered', 0
            result[emp_id] = (state, days)
        return result

    def _attach_pending_attachments(self):
        """Link new attachments matching this employee/period and mark them
        attached .

        The month of every running Salary Adjustment on the employee is made
        first, so it is collected with the rest."""
        self.ensure_one()
        if self.state != 'paid':
            self.env['construction.attachment']._construction_lines_from_adjustments(
                self.employee_id, self.month, self.year)
        pending = self.env['construction.attachment'].search([
            ('employee_id', '=', self.employee_id.id),
            ('month', '=', self.month),
            ('year', '=', self.year),
            ('state', '!=', 'paid'),
            ('payslip_id', '=', False),
        ])
        pending.write({'payslip_id': self.id, 'state': 'attached'})

    # ------------------------------------------------------------------
    # Keeping the project lines in step with the payslip.
    #
    # The overtime report and the dashboard read the project lines, not the
    # payslip, so overtime corrected on a payslip has to travel down to its
    # lines - and hours corrected line by line back up to the payslip - or the
    # two would tell different stories for the same employee.
    #
    # Downwards the payslip's figure wins outright: it is what the employee is
    # paid, so the projects share exactly it. Upwards only the edit's delta is
    # carried, because a line edit must never quietly cut overtime the payslip
    # holds outside any project.
    # ------------------------------------------------------------------
    # The salary this payslip pays on is its own copy of the contract, taken
    # at generation. Raising the employee afterwards does not reach it; raising
    # it here changes this month only - and only while it is New. Once it has
    # moved on, the figures stand. The employee's record is never touched.
    _SALARY_FIELDS = ('basic_salary', 'house_allowance', 'transport_allowance',
                      'other_allowance', 'gross_salary',
                      'ot_rate_regular', 'ot_rate_off')

    def _salary_edit_guard(self, vals):
        """Refuse a salary edit on a payslip that is no longer New.

        """
        if not any(name in vals for name in self._SALARY_FIELDS):
            return
        locked = self.filtered(lambda s: s.state != 'new')
        if locked:
            raise UserError(self.env._(
                "The salary on a payslip can only be changed while it is New. "
                "%(names)s has moved on.",
                names=", ".join(locked.mapped('display_name'))))

    def _salary_change_note(self, before):
        """Say in the chatter that this month's salary moved, and that the
        employee's own record did not."""
        for slip in self:
            old = before.get(slip.id)
            if old is None or abs(old - slip.gross_salary) < 0.005:
                continue
            employee = slip.employee_id.sudo()
            slip.message_post(body=self.env._(
                "Salary for %(period)s changed on this payslip: gross %(old)s to %(new)s. "
                "The employee record still holds %(master)s, so next month is unaffected.",
                period="%s-%s" % (slip.month or '', slip.year or ''),
                old="{:,.2f}".format(old), new="{:,.2f}".format(slip.gross_salary),
                master="{:,.2f}".format(employee.construction_gross_salary or 0.0)))

    def write(self, vals):
        self._salary_edit_guard(vals)
        if not any(name in vals for name in self._SALARY_FIELDS):
            return self._write_with_overtime_sync(vals)
        before = {s.id: s.gross_salary for s in self}
        if ('basic_salary' in vals and 'ot_rate_regular' not in vals
                and 'ot_rate_off' not in vals):
            # Both overtime rates are the hourly basic times a factor, so a
            # basic raised by a fifth raises each rate by a fifth. A rate typed
            # in the same edit wins and is left alone.
            for slip in self:
                extra = {}
                if slip.basic_salary:
                    ratio = (vals['basic_salary'] or 0.0) / slip.basic_salary
                    extra = {'ot_rate_regular': slip.ot_rate_regular * ratio,
                             'ot_rate_off': slip.ot_rate_off * ratio}
                slip._write_with_overtime_sync(dict(vals, **extra))
        else:
            self._write_with_overtime_sync(vals)
        self._salary_change_note(before)
        return True

    def _write_with_overtime_sync(self, vals):
        skip = self.env.context.get('construction_skip_project_ot_sync')
        push = not skip and ('overtime_reg' in vals or 'overtime_off' in vals)
        pull = not skip and not push and 'project_ids' in vals
        before = self._project_overtime_totals() if pull else {}
        res = super().write(vals)
        if push:
            self._apply_overtime_to_projects()
        elif pull:
            self._pull_overtime_from_projects(before)
        return res

    def _project_overtime_totals(self):
        return {
            slip.id: (sum(slip.project_ids.mapped('total_overtime_reg')),
                      sum(slip.project_ids.mapped('total_overtime_off')))
            for slip in self
        }

    def _apply_overtime_to_projects(self):
        """Spread the payslip's overtime hours over its project lines.

        Each kind keeps the proportions its lines already carry, falling back
        to their share of the total overtime, then of the attendance days,
        then to an even split. The last line absorbs the rounding, so the
        lines always add up to the payslip exactly.
        """
        for slip in self:
            lines = slip.project_ids
            if not lines:
                continue
            updates = {line: {} for line in lines}
            for fname, total in (('total_overtime_reg', slip.overtime_reg),
                                 ('total_overtime_off', slip.overtime_off)):
                weights = self._overtime_weights(lines, fname)
                base = sum(weights)
                spread = 0.0
                for index, line in enumerate(lines):
                    if index == len(lines) - 1:
                        value = round(total - spread, 2)
                    else:
                        value = round(total * weights[index] / base, 2)
                    spread += value
                    if round(line[fname], 2) != value:
                        updates[line][fname] = value
            for line, line_vals in updates.items():
                if line_vals:
                    line.with_context(construction_skip_project_ot_sync=True).write(line_vals)

    @api.model
    def _overtime_weights(self, lines, fname):
        """How to divide one kind of overtime over ``lines``."""
        for weights in (lines.mapped(fname),
                        [l.total_overtime_reg + l.total_overtime_off for l in lines],
                        lines.mapped('total_attendance')):
            if sum(weights) > 0:
                return weights
        return [1.0] * len(lines)

    def _pull_overtime_from_projects(self, before):
        """Carry hours corrected on a project line up to the payslip.

        Only what the edit changed is moved, never the lines' new total: a
        payslip may legitimately carry overtime no project line accounts for
        (hours worked on a day with no project on it), and that part must
        survive an edit on one of its lines.
        """
        after = self._project_overtime_totals()
        for slip in self:
            old_reg, old_off = before.get(slip.id, (0.0, 0.0))
            new_reg, new_off = after.get(slip.id, (0.0, 0.0))
            vals = {}
            if round(new_reg - old_reg, 2):
                vals['overtime_reg'] = max(
                    0.0, round(slip.overtime_reg + new_reg - old_reg, 2))
            if round(new_off - old_off, 2):
                vals['overtime_off'] = max(
                    0.0, round(slip.overtime_off + new_off - old_off, 2))
            if vals:
                slip.with_context(construction_skip_project_ot_sync=True).write(vals)

    def unlink(self):
        # Free the salary attachments this payslip claimed so that a payslip
        # regenerated for the same period re-collects them. Paid attachments are
        # left untouched.
        self.mapped('attachment_ids').filtered(
            lambda a: a.state != 'paid').write({'state': 'new', 'payslip_id': False})
        return super().unlink()


class ConstructionPayslipDay(models.Model):
    _name = 'construction.payslip.day'
    _description = "Payslip Daily Attendance"
    _order = 'payslip_id, date'

    payslip_id = fields.Many2one(
        'construction.payslip', string="Payslip", required=True, ondelete='cascade', index=True)
    date = fields.Date(string="Date")
    day_name = fields.Char(string="Day")
    # The day as the salary read it: what priced it, the share of it that
    # was earned, and the punch pair that share was taken from.
    status = fields.Char(string="Status")
    day_ratio = fields.Float(string="Day Earned", digits=(3, 2))
    worked_hours = fields.Float(string="Worked Hours")
    overtime = fields.Float(string="Overtime")
    # The weekly off day or a public holiday: its hours are off-day overtime.
    is_off_day = fields.Boolean(string="Off Day")
    check_in = fields.Datetime(string="Check In")
    check_out = fields.Datetime(string="Check Out")
    # The same day as the device recorded it, before hr.attendance closed a
    # missing check-out on its own check-in.
    bio_check_in = fields.Datetime(string="Punch In (Device)")
    bio_check_out = fields.Datetime(string="Punch Out (Device)")
    missing_checkout = fields.Boolean(
        string="Missing Check-Out",
        help="The employee checked in and never checked out, so the day earned "
             "nothing.")
    project_id = fields.Many2one('project.project', string="Project")


class ConstructionPayslipProject(models.Model):
    _name = 'construction.payslip.project'
    _description = "Payslip Project Distribution"
    _order = 'payslip_id, id'

    payslip_id = fields.Many2one(
        'construction.payslip', string="Payslip", required=True, ondelete='cascade', index=True,
    )
    project_id = fields.Many2one('project.project', string="Project", required=True)
    currency_id = fields.Many2one(related='payslip_id.currency_id', store=True)
    # Stored copies of the payslip period/owner, so this line can be listed,
    # filtered and grouped on its own (the overtime drill-down report).
    employee_id = fields.Many2one(related='payslip_id.employee_id', store=True, index=True)
    company_id = fields.Many2one(related='payslip_id.company_id', store=True, index=True)
    month = fields.Selection(related='payslip_id.month', store=True)
    year = fields.Char(related='payslip_id.year', store=True)
    total_attendance = fields.Float(string="Total Attendance")
    # Overtime hours are split by the kind of day they were worked on, because
    # each kind is paid at its own hourly rate.
    total_overtime_reg = fields.Float(string="Overtime hrs - Regular Days")
    total_overtime_off = fields.Float(string="Overtime hrs - Off Days")
    total_overtime = fields.Float(
        string="Total Overtime hrs", compute='_compute_total_overtime', store=True,
    )

    # Money this project carries for this payslip.
    salary_amount = fields.Monetary(
        string="Salary", compute='_compute_amounts', store=True,
        help="Attendance days on this project at the payslip's daily rate.",
    )
    # The advanced days carry no daily record, so they are attributed to the
    # projects the employee was actually on rather than observed there.
    allocated_attendance = fields.Float(
        string="Allocated Days",
        help="Attendance days plus this project's share of the advanced days.",
    )
    allocated_salary_amount = fields.Monetary(
        string="Allocated Salary", compute='_compute_amounts', store=True)
    allocated_total_amount = fields.Monetary(
        string="Allocated Total", compute='_compute_amounts', store=True)
    allocated_share = fields.Float(
        string="Allocated Share %", compute='_compute_share', store=True)
    ot_reg_amount = fields.Monetary(
        string="OT Regular", compute='_compute_amounts', store=True)
    ot_off_amount = fields.Monetary(
        string="OT Off", compute='_compute_amounts', store=True)
    total_overtime_amount = fields.Monetary(
        string="Total Overtime", compute='_compute_amounts', store=True,
        help="What both kinds of overtime cost together on this project.")
    total_amount = fields.Monetary(
        string="Total", compute='_compute_amounts', store=True)
    share = fields.Float(
        string="Share %", compute='_compute_share', store=True,
        help="This project's share of the payslip's project cost.",
    )

    @api.depends('total_overtime_reg', 'total_overtime_off')
    def _compute_total_overtime(self):
        for rec in self:
            rec.total_overtime = rec.total_overtime_reg + rec.total_overtime_off

    @api.depends('total_attendance', 'allocated_attendance',
                 'total_overtime_reg', 'total_overtime_off',
                 'payslip_id.rate_per_day', 'payslip_id.ot_rate_regular',
                 'payslip_id.ot_rate_off')
    def _compute_amounts(self):
        for rec in self:
            slip = rec.payslip_id
            rec.ot_reg_amount = rec.total_overtime_reg * slip.ot_rate_regular
            rec.ot_off_amount = rec.total_overtime_off * slip.ot_rate_off
            overtime = rec.ot_reg_amount + rec.ot_off_amount
            rec.total_overtime_amount = overtime

            rec.salary_amount = rec.total_attendance * slip.rate_per_day
            rec.total_amount = rec.salary_amount + overtime

            rec.allocated_salary_amount = rec.allocated_attendance * slip.rate_per_day
            rec.allocated_total_amount = rec.allocated_salary_amount + overtime

    @api.depends('total_amount', 'allocated_total_amount',
                 'payslip_id.project_ids.total_amount',
                 'payslip_id.project_ids.allocated_total_amount')
    def _compute_share(self):
        for rec in self:
            lines = rec.payslip_id.project_ids
            total = sum(lines.mapped('total_amount'))
            rec.share = 100.0 * rec.total_amount / total if total else 0.0
            allocated = sum(lines.mapped('allocated_total_amount'))
            rec.allocated_share = (
                100.0 * rec.allocated_total_amount / allocated if allocated else 0.0)
