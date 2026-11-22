# -*- coding: utf-8 -*-
import calendar
import logging

from markupsafe import Markup
from datetime import datetime, time, timedelta

from odoo import api, fields, models
from odoo.exceptions import UserError

from .utils import MONTH_ABBR, ordinal as _ordinal

_logger = logging.getLogger(__name__)

# A day is paid in proportion to the hours worked on it, and this is the whole
# of one: eight hours earns the full day, six earns three quarters of it.
STANDARD_WORK_HOURS = 8.0

# A check-out this close to the check-in is the same tap logged twice, not a
# worked session, so the day reads as a missing check-out. Same threshold as
# the companion attendance module's MISSING_CHECKOUT_GAP_MINUTES: the two
# modules have to read a punch pair the same way, or a day is present in one and absent in the other.
MISSING_CHECKOUT_GAP_MINUTES = 15

# Share of the day each leave type pays. Sick leave follows the ladder of
# Decree-Law 33 - full pay, then half, then nothing - which is encoded in
# the leave types themselves. Every other approved leave
# is neutral: neither attendance nor absence, because it is settled outside
# this sheet.
SICK_LEAVE_RATIO = {
    'Sick Time Off': 1.0,
    'Sick Leave 50%': 0.5,
    'Sick Leave 0%': 0.0,
}

# Attendance percentage of the current period below which the advance is not
# granted automatically and has to be approved or rejected line by line.
ADVANCE_REVIEW_RATIO = 50.0

# Share of the period on a single project above which an employee counts as
# dedicated to it, so the advanced days are all charged there instead of being
# spread over the projects they merely passed through.
ADVANCE_DEDICATION_RATIO = 60.0


class ConstructionAttendanceSheet(models.Model):
    _name = 'construction.attendance.sheet'
    _description = "Monthly Attendance Sheet (Labour)"
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'last_date desc, id desc'

    name = fields.Char(string="Reference", compute='_compute_name', store=True, readonly=False, tracking=True)
    company_id = fields.Many2one(
        'res.company', string="Company", required=True, tracking=True,
        default=lambda self: self.env.company,
    )
    start_date = fields.Date(string="Start Date", required=True, tracking=True)
    last_date = fields.Date(string="Last Date", required=True, tracking=True)
    # Previous-month period. Absence / overtime computed over this range are
    # settled on the current payslip as salary attachments (not paid via base).
    last_month_start = fields.Date(string="Last Month Start", tracking=True)
    last_month_end = fields.Date(string="Last Month End", tracking=True)
    state = fields.Selection(
        [
            ('new', 'New'),
            ('generated', 'Generated'),
            ('to_approve', 'Submitted for Approval'),
            ('approved', 'Approved'),
            ('reject', 'Rejected'),
        ],
        string="Status", default='new', tracking=True, index=True,
    )
    no_advance_days = fields.Boolean(
        string="No Advance Days", tracking=True,
        help="Withhold the days between the end of the period and the end of "
             "its month from everybody on this sheet. The employees are still "
             "paid every day they worked, and last month's advance is still "
             "settled - only this period's advance is not given. Applies when "
             "the sheet is generated.",
    )
    missing_checkout_is_absent = fields.Boolean(
        string="No Check-Out Is Absence", default=True, tracking=True,
        help="On: a day with a check-in and no check-out earns nothing, "
             "UNLESS the device itself recorded a closing punch - the line's "
             "Check Out or the device's Punch Out - in which case the day is "
             "paid on those hours. Off: the day is paid whole, on the "
             "reasoning that they turned up and only the closing tap is "
             "missing. Applies when the sheet is generated.",
    )
    only_employee_ids = fields.Many2many(
        'hr.employee', 'construction_attendance_sheet_only_employee_rel',
        'sheet_id', 'employee_id', string="Only These Employees",
        help="Empty on a monthly sheet, which lists the whole company. Filled "
             "by Generate One Payslip: the sheet then lists these employees and "
             "nobody else, and its payslip carries the sheet's own period.")
    summary_ids = fields.One2many(
        'construction.attendance.summary', 'sheet_id', string="Employees",
    )
    submitted_by = fields.Many2one('res.users', string="Submitted by", readonly=True, tracking=True)
    approved_by = fields.Many2one('res.users', string="Approved by", readonly=True, tracking=True)
    approval_date = fields.Date(string="Approval Date", readonly=True, tracking=True)
    summary_count = fields.Integer(compute='_compute_summary_count')

    @api.depends('summary_ids')
    def _compute_summary_count(self):
        for sheet in self:
            sheet.summary_count = len(sheet.summary_ids)

    @api.depends('company_id', 'start_date', 'last_date')
    def _compute_name(self):
        for sheet in self:
            if not (sheet.start_date and sheet.last_date):
                sheet.name = sheet.name or "New Attendance Sheet"
                continue
            code = sheet.company_id.construction_company_code or (sheet.company_id.name or '')[:3].upper()
            s, l = sheet.start_date, sheet.last_date
            sheet.name = "%s/%s%s%s to %s%s%s/%s/Attendance Sheet" % (
                code,
                s.day, _ordinal(s.day), MONTH_ABBR[s.month],
                l.day, _ordinal(l.day), MONTH_ABBR[l.month],
                l.year,
            )

    # ------------------------------------------------------------------
    # Constraints
    # ------------------------------------------------------------------
    @api.constrains('start_date', 'last_date', 'last_month_start', 'last_month_end')
    def _check_dates(self):
        for sheet in self:
            if sheet.start_date and sheet.last_date and sheet.start_date > sheet.last_date:
                raise UserError(self.env._("Start Date must be before Last Date."))
            if sheet.last_month_start and sheet.last_month_end:
                if sheet.last_month_start > sheet.last_month_end:
                    raise UserError(self.env._(
                        "Last Month Start must be before Last Month End."))
                # The two periods must not overlap (a day would be settled twice).
                if (sheet.start_date and sheet.last_date
                        and sheet.last_month_start <= sheet.last_date
                        and sheet.last_month_end >= sheet.start_date):
                    raise UserError(self.env._(
                        "The current-month and last-month periods must not overlap."))

    # ------------------------------------------------------------------
    # Workflow
    # ------------------------------------------------------------------
    def action_generate(self):
        for sheet in self:
            if not (sheet.start_date and sheet.last_date):
                raise UserError(self.env._("Please set the Start and Last dates first."))
            sheet._generate_summaries()
            sheet.state = 'generated'
        return True

    def action_recompute(self):
        """Read the period again and rebuild the daily figures in place.

        For when something the sheet reads has changed underneath it - a leave
        approved, a punch corrected, this sheet's own settings edited - and the
        answer should follow without the sheet being deleted and made again.

        The sheet keeps its reference and its state, and every advance already
        approved or rejected keeps that decision: those are judgements about a
        person, not about the punches, and rebuilding the punches is no reason
        to ask for them twice.
        """
        for sheet in self:
            if sheet.payslip_ids:
                raise UserError(self.env._(
                    "This sheet has %(count)s payslip(s). Rebuilding it would "
                    "detach them from their summary and their advanced days "
                    "would recompute to nothing. Delete the payslips first.",
                    count=len(sheet.payslip_ids)))
            sheet._rebuild_keeping_decisions()
            if sheet.state == 'new':
                sheet.state = 'generated'
        return True

    def _rebuild_keeping_decisions(self):
        """Generate the summaries again; every advance already approved or
        rejected keeps that decision."""
        self.ensure_one()
        decided = {
            summary.employee_id.id: summary.advance_state
            for summary in self.summary_ids
            if summary.advance_state != 'pending'
        }
        self._generate_summaries()
        for summary in self.summary_ids:
            state = decided.get(summary.employee_id.id)
            if state:
                summary.advance_state = state

    def action_submit(self):
        for sheet in self:
            sheet._check_advance_decisions()
        self.write({'state': 'to_approve', 'submitted_by': self.env.uid})

    def _check_advance_decisions(self):
        """Every line flagged for review must carry an explicit decision before
        the sheet can move on: an undecided line would silently advance days to
        an employee who barely attended."""
        self.ensure_one()
        pending = self.summary_ids.filtered(
            lambda s: s.needs_review and s.advance_state == 'pending')
        if not pending:
            return
        raise UserError(self.env._(
            "These employees attended less than %(ratio)g%% of the period. "
            "Approve or reject their advance before submitting:\n\n%(names)s",
            ratio=ADVANCE_REVIEW_RATIO,
            names="\n".join(
                "- %s (%.0f%% attendance: %s of %s days)" % (
                    s.employee_id.display_name, s.attendance_ratio,
                    s.attended_days, s.period_days)
                for s in pending),
        ))

    def action_approve(self):
        """Approve, reading the period once more first.

        The engine counts a leave only once Time Off has validated it, and a
        sick leave is often validated after the sheet was generated: the day
        was absence when the sheet was built and is sick leave now. Approval
        is the moment the figures become the month's, so they are rebuilt here
        from what is approved at this moment - the punches too - keeping every
        advance decision. A sheet that already carries payslips is not rebuilt
        (that would detach them); it is approved as it stands and says so.
        """
        for sheet in self:
            if sheet.payslip_ids:
                sheet.message_post(body=self.env._(
                    "Approved as generated: the sheet already carries %(count)s "
                    "payslip(s), so its figures were not read again.",
                    count=len(sheet.payslip_ids)))
            else:
                sheet._rebuild_keeping_decisions()
        self.write({
            'state': 'approved',
            'approved_by': self.env.uid,
            'approval_date': fields.Date.context_today(self),
        })

    def _unsettled_sick_leave(self):
        """{employee: (sick days the summary holds, sick days Time Off holds
        now)} where the two disagree - a sick leave validated after approval.

        The base salary is the same either way, an absent day and a sick day
        both earning nothing there; what would go missing is the attachment
        that pays the sick day back. Payroll generation asks this before it
        writes anything, and names the difference on the sheet.
        """
        self.ensure_one()
        summaries = self.summary_ids.filtered('employee_id')
        if not summaries:
            return {}
        span_start, span_last = self._period_span()
        leave_map = self._hr_leave_map(summaries.mapped('employee_id'), span_start, span_last)

        def sick_days(employee_id, r_start, r_last):
            if not (r_start and r_last):
                return 0.0
            days = leave_map.get(employee_id) or {}
            return float(sum(1 for d, (ratio, kind) in days.items()
                             if kind == 'leave_sick' and r_start <= d <= r_last))

        differ = {}
        for summary in summaries:
            now_this = sick_days(summary.employee_id.id, self.start_date, self.last_date)
            now_last = (sick_days(summary.employee_id.id, self.last_month_start,
                                  self.last_month_end) if summary.has_advance else 0.0)
            held = summary.sick_days_this_month + summary.sick_days_last_month
            if abs(held - (now_this + now_last)) >= 0.5:
                differ[summary.employee_id] = (held, now_this + now_last)
        return differ

    def action_reject(self):
        self.write({'state': 'reject'})

    def action_reset_to_new(self):
        self.write({'state': 'new'})

    # ------------------------------------------------------------------
    # Calculation engine
    # ------------------------------------------------------------------
    def _get_employees(self):
        """Labour employees of the sheet's company, read from hr.employee.

        hr.employee is the roster, the names, the salary and the
        classification. Everybody active is on the sheet except the
        Office/Engineer staff, who are paid through the staff attendance
        sheet. The "Staff" flag does NOT exclude an employee here, and
        neither does a departure date or a leave: those only withhold the
        advance. Cancelled employees are the archived ones, and the default
        active_test leaves them out.
        """
        self.ensure_one()
        domain = [
            ('company_id', '=', self.company_id.id),
            ('construction_is_office_staff', '=', False),
        ]
        if self.only_employee_ids:
            # A sheet made for named people lists them and nobody else. Every
            # other rule of the roster still applies to them.
            domain.append(('id', 'in', self.only_employee_ids.ids))
        return self.env['hr.employee'].sudo().search(domain)

    def _period_span(self):
        """Overall [start, last] covering both the current and last-month
        ranges, used to read the punches and the leaves in one pass."""
        self.ensure_one()
        starts, ends = [self.start_date], [self.last_date]
        if self.last_month_start and self.last_month_end:
            # Include the last month's main period (1st -> day before the
            # last-month period) so we can tell whether the advance was given.
            starts.append(self.last_month_start.replace(day=1))
            ends.append(self.last_month_end)
        return min(starts), max(ends)

    def _data_span(self):
        """The span the punches, leaves and holidays are read over: the
        periods, plus one day on each side.

        The weekly off day is priced from its neighbours and a holiday's
        penalty from the days either side of it. When the period opens on a
        Friday, the Thursday before it belongs to another period - it is
        still a real day with a real punch record, and reading it is the
        only way to know whether the Friday is stranded. Left unread it
        looked like a blank, and a blank reads as absence.
        """
        span_start, span_last = self._period_span()
        return span_start - timedelta(days=1), span_last + timedelta(days=1)

    def _get_public_holidays(self, span_start, span_last):
        """Map {public-holiday date: name} in the span for this company.
        A holiday with no company applies to every company."""
        self.ensure_one()
        holidays = self.env['construction.public.holiday'].search([
            ('date', '>=', span_start), ('date', '<=', span_last),
            '|', ('company_id', '=', False), ('company_id', '=', self.company_id.id),
        ])
        result = {}
        for h in holidays:
            result.setdefault(h.date, h.name)
        return result

    def _advance_days(self, profile, month_end):
        """Number of days paid in advance: everything after the period up to
        the end of its month. An employee joining inside that window is only
        advanced from their joining date on."""
        self.ensure_one()
        first = self.last_date + timedelta(days=1)
        joining = profile['joining_date']
        if joining and joining > first:
            first = joining
        return max((month_end - first).days + 1, 0)

    def _generate_summaries(self):
        self.ensure_one()
        self.summary_ids.unlink()

        employees = self._get_employees()
        off_weekday = int(self.company_id.construction_weekly_off_day or '4')
        span_start, span_last = self._period_span()
        data_start, data_last = self._data_span()
        holidays = self._get_public_holidays(data_start, data_last)
        # The days between the end of the period and the end of its month are
        # paid in advance, so their count follows the calendar rather than a
        # fixed number: 01->20 JUN advances 10 days (June has 30), while
        # 01->20 MAY advances 11 (May has 31).
        month_days = calendar.monthrange(self.last_date.year, self.last_date.month)[1]
        month_end = self.last_date.replace(day=month_days)

        # Everything the period needs, read once for the whole sheet rather
        # than once per employee.
        punch_map = self._hr_attendance_map(employees, data_start, data_last)
        leave_map = self._hr_leave_map(employees, data_start, data_last)
        bio_map = self._biometric_map(employees, data_start, data_last)
        self._flag_attendance_gaps(
            employees, punch_map, leave_map, span_start, span_last)

        # One leave search for the whole roster rather than one per employee.
        eligible = employees._construction_advance_eligible()
        # What last month's payslip did about the days now being settled -
        # one search for the roster. Without a last-month window there is
        # nothing to settle and nothing to ask.
        advance_paid = {}
        if self.last_month_start and self.last_month_end:
            advance_paid = self.env['construction.payslip']._construction_advance_paid_map(
                employees, MONTH_ABBR[self.last_month_end.month],
                self.last_month_end.year, self.last_month_start)

        vals_list = []
        for emp in employees:
            profile = self._employee_profile(emp, eligible.get(emp.id))
            # Skip employees who joined after the advanced days end.
            if profile['joining_date'] and profile['joining_date'] > month_end:
                continue
            punches = punch_map.get(emp.id) or {}
            bio = bio_map.get(emp.id) or {}
            day_map = self._build_day_map(
                punches, leave_map.get(emp.id) or {}, bio, holidays, off_weekday)

            this = self._compute_range(
                profile, day_map, self.start_date, self.last_date, holidays, off_weekday)
            # Same rule for both periods: the Friday is answered for by its
            # neighbours in last month exactly as in this one.
            last = self._compute_range(
                profile, day_map, self.last_month_start, self.last_month_end,
                holidays, off_weekday)

            # Display-only daily snapshot: the punch pair the day was priced
            # from, the share of the day it earned, and the project worked.
            earned = dict(last['earned'])
            earned.update(this['earned'])
            day_cmds = self._build_summary_days(
                day_map, punches, bio, earned,
                holidays, off_weekday, profile['overtime_eligible'])

            # Somebody with no punch at all inside the period is not on the
            # sheet: a line of thirty-one absences for a person who was never
            # here is a line somebody has to explain, and nothing on it is
            # paid. A day on approved leave or a public holiday counts as
            # being here; only a period that earned nothing and holds no leave
            # is empty.
            if not any(
                    (entry['kind'] != 'punch' or entry['ratio'])
                    for day, entry in day_map.items()
                    if self.start_date <= day <= self.last_date):
                continue

            # The holiday penalty is the one deduction the daily shares do not
            # already carry, so it comes off the earned total here.
            attended = max(this['attended'] - this['penalty'], 0.0)
            # The advance is withheld from employees who are leaving or away.
            has_advance = profile['has_advance']

            # Last month is settled by what last month's payslip paid, not by
            # whether this person is eligible today. Deducting an absence from
            # somebody whose advance was withheld charged them for days they
            # were never given. The eligibility rule is used only where nothing
            # better is known: no payslip for last
            # month, and not eligible now, means nothing to reconcile.
            last_state = (advance_paid.get(emp.id, ('unknown', 0))[0]
                          if advance_paid or (self.last_month_start and self.last_month_end)
                          else False)
            if last_state == 'covered' or (last_state == 'unknown' and not has_advance):
                last = self._empty_range()
            worked_last = max(last['attended'] - last['penalty'], 0.0)
            worked_last_dates = sorted(d for d, share in last['earned'].items() if share)

            vals_list.append((0, 0, {
                'employee_id': emp.id,
                'attended_days': attended,
                'period_days': this['period_days'],
                'advance_days': 0 if self.no_advance_days
                                  else self._advance_days(profile, month_end),
                'has_advance': bool(has_advance),
                'absence_this_month': this['absence'],
                'penalty_this_month': this['penalty'],
                'absence_this_dates': self._fmt_dates(this['absent_dates']),
                'sick_days_this_month': this['sick'],
                'sick_paid_this_month': this['sick_paid'],
                'sick_this_detail': self._fmt_sick(this['sick_detail']),
                'penalty_this_detail': this['penalty_detail'],
                'reg_ot_this_month': this['reg_ot'],
                'off_ot_this_month': this['off_ot'],
                'last_month_advance': last_state,
                'worked_last_month': worked_last,
                'worked_last_dates': self._fmt_dates(worked_last_dates),
                'absence_last_month': last['absence'],
                'penalty_last_month': last['penalty'],
                'reg_ot_last_month': last['reg_ot'],
                'off_ot_last_month': last['off_ot'],
                'absence_last_dates': self._fmt_dates(last['absent_dates']),
                'sick_days_last_month': last['sick'],
                'sick_paid_last_month': last['sick_paid'],
                'sick_last_detail': self._fmt_sick(last['sick_detail']),
                'penalty_last_detail': last['penalty_detail'],
                'reg_ot_last_detail': self._fmt_ot(last['reg_ot_detail']),
                'off_ot_last_detail': self._fmt_ot(last['off_ot_detail']),
                'day_ids': day_cmds,
            }))
        self.summary_ids = vals_list

    # ------------------------------------------------------------------
    # What the engine could not see
    # ------------------------------------------------------------------
    def _pending_leaves(self, employees, span_start, span_last):
        """{employee id: leaves} still waiting on somebody's approval.

        _hr_leave_map reads state 'validate' only, because a leave nobody has
        approved is not yet a fact. But then the day is read from the punches
        alone, and a sick week still sitting in To Approve becomes a week of
        absence with a deduction behind it.
        """
        result = {}
        Leave = self.env.get('hr.leave')
        if Leave is None or not employees:
            return result
        for leave in Leave.sudo().with_context(lang='en_US').search(
                [('state', 'in', ('confirm', 'validate1'))]
                + self._leave_domain(employees, span_start, span_last)):
            result.setdefault(leave.employee_id.id, []).append(leave)
        return result

    def _flag_attendance_gaps(self, employees, punch_map, leave_map,
                              span_start, span_last):
        """Name what the period could not account for, on the sheet itself.

        Two things turn into a deduction without anybody deciding they should:
        an employee with no punch and no approved leave at all, who is far more
        likely to be a hole in the data than somebody who vanished for two
        months; and a leave that exists but is still waiting on an approval,
        which the engine is right not to count and wrong to leave unsaid.

        Posted to the chatter rather than only logged: the person generating
        the sheet is the person who can still fix it.
        """
        self.ensure_one()
        pending = self._pending_leaves(employees, span_start, span_last)
        silent = employees.filtered(
            lambda e: not punch_map.get(e.id) and not leave_map.get(e.id))
        if not (pending or silent):
            return

        blocks = []
        if silent:
            blocks.append((
                self.env._(
                    "%(count)s employee(s) have no punch and no approved leave "
                    "between %(start)s and %(last)s. Every working day of "
                    "theirs reads as absence.",
                    count=len(silent), start=span_start, last=span_last),
                ["%s (%s)" % (e.name, e.barcode or '-') for e in silent]))
        if pending:
            names = []
            for employee in employees:
                for leave in pending.get(employee.id, []):
                    leave_start, leave_end = self._leave_span(leave)
                    names.append("%s - %s, %s to %s" % (
                        employee.name, leave.holiday_status_id.name,
                        leave_start, leave_end))
            blocks.append((
                self.env._(
                    "%(count)s leave(s) are still waiting for approval and were "
                    "not counted. Those days read as absence until they are "
                    "approved and the sheet is generated again.",
                    count=len(names)),
                names))

        body = Markup('')
        for headline, names in blocks:
            shown = names[:30]
            more = len(names) - len(shown)
            body += Markup("<p><b>%s</b></p><ul>%s</ul>") % (
                headline,
                Markup('').join(Markup("<li>%s</li>") % n for n in shown))
            if more:
                body += Markup("<p>%s</p>") % self.env._(
                    "and %(more)s more.", more=more)
        self.message_post(body=body)
        _logger.warning(
            "construction_payroll: sheet %s - %s employee(s) with no record at all, "
            "%s leave(s) awaiting approval",
            self.display_name, len(silent),
            sum(len(v) for v in pending.values()))

    def _employee_profile(self, employee, has_advance=None):
        """The facts the attendance engine needs about one employee.

        hr.employee is who they are, what they punched and what they are paid;
        these three facts are the only part of it the day arithmetic reads.
        The advance answer is passed in when the caller has already asked it
        for the whole roster (one leave search, not one per employee), and
        asked here otherwise.
        """
        employee = employee.sudo()
        if has_advance is None:
            has_advance = employee._construction_advance_eligible().get(employee.id, False)
        return {
            'employee': employee,
            'joining_date': employee.contract_date_start,
            'overtime_eligible': employee.construction_overtime_eligible,
            # Archived employees never reach here: they are out of the sheet.
            'has_advance': bool(has_advance),
        }

    # ------------------------------------------------------------------
    # Daily sources: punches, leaves, projects
    # ------------------------------------------------------------------
    def _hr_attendance_map(self, employees, span_start, span_last):
        """{employee id: {date: punch dict}} over the span, in one search.

        hr.attendance.date is stored and computed in the employee's timezone,
        so grouping on it needs no timezone arithmetic here. A day can carry
        several records: the day opens on the earliest check-in, closes on the
        latest check-out, and its worked and overtime hours are the sum.
        """
        result = {}
        if not employees:
            return result
        records = self.env['hr.attendance'].sudo().search([
            ('employee_id', 'in', employees.ids),
            ('date', '>=', span_start), ('date', '<=', span_last),
        ])
        for att in records:
            if not att.date:
                continue
            day = result.setdefault(att.employee_id.id, {}).setdefault(att.date, {
                'check_in': False, 'check_out': False, 'worked': 0.0,
            })
            if att.check_in and (not day['check_in'] or att.check_in < day['check_in']):
                day['check_in'] = att.check_in
            if att.check_out and (not day['check_out'] or att.check_out > day['check_out']):
                day['check_out'] = att.check_out
            day['worked'] += att.worked_hours or 0.0

        for days in result.values():
            for day in days.values():
                day['missing_checkout'] = self._is_missing_checkout(
                    day['check_in'], day['check_out'])
                if day['missing_checkout']:
                    # A check-in nobody closed is not a worked day. Whatever
                    # the clock says, it earns nothing.
                    day['worked'] = 0.0
        return result

    @staticmethod
    def _is_missing_checkout(check_in, check_out):
        """True when the day carries a check-in the employee never closed.

        construction_attendance mirrors a punch-out it never saw by closing the record
        on the check-in itself, so the signal reaches hr.attendance as a record
        of no length rather than an open one - both shapes are caught here.
        """
        if not check_in or not check_out:
            return True
        return (check_out - check_in) <= timedelta(minutes=MISSING_CHECKOUT_GAP_MINUTES)

    @staticmethod
    def _leave_span(leave):
        """The local dates a leave covers.

        request_date_from / request_date_to are plain Date fields, not computed
        ones - a leave written programmatically can carry only date_from and
        date_to and leave them empty. Reading the request dates alone made such
        a leave invisible: NULL fails every comparison, so it never matched the
        period and its days were charged as absence.
        """
        start = leave.request_date_from or (
            leave.date_from and leave.date_from.date())
        end = leave.request_date_to or (
            leave.date_to and leave.date_to.date())
        return start, (end or start)

    @staticmethod
    def _leave_domain(employees, span_start, span_last):
        """Leaves overlapping the span, matched on date_from / date_to.

        Those two are always set - the leave could not exist without them -
        while the request dates can be empty. See _leave_span.
        """
        return [
            ('employee_id', 'in', employees.ids),
            ('date_from', '<=', datetime.combine(span_last, time.max)),
            ('date_to', '>=', datetime.combine(span_start, time.min)),
        ]

    def _hr_leave_map(self, employees, span_start, span_last):
        """{employee id: {date: (ratio, kind)}} from approved Time Off.

        Sick leave pays the share its own type carries; every other approved
        leave is neutral and carries no ratio at all, so the day counts as
        neither attendance nor absence. Leave type names are read in English,
        because that is what SICK_LEAVE_RATIO is keyed on.
        """
        result = {}
        Leave = self.env.get('hr.leave')
        if Leave is None or not employees:
            return result
        leaves = Leave.sudo().with_context(lang='en_US').search(
            [('state', '=', 'validate')]
            + self._leave_domain(employees, span_start, span_last))
        for leave in leaves:
            name = leave.holiday_status_id.name or ''
            is_sick = name in SICK_LEAVE_RATIO
            ratio = SICK_LEAVE_RATIO.get(name)
            kind = 'leave_sick' if is_sick else 'leave_other'
            leave_start, leave_end = self._leave_span(leave)
            if not (leave_start and leave_end):
                continue
            days = result.setdefault(leave.employee_id.id, {})
            current = max(leave_start, span_start)
            end = min(leave_end, span_last)
            while current <= end:
                previous = days.get(current)
                # Two leaves on one date: a sick day outranks a neutral one,
                # and between two sick days the one that pays more wins.
                if (previous is None
                        or (is_sick and (previous[1] != 'leave_sick'
                                         or ratio > previous[0]))):
                    days[current] = (ratio, kind)
                current += timedelta(days=1)
        return result

    def _biometric_map(self, employees, span_start, span_last):
        """{employee id: {date: {'project', 'check_in', 'check_out'}}} from the
        biometric attendance lines.

        Display only - none of it prices a day. The value is that it is the
        machine's own record: construction_attendance closes a check-out it never saw on
        the check-in itself before mirroring to hr.attendance, so a day can read
        differently on the two sides. Showing both is what makes that visible
        instead of leaving an absence looking unexplained.

        Read dynamically so this module keeps no hard dependency on
        construction_attendance, and matched on the link the punch line carries rather
        than on the badge: one badge rule in the codebase is enough, and the
        line resolved its own once already.
        """
        result = {}
        Line = self.env.get('construction.attendance.line')
        if Line is None or not employees or 'hr_employee_id' not in Line._fields:
            return result
        # The device columns arrive with construction_attendance 2.17; without them
        # there is nothing honest to show, so the pair stays empty rather than
        # falling back to values somebody may have corrected.
        device = 'device_first_punch' in Line._fields
        lines = Line.sudo().search([
            ('date', '>=', span_start), ('date', '<=', span_last),
            ('hr_employee_id', 'in', employees.ids),
        ])
        for line in lines:
            if not line.date:
                continue
            result.setdefault(line.hr_employee_id.id, {})[line.date] = {
                'project': line.project_id.id or False,
                'check_in': device and line.device_first_punch or False,
                'check_out': device and line.device_last_punch or False,
                # The pair as it stands on the line - corrected by hand if
                # anybody did - beside the pair the device recorded. Either
                # closed answers for a day hr.attendance could not close.
                'line_in': line.first_punch or False,
                'line_out': line.last_punch or False,
                # The hours the punch module worked out for itself: the whole
                # day, and the part of it above a standard shift.
                'worked': (line.total_time or 0.0) + (line.total_ot or 0.0),
                'overtime': line.total_ot or 0.0,
            }
        return result

    def _build_day_map(self, punches, leaves, bio, holidays, off_weekday):
        """{date: {'ratio', 'overtime', 'kind'}} for one employee.

        ``ratio`` is the share of the day that is earned: one for a full day,
        a fraction for a short one, zero for an absence, and None for a day
        that is neither - an approved leave settled outside this sheet.

        An approved leave outranks the punches: the leave type says what the
        day pays, and a sick day nobody punched is not an absence.
        """
        day_map = {}
        charge_missing = self.missing_checkout_is_absent
        for day, punch in punches.items():
            worked = punch['worked']
            if punch.get('missing_checkout'):
                # hr.attendance closes a punch-out it never saw on the
                # check-in itself, so every unclosed day reaches here as a
                # record of no length - whatever the device actually said.
                # Before charging it, ask the device: the line's own pair,
                # or the pair the machine recorded, either one closed is a
                # worked day, priced on the hours the line worked out.
                device_hours = self._device_hours(bio.get(day))
                if device_hours > 0:
                    ratio = min(device_hours / STANDARD_WORK_HOURS, 1.0)
                elif not charge_missing:
                    # They turned up; only the closing tap is missing. The
                    # overtime stays zero either way - nobody knows how long
                    # they stayed.
                    ratio = 1.0
                else:
                    ratio = 0.0
            else:
                ratio = min(worked / STANDARD_WORK_HOURS, 1.0) if worked > 0 else 0.0
            day_map[day] = {
                'ratio': ratio,
                'overtime': self._device_overtime(
                    bio.get(day), day in holidays, day.weekday() == off_weekday),
                'kind': 'punch',
            }
        for day, (ratio, kind) in leaves.items():
            entry = day_map.get(day)
            if kind == 'leave_other':
                # Approved, but not gone yet: a leave is often approved before
                # the travel, and the days worked in between are worked days.
                # A day with a punch - hr.attendance or the device - is priced
                # as any punched day and only labelled as falling in the
                # leave. Sick leave is not: its own type prices the day.
                worked = self._ratio_worked_during_leave(entry, bio.get(day), charge_missing)
                if worked:
                    day_map[day] = {
                        'ratio': worked,
                        'overtime': entry['overtime'] if entry else self._device_overtime(
                            bio.get(day), day in holidays, day.weekday() == off_weekday),
                        'kind': 'punch',
                        'on_leave': True,
                    }
                    continue
            day_map[day] = {
                'ratio': ratio,
                # Somebody who punched during an approved leave still worked
                # those hours, so the overtime survives the overlay.
                'overtime': entry['overtime'] if entry else 0.0,
                'kind': kind,
            }
        return day_map

    @classmethod
    def _ratio_worked_during_leave(cls, entry, raw, charge_missing):
        """The share of a leave day the employee worked, or 0 if they did not.

        The punch already priced from hr.attendance answers first. A day only
        the device saw - no hr.attendance record behind it - is priced the way
        an unclosed day is: on the hours of a closed pair, else whole when an
        open punch-in is not charged as absence. A day that earned nothing
        stays the leave it was approved as.
        """
        if entry and entry.get('ratio'):
            return entry['ratio']
        if not raw or not (raw.get('check_in') or raw.get('line_in')):
            return 0.0
        hours = cls._device_hours(raw)
        if hours > 0:
            return min(hours / STANDARD_WORK_HOURS, 1.0)
        return 0.0 if charge_missing else 1.0

    @staticmethod
    def _device_hours(raw):
        """Hours the punch line itself stands behind, for a day hr.attendance
        could not close.

        A day counts as closed when either pair on the line is a real pair -
        Check In / Check Out as corrected, or Punch In / Punch Out as the
        device recorded them - and the hours are the line's own reckoning
        (total_time + total_ot), which is what it computed from that pair.
        A pair that is the same tap twice is not a pair.
        """
        if not raw:
            return 0.0
        for start, stop in ((raw.get('line_in'), raw.get('line_out')),
                            (raw.get('check_in'), raw.get('check_out'))):
            if not (start and stop):
                continue
            hours = (stop - start).total_seconds() / 3600.0
            if hours * 60.0 <= MISSING_CHECKOUT_GAP_MINUTES:
                continue
            # The line's own arithmetic, applied to the pair that is closed:
            # the lunch hour comes off when the shift ran into the afternoon.
            # total_time on the line was computed from the line's own pair,
            # which may be the unclosed one, so it cannot be read here.
            if (stop + timedelta(hours=4)).hour >= 13:
                hours -= 1.0
            return max(hours, 0.0)
        return 0.0

    @staticmethod
    def _device_overtime(raw, is_holiday, is_off_weekday):
        """Overtime hours for one day, from the punch module's own reckoning.

        construction.attendance.line works the hours out from the punches alone -
        check-out minus check-in, less the lunch break, less a standard shift -
        and owes nothing to a working schedule or a contract ruleset. That is
        the point: hr.attendance.overtime_hours reads those, and reads them
        wrong here.

        The classification is the sheet's, not the line's. The line only knows
        Regular Day and Off Day and has never heard of a public holiday, so a
        full shift worked on Eid would come back as no overtime at all. On any
        day this sheet calls an off day - the weekly one or a public holiday -
        there is no standard shift to exceed, so every hour worked is overtime.
        """
        if not raw:
            return 0.0
        if is_holiday or is_off_weekday:
            return raw.get('worked') or 0.0
        return raw.get('overtime') or 0.0

    def _build_summary_days(self, day_map, punches, bio, earned, holidays,
                            off_weekday, eligible):
        """Build the daily-attendance commands.

        ``day_ratio`` is what the day actually earned, taken from the engine
        rather than from the punches: a public holiday and a bridged off day
        are paid whole even though nobody punched them, and a column that read
        the punches instead would add up to less than the total above it.

        ``is_off_day`` and the overtime follow the same reading - the weekly
        off day and a public holiday send their hours to the off-day bucket,
        and an employee who earns no overtime is shown none.
        """
        cmds = []
        for d in sorted(set(day_map) | set(punches) | set(bio) | set(earned)):
            entry = day_map.get(d) or {}
            punch = punches.get(d) or {}
            raw = bio.get(d) or {}
            is_holiday = d in holidays
            is_off = d.weekday() == off_weekday
            # ``earned`` holds every day inside the two priced ranges and
            # nothing else. A day outside them - the maps are read wider than
            # the periods so a Friday at the edge can see its neighbours, and
            # a sheet settling late in a month reads early in it too - was never
            # judged, so it is shown for what it is and never as absence. The
            # first version fell back to the raw punch map, where a Friday
            # nobody punched has no entry, and printed "Absent 0.00" over
            # Fridays that cost nobody a fils.
            priced = d in earned
            share = earned.get(d, entry.get('ratio') or 0.0)
            cmds.append((0, 0, {
                'date': d,
                'day_name': d.strftime('%A'),
                'status': self._day_status(entry, share, is_holiday, is_off,
                                           priced=priced),
                'day_ratio': share,
                'worked_hours': punch.get('worked') or 0.0,
                'is_off_day': is_holiday or is_off,
                'overtime': (entry.get('overtime') or 0.0) if eligible else 0.0,
                'project_id': raw.get('project') or False,
                'check_in': punch.get('check_in') or False,
                'check_out': punch.get('check_out') or False,
                'missing_checkout': bool(punch.get('missing_checkout')),
                'bio_check_in': raw.get('check_in') or False,
                'bio_check_out': raw.get('check_out') or False,
            }))
        return cmds

    @staticmethod
    def _day_status(entry, share, is_holiday, is_off, priced=True):
        """The label beside the day, naming whatever priced it.

        ``priced`` is False for a day outside the sheet's counted ranges. Such
        a day is context - it was read so a neighbour could be judged - and
        it is labelled by what happened on it, never as Absent: absence is a
        verdict, and the engine reached none for it.
        """
        kind = (entry or {}).get('kind')
        if is_holiday:
            return 'Holiday'
        if kind == 'leave_sick':
            return 'Sick Leave'
        if kind == 'leave_other':
            return 'On Leave'
        if (entry or {}).get('on_leave'):
            return 'Worked during leave'
        if is_off:
            if not priced:
                return 'Off Day'
            return 'Off Day' if share else 'Absent'
        if not share:
            return 'Absent' if priced else 'Not Counted'
        return 'Present' if share >= 1.0 else 'Partial'

    @staticmethod
    def _attended(day_map, holidays, d):
        """A day answers for its neighbour when it is a public holiday, an
        approved leave, or a day the employee earned some part of.

        A leave answers because it is not absence - the same reading the
        holiday penalty takes. Without it somebody on approved annual leave
        over a weekend loses the Friday inside it.
        """
        if d in holidays:
            return True
        entry = day_map.get(d)
        if not entry:
            return False
        return entry['kind'] != 'punch' or bool(entry['ratio'])

    def _neighbour_attended(self, day_map, holidays, d, joining, r_last=None):
        """_attended, for a day that may lie where no record can exist.

        A day after today has not happened: nobody can have been absent on
        it, so it does not take the Friday before it away. A day before the
        employee joined is nobody's absence either. And a day after the
        period's last day is not this period's to judge: it belongs to the
        next sheet, its punch may not have reached the clock yet when this
        one is generated, and reading it here would take a day away from a person
        who was there the day before because the next one read as one unclosed
        tap. The last Friday of a period is answered for by
        the day before it alone. Any other day is read as it is - the maps
        are loaded one day wider than the periods precisely so that the
        neighbour of a period's FIRST day is a real day and not a blank.
        """
        if d > fields.Date.context_today(self):
            return True
        if joining and d < joining:
            return True
        if r_last and d > r_last:
            return True
        return self._attended(day_map, holidays, d)

    @staticmethod
    def _empty_range():
        return {'period_days': 0, 'attended': 0.0, 'absence': 0.0, 'penalty': 0.0,
                'sick': 0.0, 'neutral': 0, 'reg_ot': 0.0, 'off_ot': 0.0,
                'absent_dates': [], 'sick_dates': [],
                'reg_ot_detail': [], 'off_ot_detail': [], 'penalty_detail': '',
                # Sick days are settled by their own attachment, so the base
                # pays none of them: 'sick' is how many were taken out of it,
                # 'sick_paid' is how much of that the attachment gives back.
                'sick_paid': 0.0, 'sick_detail': [],
                # {date: share of the day it earned} - the daily lines are
                # written from this, so the column adds up to the total above
                # it instead of to something close.
                'earned': {}}

    @staticmethod
    def _fmt_dates(dates):
        return ", ".join(str(d) for d in dates)

    @staticmethod
    def _fmt_ot(pairs):
        return ", ".join("%s (%gh)" % (d, h) for d, h in pairs)

    @staticmethod
    def _fmt_sick(pairs):
        """Sick days with the share each one pays, so the attachment behind
        them can be read without opening the leave."""
        return ", ".join("%s (%g%%)" % (d, ratio * 100) for d, ratio in pairs)

    def _compute_range(self, profile, day_map, r_start, r_last, holidays,
                       off_weekday):
        """Compute attendance figures over the [r_start, r_last] range.

        Every day earns a share of itself. A public holiday earns all of it; a
        working day earns the hours punched over the eight of a whole one; the
        weekly off day earns all of it when it is worked or when both the day
        before and the day after were attended, and nothing when either was
        missed; a sick day earns what its leave type pays. Any other approved leave earns nothing and is
        not absence either - it is settled outside this sheet.

        Absence is the part of a counted day that was not earned, so half a day
        worked is half a day absent, and a check-in with no check-out earns
        nothing at all. Any overtime worked on the weekly off day OR on a
        public holiday is counted as "off" overtime. A separate holiday penalty
        is added for absence around holidays.
        """
        res = self._empty_range()
        if not (r_start and r_last) or r_start > r_last:
            return res

        eff_start = r_start
        joining = profile['joining_date']
        if joining:
            if joining > r_last:
                return res
            if joining > r_start:
                eff_start = joining

        eligible = profile['overtime_eligible']
        total_days = 0
        current = eff_start
        while current <= r_last:
            total_days += 1
            entry = day_map.get(current)
            ratio = entry['ratio'] if entry else 0.0
            kind = entry['kind'] if entry else 'punch'
            overtime = (entry['overtime'] if entry else 0.0) if eligible else 0.0
            is_holiday = current in holidays
            is_off = current.weekday() == off_weekday

            earned = 0.0
            if kind == 'leave_other':
                # Approved leave settled elsewhere: neither earned nor absent.
                res['neutral'] += 1
                self._add_overtime(res, current, overtime, is_holiday or is_off)
            elif is_holiday:
                # Public holiday: paid whole, and never absence here - the
                # penalty for absence around it is handled apart.
                earned = 1.0
                res['attended'] += 1.0
                self._add_overtime(res, current, overtime, True)
            elif is_off:
                # The weekly off day is paid when it is worked, or when the
                # employee was there on BOTH sides of it. Absent on the
                # Thursday or on the Saturday and the Friday between them is
                # not paid: that is the two-day deduction, the rule the
                # company applies. A neighbour outside the
                # period is read from its own record, a neighbour still in
                # the future or before the joining date is not held against
                # anybody - see _neighbour_attended.
                bridged = (
                    self._neighbour_attended(day_map, holidays,
                                             current - timedelta(days=1), joining, r_last)
                    and self._neighbour_attended(day_map, holidays,
                                                 current + timedelta(days=1), joining, r_last))
                if ratio or bridged:
                    earned = 1.0
                    res['attended'] += 1.0
                    self._add_overtime(res, current, overtime, True)
                else:
                    res['absence'] += 1.0
                    res['absent_dates'].append(current)
            elif kind == 'leave_sick':
                # Neutral in the base: the whole day comes out of it and the
                # sick-leave attachment pays back whatever the leave type pays.
                # Earning any part of it here would pay the day twice. It is
                # still not absence - nobody is answerable for being ill.
                earned = 0.0
                res['sick'] += 1.0
                res['sick_paid'] += ratio
                res['sick_dates'].append(current)
                res['sick_detail'].append((current, ratio))
                self._add_overtime(res, current, overtime, False)
            else:
                # Working day: it earns the share of itself that was worked.
                earned = ratio
                res['attended'] += ratio
                if ratio < 1.0:
                    res['absence'] += 1.0 - ratio
                    res['absent_dates'].append(current)
                self._add_overtime(res, current, overtime, False)

            res['earned'][current] = earned
            current += timedelta(days=1)

        res['period_days'] = total_days
        penalty, detail = self._holiday_penalty(
            day_map, holidays, eff_start, r_last, off_weekday, joining)
        res['penalty'] = penalty
        res['penalty_detail'] = detail
        return res

    @staticmethod
    def _add_overtime(res, day, hours, is_off):
        """File the day's overtime under the off-day or the regular bucket."""
        if not hours:
            return
        bucket = 'off_ot' if is_off else 'reg_ot'
        res[bucket] += hours
        res[bucket + '_detail'].append((day, hours))

    def _holiday_penalty(self, day_map, holidays, r_start, r_last, off_weekday,
                         joining=None):
        """Extra deduction (in days) for absence adjacent to a public holiday.

        For each consecutive holiday block of length L:
          * short block (< 4 days): absence the day before OR after -> the whole
            block is deducted;
          * long block (>= 4 days): absence one side -> half the block; both
            sides -> the whole block.
        If the adjacent day is the weekly off day, it is not treated as absence.
        """
        block_dates = sorted(d for d in holidays if r_start <= d <= r_last)
        if not block_dates:
            return 0.0, ''

        blocks, group = [], [block_dates[0]]
        for d in block_dates[1:]:
            if (d - group[-1]).days == 1:
                group.append(d)
            else:
                blocks.append(group)
                group = [d]
        blocks.append(group)

        today = fields.Date.context_today(self)

        def _absent_working(d):
            if d.weekday() == off_weekday or d in holidays:
                return False
            # The day after the period is the next sheet's, not this one's -
            # the same reading the Friday takes in _neighbour_attended.
            if d > today or (joining and d < joining) or d > r_last:
                # Not yet happened, or not yet employed: nobody's absence.
                return False
            entry = day_map.get(d)
            if entry is None:
                return True
            if entry['kind'] != 'punch':
                # An approved leave either side of a holiday is not absence.
                return False
            # Turning up for any part of the day answers for it.
            return not entry['ratio']

        penalty, detail = 0.0, []
        for group in blocks:
            length = len(group)
            before = group[0] - timedelta(days=1)
            after = group[-1] + timedelta(days=1)
            ab, aa = _absent_working(before), _absent_working(after)
            if length >= 4:
                add = length if (ab and aa) else (length / 2.0 if (ab or aa) else 0.0)
            else:
                add = length if (ab or aa) else 0.0
            if add:
                penalty += add
                name = holidays.get(group[0]) or "Public Holiday"
                reason = "before & after" if (ab and aa) else ("before" if ab else "after")
                detail.append("%s (%s..%s): -%g day(s) [absence %s]" % (
                    name, group[0], group[-1], add, reason))
        return penalty, "; ".join(detail)


class ConstructionAttendanceSummary(models.Model):
    _name = 'construction.attendance.summary'
    _description = "Attendance Sheet Employee Summary"
    _order = 'sheet_id, employee_id'

    sheet_id = fields.Many2one(
        'construction.attendance.sheet', string="Attendance Sheet",
        required=True, ondelete='cascade', index=True,
    )
    # Who they are, what they punched, what they are paid and how they are
    # classified - one record, and it is what the payroll generation reads.
    employee_id = fields.Many2one(
        'hr.employee', string="Employee", required=True, index=True,
    )
    # The photo, read off hr.employee and never copied: a sheet has hundreds
    # of lines, and a face stored on each would be the largest thing in
    # the table for the least important column. The small size, because it
    # is looked at as an avatar beside the name.
    employee_avatar = fields.Image(
        string="Photo", related='employee_id.image_128', readonly=True)
    day_ids = fields.One2many(
        'construction.attendance.summary.day', 'summary_id', string="Daily Attendance")
    # groups=False on both: a related field inherits the target's groups, and
    # these two sit behind hr.group_hr_user on hr.employee. The copies are on
    # the sheet, which the attendance groups read as they always did.
    employee_code = fields.Char(related='employee_id.registration_number', string="Employee ID", store=True, groups=False)
    attendance_code = fields.Char(related='employee_id.barcode', string="Attendance ID", store=True, groups=False)
    company_id = fields.Many2one(related='sheet_id.company_id', store=True)

    # Days actually attended inside the current period, and the days between
    # the end of the period and the end of its month that may be advanced.
    attended_days = fields.Float(string="Attended Days (Period)")
    period_days = fields.Integer(string="Period Days")
    advance_days = fields.Integer(string="Advance Days")
    total_att_days = fields.Float(
        string="Total Attendance Days",
        compute='_compute_total_att_days', store=True,
    )
    has_advance = fields.Boolean(
        string="Advance Eligible",
        help="False when the employee is NOR approved, on leave or has "
             "submitted their cancellation.",
    )
    attendance_ratio = fields.Float(
        string="Attendance %", compute='_compute_attendance_ratio', store=True,
        help="Attended days over the days of the current period.",
    )
    needs_review = fields.Boolean(
        string="Needs Review", compute='_compute_needs_review', store=True,
        help="Advance-eligible employees who attended less than half of the "
             "period. Their advance must be approved or rejected by hand.",
    )
    advance_state = fields.Selection(
        [
            ('pending', 'Pending'),
            ('approved', 'Approved'),
            ('rejected', 'Rejected'),
        ],
        string="Advance Decision", default='pending', copy=False,
    )
    advance_granted = fields.Boolean(
        string="Advance Granted", compute='_compute_advance_granted', store=True,
    )
    # True while a flagged line still awaits a decision; used to float the
    # undecided lines to the top of the sheet.
    is_pending_review = fields.Boolean(
        string="Pending Review", compute='_compute_is_pending_review', store=True,
    )
    absence_this_month = fields.Float(string="Absence (This Month)")
    penalty_this_month = fields.Float(string="Holiday Penalty (This Month)")
    absence_this_dates = fields.Char(string="Absence Dates (This Month)")
    # Sick days are neutral in the base and paid by their own attachment.
    # ``sick_days_this_month`` is how many days were taken out of the base;
    # ``sick_paid_this_month`` is what the attachment gives back, in days, after
    # the leave type's own ladder.
    sick_days_this_month = fields.Float(string="Sick Days (This Month)")
    sick_paid_this_month = fields.Float(string="Sick Days Paid (This Month)")
    sick_this_detail = fields.Char(string="Sick Leave Detail (This Month)")
    # Last month runs the other way round. Those days were paid whole by the
    # advance and were never deducted, sickness not being absence - so what is
    # settled here is the part the leave type does NOT pay.
    sick_days_last_month = fields.Float(string="Sick Days (Last Month)")
    sick_paid_last_month = fields.Float(string="Sick Days Paid (Last Month)")
    sick_last_detail = fields.Char(string="Sick Leave Detail (Last Month)")
    penalty_this_detail = fields.Char(string="Holiday Penalty Detail (This Month)")
    absence_last_month = fields.Float(string="Absence (Last Month)")
    reg_ot_this_month = fields.Float(string="Reg OT (This Month)")
    reg_ot_last_month = fields.Float(string="Reg OT (Last Month)")
    off_ot_this_month = fields.Float(string="Off OT (This Month)")
    off_ot_last_month = fields.Float(string="Off OT (Last Month)")
    total_ot = fields.Float(string="Total OT", compute='_compute_total_ot', store=True)
    # Holiday penalty days for the last month (fractional allowed).
    penalty_last_month = fields.Float(string="Holiday Penalty (Last Month)")
    # What last month's payslip did about the days this sheet settles. It
    # decides the direction of the settlement: an advance that was paid has
    # its absences deducted; one that was not has its worked days added.
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
             "worked then are added here instead. Paid as attendance: last "
             "month's period reached into these days, nothing to settle. No "
             "payslip found: settled as if the advance had been paid.",
    )
    worked_last_month = fields.Float(
        string="Worked Days (Last Month)",
        help="Days earned in the last-month window, net of the holiday "
             "penalty. Paid here when no advance paid them last month.")
    worked_last_dates = fields.Char(string="Worked Dates (Last Month)")
    # Specific dates behind the last-month figures, used in attachment texts.
    absence_last_dates = fields.Char(string="Absence Dates (Last Month)")
    penalty_last_detail = fields.Char(string="Holiday Penalty Detail (Last Month)")
    reg_ot_last_detail = fields.Char(string="Reg OT Detail (Last Month)")
    off_ot_last_detail = fields.Char(string="Off OT Detail (Last Month)")

    @api.depends('reg_ot_this_month', 'reg_ot_last_month',
                 'off_ot_this_month', 'off_ot_last_month')
    def _compute_total_ot(self):
        for rec in self:
            rec.total_ot = (
                rec.reg_ot_this_month + rec.reg_ot_last_month
                + rec.off_ot_this_month + rec.off_ot_last_month
            )

    @api.depends('attended_days', 'period_days')
    def _compute_attendance_ratio(self):
        for rec in self:
            rec.attendance_ratio = (
                100.0 * rec.attended_days / rec.period_days
                if rec.period_days else 0.0
            )

    @api.depends('has_advance', 'advance_days', 'attendance_ratio')
    def _compute_needs_review(self):
        for rec in self:
            rec.needs_review = bool(
                rec.has_advance and rec.advance_days
                and rec.attendance_ratio < ADVANCE_REVIEW_RATIO)

    @api.depends('needs_review', 'advance_state')
    def _compute_is_pending_review(self):
        for rec in self:
            rec.is_pending_review = rec.needs_review and rec.advance_state == 'pending'

    @api.depends('has_advance', 'needs_review', 'advance_state')
    def _compute_advance_granted(self):
        """Eligible employees keep their advance automatically; the ones under
        the attendance threshold only get it once approved by hand."""
        for rec in self:
            if not rec.has_advance:
                rec.advance_granted = False
            elif rec.needs_review:
                rec.advance_granted = rec.advance_state == 'approved'
            else:
                rec.advance_granted = True

    @api.depends('attended_days', 'advance_days', 'advance_granted')
    def _compute_total_att_days(self):
        for rec in self:
            rec.total_att_days = rec.attended_days + (
                rec.advance_days if rec.advance_granted else 0)

    def action_approve_advance(self):
        self.write({'advance_state': 'approved'})
        return True

    def action_reject_advance(self):
        self.write({'advance_state': 'rejected'})
        return True


class ConstructionAttendanceSummaryDay(models.Model):
    _name = 'construction.attendance.summary.day'
    _description = "Labour Daily Attendance"
    _order = 'summary_id, date'

    summary_id = fields.Many2one(
        'construction.attendance.summary', string="Summary",
        required=True, ondelete='cascade', index=True,
    )
    date = fields.Date(string="Date")
    day_name = fields.Char(string="Day")
    # Present / Partial / Sick Leave / On Leave / Worked during leave / Absent -
    # what priced the day.
    status = fields.Char(string="Status")
    # The share of the day that was earned: one for a full day, a fraction for
    # a short one, zero for an absence.
    day_ratio = fields.Float(string="Day Earned", digits=(3, 2))
    # Hours the punches actually covered, which is what day_ratio is taken
    # from. Zero on a day whose check-in was never closed.
    worked_hours = fields.Float(string="Worked Hours")
    overtime = fields.Float(string="Overtime")
    # The weekly off day or a public holiday: its hours are off-day overtime.
    is_off_day = fields.Boolean(string="Off Day")
    # A check-in the employee never closed. The day earns nothing, and this is
    # what says why - otherwise the absence looks unexplained.
    missing_checkout = fields.Boolean(string="Missing Check-Out")
    project_id = fields.Many2one('project.project', string="Project")
    # Punch times, from hr.attendance - what the day was priced from.
    check_in = fields.Datetime(string="Check In")
    check_out = fields.Datetime(string="Check Out")
    # The same day as the device recorded it, on construction.attendance.line, before
    # hr.attendance closed a missing check-out on its own check-in. Shown
    # beside the pair above so any adjustment is visible rather than implied.
    bio_check_in = fields.Datetime(string="Punch In (Device)")
    bio_check_out = fields.Datetime(string="Punch Out (Device)")
