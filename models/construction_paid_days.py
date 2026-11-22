# -*- coding: utf-8 -*-
"""The paid days of one employee over one period, as the attendance sheet counts them.

Odoo payroll prices a day; this decides which days are paid. Salary rules that
work it out for themselves - hours punched over hours scheduled, with a floor
and a divisor patched on afterwards - disagree with the attendance sheet, by a
Friday here and a half day there. Two engines counting the same punches will
always drift, because every rule a company has about a day (a check-in
nobody closed, the weekly off day between two absences, the block of public
holidays somebody skipped around, the days after the period paid in advance)
lives in one of them and has to be copied into the other.

So there is one engine. The rule asks this method, and this method asks the
attendance sheet's own code - the same methods that generate a sheet - on a
sheet that exists only for the length of the call. Nothing is transcribed and
nothing can drift: when the company changes what a day means, it changes it in
construction.attendance.sheet and the payslip follows.

WHAT IT RETURNS

    period_days   calendar days from the later of the period start and the
                  contract start, to the period end
    attended      days earned inside the period, less the holiday penalty
    advance_days  the days after the period to the end of its month, paid in
                  advance - zero when the period already runs to month end
    advance_granted  whether they are actually paid: eligible (active, not
                  leaving, not on leave today) and attendance at least half
                  the period. Under half, the sheet holds the advance for a
                  person to approve; a payslip has no person in the loop, so
                  it is withheld. ADVANCE_DAYS as a payslip input can grant it
                  by hand.
    total_days    attended + the advance if granted - what construction.payslip calls
                  total_attendance
    days_in_month days in the month of the period end
    ratio         min(total_days / days_in_month, 1) - the share of the
                  contract gross this period pays. Exactly construction.payslip's
                  total_salary: the gross when the days reach the month, a
                  thirty-first of it per day otherwise.

THE TWO SWITCHES A SHEET HAS AND A PAYSLIP DOES NOT

A generated sheet carries "No Check-Out Is Absence" (default on) and "No
Advance Days" (default off), set by a person for that sheet. There is no sheet
here, so the defaults apply; a company that wants them otherwise sets
construction_payroll.missing_checkout_is_absent / construction_payroll.no_advance_days in
System Parameters, which is the same decision made once instead of monthly.
"""
import calendar
from datetime import timedelta

from odoo import api, models

# The attendance percentage under which the sheet holds the advance for
# review. Read from the sheet module so the two cannot disagree.
from .construction_attendance_sheet import ADVANCE_REVIEW_RATIO


class HrEmployee(models.Model):
    _inherit = 'hr.employee'

    @api.model
    def _construction_sheet_switch(self, name, default):
        """A sheet switch, from System Parameters, as a bool."""
        # get_param answers False, not None, for a key that is not there -
        # and False read as "the switch is off" turned "No Check-Out Is
        # Absence" off in the payslip while the sheet had it on. The
        # differential test caught it: the method paid a day the sheet did
        # not.
        value = self.env['ir.config_parameter'].sudo().get_param(
            'construction_payroll.%s' % name, default=None)
        if value in (None, False, ''):
            return default
        return str(value).strip().lower() in ('1', 'true', 'yes', 'on')

    def _construction_paid_days(self, date_from, date_to):
        """The paid days of this employee over [date_from, date_to]. See the module docstring."""
        self.ensure_one()
        employee = self.sudo()
        Sheet = self.env['construction.attendance.sheet'].sudo()
        empty = {
            'period_days': 0, 'attended': 0.0, 'absence': 0.0, 'penalty': 0.0,
            'sick': 0.0, 'neutral': 0, 'advance_days': 0,
            'advance_granted': False, 'total_days': 0.0,
            'days_in_month': 0, 'ratio': 0.0,
            'reg_ot': 0.0, 'off_ot': 0.0, 'absent_dates': [],
        }
        if not (date_from and date_to) or date_from > date_to:
            return empty

        company = employee.company_id or self.env.company
        # A sheet that lives for the length of this call. new() gives a record
        # with the fields set and nothing written, and every engine method
        # reads only these fields off self.
        sheet = Sheet.new({
            'company_id': company.id,
            'start_date': date_from,
            'last_date': date_to,
            'missing_checkout_is_absent': self._construction_sheet_switch(
                'missing_checkout_is_absent', True),
            'no_advance_days': self._construction_sheet_switch('no_advance_days', False),
        })
        off_weekday = int(company.construction_weekly_off_day or '4')
        days_in_month = calendar.monthrange(date_to.year, date_to.month)[1]
        month_end = date_to.replace(day=days_in_month)

        # Widened by a day either side: the off-day bridge and the holiday
        # penalty look at the neighbours of the period's first and last day.
        span_start = date_from - timedelta(days=1)
        span_last = max(date_to, month_end) + timedelta(days=1)

        holidays = sheet._get_public_holidays(span_start, span_last)
        punches = sheet._hr_attendance_map(employee, span_start, span_last).get(
            employee.id) or {}
        leaves = sheet._hr_leave_map(employee, span_start, span_last).get(
            employee.id) or {}
        # The biometric map feeds the device overtime: not a paid day, but
        # the final settlement prices the last month's overtime from it.
        bio = sheet._biometric_map(employee, span_start, span_last).get(employee.id) or {}
        day_map = sheet._build_day_map(punches, leaves, bio, holidays, off_weekday)
        profile = sheet._employee_profile(employee)

        joining = profile['joining_date']
        if joining and joining > month_end:
            return empty

        figures = sheet._compute_range(
            profile, day_map, date_from, date_to, holidays, off_weekday)
        attended = max(figures['attended'] - figures['penalty'], 0.0)
        period_days = figures['period_days']

        advance_days = 0 if sheet.no_advance_days else sheet._advance_days(
            profile, month_end)
        attendance_ratio = (100.0 * attended / period_days) if period_days else 0.0
        # Word for word the summary line's _compute_advance_granted: eligible
        # people keep the advance, and only those under the review threshold
        # need a person - who is not here, so for them it is withheld.
        needs_review = bool(profile['has_advance'] and advance_days
                            and attendance_ratio < ADVANCE_REVIEW_RATIO)
        advance_granted = bool(profile['has_advance'] and not needs_review)
        total_days = attended + (advance_days if advance_granted else 0)

        return {
            'period_days': period_days,
            'attended': attended,
            'absence': figures['absence'],
            'penalty': figures['penalty'],
            'sick': figures['sick'],
            'neutral': figures['neutral'],
            'advance_days': advance_days,
            'advance_granted': advance_granted,
            'total_days': total_days,
            'days_in_month': days_in_month,
            'ratio': min(total_days / days_in_month, 1.0) if days_in_month else 0.0,
            # overtime hours as the sheet classifies them, zero for an
            # employee who is not paid overtime
            'reg_ot': figures['reg_ot'],
            'off_ot': figures['off_ot'],
            'absent_dates': figures['absent_dates'],
        }
