# -*- coding: utf-8 -*-
import calendar
from datetime import date, timedelta

from markupsafe import Markup

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.tools import html_escape

from .construction_attendance_sheet import ADVANCE_DEDICATION_RATIO, ADVANCE_REVIEW_RATIO
from .utils import MONTH_ABBR

# The leave that counts as an answer for a low attendance. A sick day or two
# does not explain missing half a period; an annual or emergency leave does,
# and those are the two the payroll officer treats as a reason. Matched on the
# type's name because that is the only stable handle hr.leave.type offers.
ADVANCE_EXCUSE_TYPES = ('annual leave', 'emergency leave')

# How far back to look for an annual/emergency leave that does NOT reach into
# the period. Finding one matters: "he has no leave" and "his leave ended
# before the month began" are different answers to the same question, and the
# second is the one that looks like the first if nobody goes looking.
ADVANCE_LEAVE_LOOKBACK = 90


class ConstructionAttendanceSheetGeneration(models.Model):
    """Payroll generation from a labour attendance sheet."""
    _inherit = 'construction.attendance.sheet'

    payslip_ids = fields.One2many('construction.payslip', 'attendance_sheet_id', string="Payslips")
    payslip_count = fields.Integer(compute='_compute_payslip_count')
    batch_ids = fields.One2many(
        'construction.salary.batch', 'attendance_sheet_id', string="Salary Batches")

    @api.depends('payslip_ids')
    def _compute_payslip_count(self):
        for sheet in self:
            sheet.payslip_count = len(sheet.payslip_ids)

    # ------------------------------------------------------------------
    def action_generate_payroll(self):
        for sheet in self:
            if sheet.state != 'approved':
                raise UserError(self.env._(
                    "The attendance sheet must be approved before generating payroll."))
            sheet._warn_unsettled_sick_leave()
            sheet._generate_payroll()
            # The distribution is a pure roll-up of the payslips just created,
            # so there is nothing for the user to trigger by hand.
            sheet.batch_ids.action_compute_project_distribution()
        return True

    def _warn_unsettled_sick_leave(self):
        """A sick leave validated after the sheet was approved is not in the
        summary, so the attachment paying it back would not be made. Said on
        the sheet, before anything is written, with the way out."""
        self.ensure_one()
        differ = self._unsettled_sick_leave()
        if not differ:
            return
        lines = "".join(
            "<li>%s: the sheet holds %g sick day(s), Time Off holds %g</li>"
            % (html_escape(employee.display_name), held, now)
            for employee, (held, now) in differ.items())
        self.message_post(body=Markup(
            "<p><b>%s</b></p><ul>%s</ul><p>%s</p>") % (
            self.env._("%(count)s employee(s) have approved sick leave the sheet "
                       "does not carry.", count=len(differ)),
            Markup(lines),
            self.env._("Their sick day is deducted from the base and nothing pays "
                       "it back. To settle it: delete the payslips if any were "
                       "made, Reset to New, Approve again (the sheet re-reads the "
                       "leaves), then generate payroll.")))

    def _generate_payroll(self):
        self.ensure_one()
        mon = self.last_date.month
        year = self.last_date.year
        month_abbr = MONTH_ABBR[mon]
        days = calendar.monthrange(year, mon)[1]
        # Rate basis for last-month absence deductions = days in that month.
        if self.last_month_end:
            prev_days = calendar.monthrange(self.last_month_end.year, self.last_month_end.month)[1]
        else:
            prev_days = (calendar.monthrange(year - 1, 12)[1] if mon == 1
                         else calendar.monthrange(year, mon - 1)[1])
        period_from = date(year, mon, 1)
        period_to = date(year, mon, days)
        if self.only_employee_ids:
            # A payslip made for one person over a chosen period says that
            # period. The day is still priced at the gross over the days of
            # the month - `days` does not move.
            period_from, period_to = self.start_date, self.last_date

        Payslip = self.env['construction.payslip']
        batch_cache = {}
        # Same day classification the attendance engine used, so the project
        # overtime split matches the payslip's own reg/off hours.
        off_weekday = int(self.company_id.construction_weekly_off_day or '4')
        holidays = self._get_public_holidays(self.start_date, self.last_date)

        for summary in self.summary_ids:
            # sudo: the contract figures are behind hr.group_hr_user and the
            # payroll officer generating the month need not be HR.
            employee = summary.employee_id.sudo()
            if not (employee and summary.total_att_days):
                continue
            # Skip employees who already have a payslip for this sheet.
            if Payslip.search_count([
                ('attendance_sheet_id', '=', self.id),
                ('employee_id', '=', employee.id),
            ]):
                continue

            # Settle the previous-month (second) period natively: last-month
            # absence/penalty deduction and last-month overtime additions.
            # Their notes are written into the payslip note below
            # (_build_payslip_note). Everything else comes from the
            # attachments entered by hand.
            self.env['construction.attachment']._clear_generated_for(
                employee, month_abbr, year)
            self._create_period_attachments(summary, month_abbr, year, prev_days)
            self._create_sick_leave_attachments(summary, month_abbr, year, days)
            # The phone allowance is a Salary Adjustment on the employee now
            # (hr.salary.attachment), paid by the native payslip; nothing is
            # generated for it here, or it would be paid twice.
            is_wps = employee._construction_is_wps()
            batch = self._get_or_create_batch(
                batch_cache, is_wps=is_wps, is_staff=False,
                month_abbr=month_abbr, year=year)

            slip = Payslip.create({
                'employee_id': employee.id,
                'attendance_sheet_id': self.id,
                'summary_id': summary.id,
                'batch_id': batch.id,
                'from_date': period_from,
                'to_date': period_to,
                'days': days,
                'month': month_abbr,
                'year': str(year),
                'is_cash': not is_wps,
                'is_staff': False,
                'designation': employee.job_title,
                'basic_salary': employee.wage,
                'house_allowance': employee.l10n_ae_housing_allowance,
                'transport_allowance': employee.l10n_ae_transportation_allowance,
                'other_allowance': employee.l10n_ae_other_allowances,
                'gross_salary': employee.construction_gross_salary,
                'ot_rate_regular': employee.construction_ot_rate_regular,
                'ot_rate_off': employee.construction_ot_rate_off,
                # total_attendance / overtime_reg / overtime_off are not
                # passed: the payslip adds them up from the daily lines below,
                # so a punch corrected after generation reaches the salary.
                'auto_note': self._build_payslip_note(summary),
                'project_ids': self._build_labour_projects(
                    summary, employee, holidays, off_weekday),
                'day_ids': Payslip._copy_days(summary.day_ids),
            })
            slip._attach_pending_attachments()

    # ------------------------------------------------------------------
    def _create_period_attachments(self, summary, month_abbr, year, prev_days):
        """Create the additions/deductions that settle the previous-month
        portion of the cycle plus the fixed monthly allowance."""
        employee = summary.employee_id.sudo()
        Attachment = self.env['construction.attachment']
        add_type = self.env.ref('construction_payroll.attachment_type_salary_addition')
        ded_type = self.env.ref('construction_payroll.attachment_type_salary_deduction')
        base = {'month': month_abbr, 'year': str(year),
                'employee_id': employee.id, 'auto_generated': True}

        # The two overtime additions share the generic "Salary Addition" type,
        # so they can only be told apart by their description.
        def _exists_named(name):
            return Attachment.search_count([
                ('employee_id', '=', employee.id),
                ('month', '=', month_abbr), ('year', '=', str(year)),
                ('name', '=', name), ('state', '!=', 'paid'),
            ])

        rate = (employee.construction_gross_salary or 0.0) / prev_days if prev_days else 0.0
        if summary.last_month_advance == 'not_granted':
            # Nothing paid those days: what was worked on them is owed, and
            # an absence on them is nobody's debt. Sick days are paid at the
            # share their leave type pays, as this month's are.
            pay_days = summary.worked_last_month + summary.sick_paid_last_month
            if pay_days > 0 and prev_days:
                parts = ["%g day(s) worked: %s" % (
                    summary.worked_last_month, summary.worked_last_dates or '')]
                if summary.sick_paid_last_month:
                    parts.append("sick leave %g day(s) paid: %s" % (
                        summary.sick_paid_last_month, summary.sick_last_detail or ''))
                name = "Addition for last month, no advance was paid - " + "; ".join(parts)
                if not _exists_named(name):
                    Attachment.create(dict(base, name=name, type_id=add_type.id,
                                           value=pay_days * rate))
        else:
            deduct_days = summary.absence_last_month + summary.penalty_last_month
            if deduct_days > 0 and prev_days:
                parts = []
                if summary.absence_last_month:
                    parts.append("%s absence day(s): %s" % (
                        summary.absence_last_month, summary.absence_last_dates or ''))
                if summary.penalty_last_month:
                    parts.append("holiday penalty %g day(s): %s" % (
                        summary.penalty_last_month, summary.penalty_last_detail or ''))
                name = "Deduction for last month - " + "; ".join(parts)
                if not _exists_named(name):
                    Attachment.create(dict(base, name=name, type_id=ded_type.id,
                                           value=deduct_days * rate))

        if summary.reg_ot_last_month > 0:
            name = "Addition for %g OT hour(s) on regular days in last month: %s" % (
                summary.reg_ot_last_month, summary.reg_ot_last_detail or '')
            if not _exists_named(name):
                Attachment.create(dict(base, name=name, type_id=add_type.id,
                                       value=summary.reg_ot_last_month * employee.construction_ot_rate_regular))

        if summary.off_ot_last_month > 0:
            name = "Addition for %g OT hour(s) on off days in last month: %s" % (
                summary.off_ot_last_month, summary.off_ot_last_detail or '')
            if not _exists_named(name):
                Attachment.create(dict(base, name=name, type_id=add_type.id,
                                       value=summary.off_ot_last_month * employee.construction_ot_rate_off))

    def _create_sick_leave_attachments(self, summary, month_abbr, year, days):
        """Pay this period's sick days, at the share each leave type pays.

        The attendance sheet takes a sick day out of the base entirely - it is
        neither attendance nor absence there - and this gives back what is
        owed: full for the first band, half for the second, nothing for the
        third, which is the ladder the Time Off types already carry.

        Last month is deliberately left alone. Those days went out whole with
        the advance, and a sick day is not deducted back: being ill is not
        absence, and the last-month deduction never counted it as such. Nothing
        is added for it either - the advance already paid it.

        One attachment per period, not one per leave. The description names
        every date and its share, so the figure reads back without opening
        anything, and _exists_for_period stops a re-run paying it again.
        """
        employee = summary.employee_id.sudo()
        paid = summary.sick_paid_this_month
        if not (employee and paid > 0 and days):
            return
        Attachment = self.env['construction.attachment']
        sick_type = self.env.ref('construction_payroll.attachment_type_sick_leave')
        if Attachment._exists_for_period(employee, month_abbr, year, sick_type.id):
            return
        Attachment.create({
            'name': self.env._(
                "Sick Leave: %(paid)g day(s) paid of %(taken)g - %(detail)s",
                paid=paid, taken=summary.sick_days_this_month,
                detail=summary.sick_this_detail or ''),
            'employee_id': employee.id,
            'type_id': sick_type.id,
            'month': month_abbr, 'year': str(year),
            'value': paid * ((employee.construction_gross_salary or 0.0) / days),
            'state': 'new',
            'auto_generated': True,
        })

    def _build_payslip_note(self, summary):
        """Human-readable summary of every deduction applied to the payslip.

        Current-month deductions reduce the base (total_att_days); last-month
        deductions are settled through the salary attachments. Both are listed
        here for transparency, including the public holiday name/reason."""
        lines = []
        if summary.absence_this_month:
            lines.append("Absence this month: %s day(s)%s" % (
                summary.absence_this_month,
                (" - " + summary.absence_this_dates) if summary.absence_this_dates else ''))
        if summary.penalty_this_month:
            lines.append("Public holiday penalty this month: %g day(s) - %s" % (
                summary.penalty_this_month, summary.penalty_this_detail or ''))
        if summary.sick_days_this_month:
            lines.append(
                "Sick leave this month: %g day(s), %g paid via attachment%s" % (
                    summary.sick_days_this_month, summary.sick_paid_this_month,
                    (" - " + summary.sick_this_detail)
                    if summary.sick_this_detail else ''))
        if summary.sick_days_last_month:
            lines.append(
                "Sick leave last month: %g day(s), not deducted%s" % (
                    summary.sick_days_last_month,
                    (" - " + summary.sick_last_detail)
                    if summary.sick_last_detail else ''))
        state = summary.last_month_advance
        if state == 'not_granted':
            lines.append(
                "Last month advance was not paid: nothing deducted for last month"
                "%s; %g day(s) worked then are added via attachment%s" % (
                    (" (%s absence day(s) not charged)" % summary.absence_last_month)
                    if summary.absence_last_month else '',
                    summary.worked_last_month,
                    (" - " + summary.worked_last_dates) if summary.worked_last_dates else ''))
        elif state == 'covered':
            lines.append("Last month period was paid in full by its own payslip: "
                         "nothing to settle for last month.")
        else:
            if state == 'unknown' and (summary.absence_last_month or summary.penalty_last_month):
                lines.append("No payslip found for last month: settled as if the "
                             "advance had been paid.")
            if summary.absence_last_month:
                lines.append("Absence last month (via attachment): %s day(s)%s" % (
                    summary.absence_last_month,
                    (" - " + summary.absence_last_dates) if summary.absence_last_dates else ''))
            if summary.penalty_last_month:
                lines.append("Public holiday penalty last month (via attachment): %g day(s) - %s" % (
                    summary.penalty_last_month, summary.penalty_last_detail or ''))
        if not summary.has_advance:
            lines.append("No advance paid (NOR approved / on leave / "
                         "submitted cancellation).")
        elif not summary.advance_days:
            # Either the period runs to the end of its month, or the advance
            # was withheld from the whole sheet. Saying it plainly beats
            # "Advance paid: 0 day(s)".
            lines.append("No advance days paid for this period.")
        elif summary.advance_granted:
            lines.append("Advance paid: %s day(s) after the period." %
                         summary.advance_days)
        else:
            lines.append(
                "Advance rejected: %s day(s) withheld (attendance %.0f%% of "
                "the period, below %g%%)." % (
                    summary.advance_days, summary.attendance_ratio,
                    ADVANCE_REVIEW_RATIO))
            lines.extend(self._advance_leave_lines(summary))
        if not lines:
            return False
        # Rendered in light purple (see auto_note) so it reads as machine-written.
        body = "<br/>".join(html_escape(line) for line in lines)
        return '<div style="color:#8f5fa6;">%s</div>' % body

    def _advance_leave_lines(self, summary):
        """What the leave records say about a withheld advance.

        The withholding is arithmetic - attended under half the period - and
        says nothing about why. The why is in Time Off, and it is listed here
        in ANY state, because a refused or still-draft request is exactly what
        leaves somebody away with nothing in the attendance to show for it. The
        record missing from the attendance is the record that explains it.

        Three answers are possible and they are not the same answer:

            an annual/emergency leave overlapping the period   explained
            one that ended before the period began             NOT explained,
                and this is the one that reads as "no leave" unless it is
                looked for on purpose
            none at all                                        unexplained

        Returns a list of note lines; never raises.
        """
        employee = summary.employee_id
        if not employee:
            return ["No employee on the summary, so no leave could be checked."]
        # hr_holidays is a soft dependency here as everywhere else in this
        # module - the manifest does not declare it, so the model may simply
        # not be in the registry.
        Leave = self.env.get('hr.leave')
        if Leave is None:
            return ["Time Off is not installed, so no leave could be checked."]
        Leave = Leave.sudo().with_context(active_test=False)
        window_start = self.start_date - timedelta(days=ADVANCE_LEAVE_LOOKBACK)
        leaves = Leave.search([
            ('employee_id', '=', employee.id),
            ('request_date_from', '<=', self.last_date),
            ('request_date_to', '>=', window_start),
        ], order='request_date_from')

        def is_excuse(leave):
            name = (leave.holiday_status_id.name or '').strip().lower()
            return name in ADVANCE_EXCUSE_TYPES

        def covered(leave):
            first = max(leave.request_date_from, self.start_date)
            last = min(leave.request_date_to, self.last_date)
            return (last - first).days + 1 if last >= first else 0

        overlapping = [lv for lv in leaves if is_excuse(lv) and covered(lv)]
        earlier = [lv for lv in leaves if is_excuse(lv) and not covered(lv)]
        other = [lv for lv in leaves if not is_excuse(lv) and covered(lv)]

        lines = []
        for leave in overlapping:
            lines.append(
                "  Leave on record: %s %s to %s (%s) - covers %s day(s) of "
                "this period." % (
                    leave.holiday_status_id.name, leave.request_date_from,
                    leave.request_date_to, leave.state, covered(leave)))
        if not overlapping:
            for leave in earlier:
                lines.append(
                    "  %s %s to %s (%s) ended BEFORE this period and does not "
                    "cover any of it." % (
                        leave.holiday_status_id.name, leave.request_date_from,
                        leave.request_date_to, leave.state))
            if not earlier:
                lines.append(
                    "  No annual or emergency leave on record - the absence is "
                    "not explained by Time Off.")
        if other:
            lines.append("  Other leave in the period: %s." % ", ".join(
                "%s %s to %s (%s)" % (
                    lv.holiday_status_id.name, lv.request_date_from,
                    lv.request_date_to, lv.state) for lv in other))
        return lines

    @api.model
    def _allocate_advance_days(self, worked_days, advance_days, period_days):
        """Spread the advanced days over the projects the employee worked on.

        Those days carry no daily record yet, so they cannot be observed; they
        are attributed from where the employee actually was. Someone who spent
        more than 60% of the period on one project counts as dedicated to it
        and carries the whole advance there. Otherwise the days follow the same
        proportions as the days that were worked.

        Returns {project_id: days}; empty when there is nothing to attribute.
        """
        total_worked = sum(worked_days.values())
        if not (advance_days and total_worked):
            return {}
        if period_days:
            pid, days = max(worked_days.items(), key=lambda kv: kv[1])
            if 100.0 * days / period_days > ADVANCE_DEDICATION_RATIO:
                return {pid: float(advance_days)}
        return {
            pid: advance_days * days / total_worked
            for pid, days in worked_days.items()
        }

    def _build_labour_projects(self, summary, employee, holidays, off_weekday):
        """Aggregate the earned days and the overtime per project from the
        sheet's own daily lines. A day contributes the share of itself it
        earned, so a half day worked charges the project half a day. Overtime
        is split the same way the attendance engine splits it: hours worked on
        a public holiday or on the weekly off day are off-day overtime.
        """
        totals = {}
        for day in summary.day_ids:
            if not day.date or not (self.start_date <= day.date <= self.last_date):
                continue
            if not (day.project_id and day.day_ratio):
                continue
            bucket = totals.setdefault(
                day.project_id.id, {'att': 0.0, 'reg': 0.0, 'off': 0.0})
            bucket['att'] += day.day_ratio
            if day.overtime and employee.construction_overtime_eligible:
                is_off = day.date in holidays or day.date.weekday() == off_weekday
                bucket['off' if is_off else 'reg'] += day.overtime
        allocated = self._allocate_advance_days(
            {pid: v['att'] for pid, v in totals.items()},
            summary.advance_days if summary.advance_granted else 0,
            summary.period_days,
        )
        return [
            (0, 0, {
                'project_id': pid,
                'total_attendance': vals['att'],
                'total_overtime_reg': vals['reg'],
                'total_overtime_off': vals['off'],
                'allocated_attendance': vals['att'] + allocated.get(pid, 0.0),
            })
            for pid, vals in totals.items()
        ]

    def _get_or_create_batch(self, cache, is_wps, is_staff, month_abbr, year):
        company = self.company_id
        code = company.construction_company_code or (company.name or '')[:3].upper()
        abbr = company.construction_company_abbr or code
        pay_type = 'WPS' if is_wps else 'CASH'
        staff_tag = 'Staff-' if is_staff else ''
        name = "%s%s/%s/%s/%s%s/Salary Batch" % (
            year, month_abbr, code, pay_type, staff_tag, abbr)

        if name in cache:
            return cache[name]
        Batch = self.env['construction.salary.batch']
        batch = Batch.search([('name', '=', name), ('company_id', '=', company.id)], limit=1)
        if not batch:
            batch = Batch.create({
                'name': name,
                'company_id': company.id,
                'attendance_sheet_id': self.id,
                'submitted_by': self.submitted_by.id or self.env.uid,
                'batch_type': 'wps' if is_wps else 'cash',
                'is_staff': is_staff,
            })
        cache[name] = batch
        return batch

    def action_view_payslips(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': self.env._("Payslips"),
            'res_model': 'construction.payslip',
            'view_mode': 'list,form',
            'domain': [('attendance_sheet_id', '=', self.id)],
            'context': {'default_attendance_sheet_id': self.id},
        }


class ConstructionStaffAttendanceGeneration(models.Model):
    """Staff payslip generation from a staff attendance sheet."""
    _inherit = 'construction.staff.attendance'

    payslip_ids = fields.One2many(
        'construction.payslip', 'staff_attendance_id', string="Payslips")
    payslip_count = fields.Integer(compute='_compute_staff_payslip_count')

    @api.depends('payslip_ids')
    def _compute_staff_payslip_count(self):
        for rec in self:
            rec.payslip_count = len(rec.payslip_ids)

    def action_generate_payslips(self):
        for rec in self:
            if rec.state != 'approved':
                raise UserError(self.env._(
                    "Approve the staff attendance before generating payslips."))
            rec._generate_staff_payslips()
            rec.payslip_ids.batch_id.action_compute_project_distribution()
        return True

    def _generate_staff_payslips(self):
        self.ensure_one()
        mon = int(self.month)
        year = self.year
        month_abbr = MONTH_ABBR[mon]
        days = self._days_in_month()
        period_from = date(year, mon, 1)
        period_to = date(year, mon, days)
        if self.only_employee_ids and self.start_date and self.last_date:
            # One person over a chosen period: the payslip says that period.
            # `days`, what a day is priced on, stays the month's.
            period_from, period_to = self.start_date, self.last_date

        Payslip = self.env['construction.payslip']
        batch_cache = {}

        for line in self.line_ids:
            # sudo: see the labour generation above.
            employee = line.employee_id.sudo()
            if Payslip.search_count([
                ('staff_attendance_id', '=', self.id),
                ('employee_id', '=', employee.id),
            ]):
                continue

            # The phone allowance is a Salary Adjustment on the employee
            # (hr.salary.attachment), paid by the native payslip - not here.
            is_wps = employee._construction_is_wps()
            batch = self._get_or_create_staff_batch(
                batch_cache, is_wps=is_wps, month_abbr=month_abbr, year=year)

            slip = Payslip.create({
                'employee_id': employee.id,
                'staff_attendance_id': self.id,
                'batch_id': batch.id,
                'from_date': period_from,
                'to_date': period_to,
                'days': days,
                'month': month_abbr,
                'year': str(year),
                'is_cash': not is_wps,
                'is_staff': True,
                'designation': employee.job_title,
                'basic_salary': line.basic_salary,
                'house_allowance': line.house_allowance,
                'transport_allowance': line.transport_allowance,
                'other_allowance': line.other_allowance,
                'gross_salary': line.gross_salary,
                'total_attendance': line.total_attended_days,
                # Staff carry no daily project record: their days are split
                # across projects by their agreed percentages. The staff sheet
                # tracks no overtime, so there are no hours to spread.
                # The staff sheet has no advance to attribute, so the allocated
                # view of a staff payslip is the same as the worked one.
                'project_ids': [
                    (0, 0, {
                        'project_id': d.project_id.id,
                        'total_attendance': (d.percentage / 100.0) * line.total_attended_days,
                        'allocated_attendance': (d.percentage / 100.0) * line.total_attended_days,
                    })
                    for d in line.distribution_ids
                ],
                'day_ids': Payslip._copy_days(line.day_ids),
            })
            slip._attach_pending_attachments()
            # Native last-month absence deduction, kept in sync with the line's
            # last_month_absence (see construction.staff.attendance.line).
            line._sync_last_month_absence_deduction()

    def _get_or_create_staff_batch(self, cache, is_wps, month_abbr, year):
        company = self.company_id
        code = company.construction_company_code or (company.name or '')[:3].upper()
        abbr = company.construction_company_abbr or code
        pay_type = 'WPS' if is_wps else 'CASH'
        name = "%s%s/%s/%s/Staff-%s/Salary Batch" % (
            year, month_abbr, code, pay_type, abbr)
        if name in cache:
            return cache[name]
        Batch = self.env['construction.salary.batch']
        batch = Batch.search([('name', '=', name), ('company_id', '=', company.id)], limit=1)
        if not batch:
            batch = Batch.create({
                'name': name,
                'company_id': company.id,
                'batch_type': 'wps' if is_wps else 'cash',
                'is_staff': True,
            })
        cache[name] = batch
        return batch

    def action_view_payslips(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': self.env._("Payslips"),
            'res_model': 'construction.payslip',
            'view_mode': 'list,form',
            'domain': [('staff_attendance_id', '=', self.id)],
            'context': {'default_staff_attendance_id': self.id},
        }
