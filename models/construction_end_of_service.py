# -*- coding: utf-8 -*-
import calendar
import logging
from datetime import timedelta

from dateutil.relativedelta import relativedelta
from markupsafe import Markup

from odoo import api, fields, models
from odoo.exceptions import UserError


from .utils import MONTH_ABBR

# Federal Decree-Law 33 of 2021, article 51: the end of service gratuity of a
# foreign worker who completed a year or more of continuous service.
GRATUITY_DAYS_FIRST_YEARS = 21      # days of basic wage per year, years 1-5
GRATUITY_DAYS_LATER_YEARS = 30      # days of basic wage per year after that
GRATUITY_FIRST_BAND_YEARS = 5
GRATUITY_CAP_YEARS = 2              # the whole gratuity may not exceed two years' wage
DAYS_IN_MONTH = 30                  # the wage divisor the law and the office both use

_logger = logging.getLogger(__name__)


class ConstructionEndOfService(models.Model):
    _name = 'construction.end.of.service'
    _description = "End of Service"
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'id desc'

    # ------------------------------------------------------------------
    # Identity / header
    # ------------------------------------------------------------------
    name = fields.Char(string="Description", compute='_compute_name', store=True, readonly=False)
    employee_id = fields.Many2one('hr.employee', string="Employee", required=True, tracking=True)
    company_id = fields.Many2one(related='employee_id.company_id', store=True, index=True)
    currency_id = fields.Many2one(related='employee_id.currency_id', store=True)
    # groups=False: the contract start is behind hr.group_hr_manager, and this
    # copy on the settlement is the payroll team's to read - see
    # construction.advance.employee_code.
    joining_date = fields.Date(related='employee_id.contract_date_start', string="Joining Date", store=True, groups=False)

    total_amount = fields.Monetary(string="Total Amount", tracking=True)
    profession = fields.Char(string="Profession")
    nationality = fields.Char(string="Nationality")
    date_of_request = fields.Date(string="Date of Request")
    eid_visa_expiry = fields.Date(string="E.I.D/Visa Expiry date")
    last_day_of_duty = fields.Date(string="Last Day of Duty", tracking=True)
    received_payslip = fields.Boolean(string="Received Final Payslip")
    hr_review = fields.Text(string="HR Review & Note")
    nor_document = fields.Binary(string="Signed NOR", attachment=True)
    nor_document_name = fields.Char(string="NOR Filename")

    state = fields.Selection(
        [
            ('new', 'New'),
            ('checked', 'Checked & Verified'),
            ('in_payment', 'In Payments'),
            ('paid', 'Paid'),
        ],
        string="Status", default='new', tracking=True, index=True,
    )

    # ------------------------------------------------------------------
    # E.O.S & Gratuity
    # ------------------------------------------------------------------
    gratuity_calculation_method = fields.Selection(
        [('basic', "Basic Salary"), ('total', "Gross Salary")],
        string="Calculation Method", default='basic',
        help="The law computes the gratuity on the basic wage.")
    gratuity_basic_salary = fields.Monetary(string="Basic Salary (Gratuity)")
    gratuity_gross_salary = fields.Monetary(string="Total Gross Salary (Gratuity)")
    gratuity_adjust = fields.Boolean(string="Adjust Amount (Gratuity)")
    gratuity_amount = fields.Monetary(
        string="Total E.O.S & Gratuity Payable", tracking=True)
    years_of_service = fields.Float(string="Years of Service", compute='_compute_service', store=True)
    gratuity_years = fields.Integer(string="Years")
    gratuity_months = fields.Integer(string="Months")
    gratuity_days = fields.Integer(string="Days")
    gratuity_for_years = fields.Monetary(string="Gratuity for Years")
    gratuity_for_months = fields.Monetary(string="Gratuity for Months")
    gratuity_for_days = fields.Monetary(string="Gratuity for Days")
    gratuity_formula_html = fields.Html(string="Gratuity Details", sanitize=False)

    # ------------------------------------------------------------------
    # Leave allowances
    # ------------------------------------------------------------------
    leave_calculation_method = fields.Selection(
        [('basic', "Basic Salary"), ('total', "Gross Salary")],
        string="Calculation Method (Leave)", default='basic',
        help="The law pays unused leave on the basic wage (art. 29). The "
             "office has paid office staff on the gross; choose it here.")
    leave_days_balance = fields.Float(
        string="Annual Leave Balance (days)", digits=(16, 2),
        help="Approved allocations less approved leaves, at the last day of "
             "duty, from Time Off.")
    leave_daily_rate = fields.Monetary(string="Leave Day Rate")
    leave_basic_salary = fields.Monetary(string="Basic Salary (Leave)")
    leave_adjust = fields.Boolean(string="Adjust Amount (Leave)")
    leave_amount_to_pay = fields.Monetary(string="Amount to Pay (Leave)")
    leave_salaries_eligible = fields.Integer(string="Total leave salaries eligible for")
    leave_salaries_balance = fields.Monetary(string="Total Leave Salaries over Balance months")
    leave_total_payable = fields.Monetary(string="Total Payable Amount for Leave Allowances")

    # ------------------------------------------------------------------
    # Final monthly salary (contract snapshot + attendance + salary)
    # ------------------------------------------------------------------
    fm_basic_salary = fields.Monetary(string="Basic Salary")
    fm_house_allowance = fields.Monetary(string="House Allowance")
    fm_travelling_allowance = fields.Monetary(string="Travelling Allowance")
    fm_other_allowance = fields.Monetary(string="Other Allowances")
    fm_gross_salary = fields.Monetary(string="Total Gross Salary")
    fm_rate_per_day = fields.Monetary(string="Rate per Day")
    fm_ot_rate_regular = fields.Float(string="OT rate on regular days")
    fm_ot_rate_off = fields.Float(string="OT rate on off/holidays")
    fm_total_days = fields.Integer(string="Total Days in this Month")
    total_absence_days = fields.Integer(string="Total Absence Days")
    total_ot_regular = fields.Float(string="Total OT hrs (Regular Days)")
    total_ot_off = fields.Float(string="Total OT hrs (Off Days)")
    fm_total_salary_month = fields.Monetary(string="Total Salary of this Month")
    fm_overtime_salary = fields.Monetary(string="Total Overtime Salary")
    fm_salary_adjustment = fields.Monetary(string="Salary Adjustment")
    fm_type_of_adjustment = fields.Char(string="Type of Adjustment")
    fm_net_salary = fields.Monetary(string="Net Salary of Last Month")
    salary_html = fields.Html(string="Salary Details", sanitize=False)

    # ------------------------------------------------------------------
    # Settlement total
    # ------------------------------------------------------------------
    other_adjustments = fields.Monetary(
        string="Other Adjustments",
        help="HR's figure, never computed. The list beside it says what the "
             "system sees as outstanding, for information.")
    adjustments_html = fields.Html(string="Outstanding (for information)", sanitize=False)
    service_excluded_days = fields.Integer(
        string="Days Excluded from Service",
        help="Unpaid absence days the employer may leave out of the service "
             "period (Decree-Law 33/2021, art. 51). Zero unless HR says so.")
    computed_on = fields.Datetime(string="Computed On", readonly=True)
    total_settlement = fields.Monetary(
        string="Total Settlement", compute='_compute_total', store=True,
    )

    @api.depends('employee_id')
    def _compute_name(self):
        for rec in self:
            rec.name = self.env._("End of Service - %s") % (rec.employee_id.name or '')

    @api.depends('joining_date', 'last_day_of_duty')
    def _compute_service(self):
        for rec in self:
            if rec.joining_date and rec.last_day_of_duty and rec.last_day_of_duty >= rec.joining_date:
                rec.years_of_service = (rec.last_day_of_duty - rec.joining_date).days / 365.0
            else:
                rec.years_of_service = 0.0

    @api.depends('gratuity_amount', 'leave_total_payable', 'fm_net_salary',
                 'other_adjustments')
    def _compute_total(self):
        for rec in self:
            rec.total_settlement = (
                rec.gratuity_amount + rec.leave_total_payable
                + rec.fm_net_salary + rec.other_adjustments)

    # ------------------------------------------------------------------
    # The settlement computes itself, in two steps a person can take apart
    #
    #   Refresh from Employee  reads the INPUTS off hr.employee, the punches,
    #                          Time Off and what is owed: contract figures,
    #                          service, absence and overtime of the last
    #                          month, the leave balance. Done on creation.
    #   Compute                derives every AMOUNT from the inputs as they
    #                          stand on the form. Edit an input - the absence
    #                          days, the years, the leave balance - press
    #                          Compute, and the figures follow the edit
    #                          instead of the edit being read over.
    #
    # Other Adjustments is HR's figure and is never written. The Adjust
    # toggles are kept as data and ignored.
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for rec in records:
            if rec.employee_id:
                rec.action_refresh()
        return records

    def _only_new(self, what):
        for rec in self:
            if rec.state != 'new':
                raise UserError(self.env._(
                    "%(name)s is %(state)s; only a new settlement can be %(what)s.",
                    name=rec.display_name, what=what,
                    state=dict(rec._fields['state'].selection)[rec.state]))

    def action_refresh(self):
        """The inputs, from the employee as they are today; then Compute."""
        self._only_new(self.env._("refreshed"))
        for rec in self:
            employee = rec.employee_id.sudo()
            last_day = rec.last_day_of_duty or employee.departure_date \
                or rec.date_of_request or fields.Date.context_today(rec)
            vals = {
                'last_day_of_duty': last_day,
                'profession': rec.profession or employee.job_title or False,
                'nationality': rec.nationality or employee.country_id.name or False,
                'eid_visa_expiry': rec.eid_visa_expiry or employee.visa_expire or False,
                'gratuity_basic_salary': employee.wage or 0.0,
                'gratuity_gross_salary': employee.construction_gross_salary or 0.0,
                'leave_basic_salary': employee.wage or 0.0,
                'fm_basic_salary': employee.wage or 0.0,
                'fm_house_allowance': employee.l10n_ae_housing_allowance or 0.0,
                'fm_travelling_allowance': employee.l10n_ae_transportation_allowance or 0.0,
                'fm_other_allowance': employee.l10n_ae_other_allowances or 0.0,
                'fm_gross_salary': employee.construction_gross_salary or 0.0,
                'fm_ot_rate_regular': employee.construction_ot_rate_regular or 0.0,
                'fm_ot_rate_off': employee.construction_ot_rate_off or 0.0,
                'adjustments_html': rec._owed_html(employee, last_day),
            }
            vals.update(rec._service_span(employee, last_day))
            vals.update(rec._leave_inputs(employee, last_day))
            vals.update(rec._final_month_inputs(employee, last_day))
            rec.write(vals)
            rec.action_compute()
        return True

    def action_compute(self):
        """The amounts, from the inputs on the form as they stand."""
        self._only_new(self.env._("computed"))
        for rec in self:
            # Every amount, every time: Compute means "from the inputs as
            # they stand, all of it". A person who wants another figure
            # types it into the amount after computing.
            vals = {'computed_on': fields.Datetime.now()}
            vals.update(rec._gratuity_amounts())
            vals.update(rec._leave_amounts())
            vals.update(rec._final_month_amounts())
            rec.write(vals)
        return True

    # --- inputs ---------------------------------------------------------
    def _service_span(self, employee, last_day):
        """Years, months and days from the contract start to the last day of
        duty, as the office has always counted them (the last day itself
        not added), less the days HR excluded."""
        joining = employee.contract_date_start
        if not joining or last_day < joining:
            return {'gratuity_years': 0, 'gratuity_months': 0, 'gratuity_days': 0}
        end = last_day - timedelta(days=self.service_excluded_days or 0)
        delta = relativedelta(end, joining)
        return {'gratuity_years': delta.years, 'gratuity_months': delta.months,
                'gratuity_days': delta.days}

    def _leave_inputs(self, employee, last_day):
        """The annual leave still owed at the last day of duty, from Time Off:
        approved allocations less approved leaves - the balance the app shows."""
        LeaveType = self.env['hr.leave.type'].sudo()
        types = LeaveType.with_context(active_test=False).search([('name', 'ilike', 'annual')])
        balance = 0.0
        if types:
            data = types.get_allocation_data(employee, last_day).get(employee, [])
            balance = sum(entry[1].get('remaining_leaves', 0.0) for entry in data)
        return {'leave_days_balance': max(round(balance, 2), 0.0)}

    def _final_month_inputs(self, employee, last_day):
        """The last month as the attendance engine reads it: absence days and
        overtime hours from the first of the month to the last day of duty."""
        figures = employee._construction_paid_days(last_day.replace(day=1), last_day)
        days_in_month = figures['days_in_month'] or calendar.monthrange(last_day.year, last_day.month)[1]
        return {
            'fm_total_days': days_in_month,
            'total_absence_days': int(round(figures['absence'] + figures['penalty'])),
            'total_ot_regular': figures['reg_ot'],
            'total_ot_off': figures['off_ot'],
        }

    # --- amounts --------------------------------------------------------
    def _gratuity_amounts(self):
        """Article 51, the way the office has always written it out.

        21 days of wage for each of the first five years, 30 days for each
        year after; a month is a twelfth of that year's rate and a day a
        365th of it. Nothing under a year of service, nothing for a UAE
        national (the pension law covers them), and never more than two
        years' wage in all.
        """
        self.ensure_one()
        years, months, days = self.gratuity_years, self.gratuity_months, self.gratuity_days
        method = self.gratuity_calculation_method or 'basic'
        wage = self.gratuity_gross_salary if method == 'total' else self.gratuity_basic_salary
        wage = wage or 0.0
        lines = []
        for_years = for_months = for_days = 0.0
        reason = None
        if (self.employee_id.country_id.code or '').upper() == 'AE':
            reason = self.env._("UAE national: the pension scheme applies, no gratuity.")
        elif years < 1:
            reason = self.env._("Less than one year of service: no gratuity (art. 51).")
        else:
            rate_first = wage * GRATUITY_DAYS_FIRST_YEARS / DAYS_IN_MONTH
            rate_later = wage * GRATUITY_DAYS_LATER_YEARS / DAYS_IN_MONTH
            first = min(years, GRATUITY_FIRST_BAND_YEARS)
            later = years - first
            for_years = first * rate_first + later * rate_later
            fraction_rate = rate_later if years >= GRATUITY_FIRST_BAND_YEARS else rate_first
            for_months = months * fraction_rate / 12.0
            for_days = days * fraction_rate / 365.0
            lines.append("%s x %s days x %s / 30 = %s" % (
                first, GRATUITY_DAYS_FIRST_YEARS, wage, round(first * rate_first, 2)))
            if later:
                lines.append("%s x %s days x %s / 30 = %s" % (
                    later, GRATUITY_DAYS_LATER_YEARS, wage, round(later * rate_later, 2)))
            lines.append("%s month(s) x %s / 12 = %s" % (months, round(fraction_rate, 2), round(for_months, 2)))
            lines.append("%s day(s) x %s / 365 = %s" % (days, round(fraction_rate, 2), round(for_days, 2)))
        total = round(for_years + for_months + for_days, 2)
        cap = round(GRATUITY_CAP_YEARS * 12 * wage, 2)
        if total > cap:
            lines.append(self.env._("Capped at two years' wage: %s") % cap)
            total = cap
        if reason:
            lines.append(reason)
        html = Markup("<p><b>%s</b> %s / %s / %s (%s)</p><ul>%s</ul>") % (
            self.env._("Service"), years, months, days,
            self.env._("years / months / days"),
            Markup('').join(Markup("<li>%s</li>") % line for line in lines))
        return {
            'gratuity_calculation_method': method,
            'gratuity_for_years': round(for_years, 2),
            'gratuity_for_months': round(for_months, 2),
            'gratuity_for_days': round(for_days, 2),
            'gratuity_amount': total,
            'gratuity_formula_html': html,
        }

    def _leave_amounts(self):
        """The leave balance on the form, priced per day: the basic over
        thirty by the law (art. 29), the gross where the office chooses it.
        The "leave salaries eligible" figure reads as whole months of it."""
        self.ensure_one()
        method = self.leave_calculation_method or 'basic'
        monthly = (self.fm_gross_salary if method == 'total' else self.leave_basic_salary) or 0.0
        daily = monthly / DAYS_IN_MONTH
        balance = self.leave_days_balance or 0.0
        amount = round(balance * daily, 2)
        return {
            'leave_calculation_method': method,
            'leave_daily_rate': round(daily, 2),
            'leave_amount_to_pay': amount,
            'leave_salaries_eligible': int(balance // DAYS_IN_MONTH),
            'leave_salaries_balance': round((balance % DAYS_IN_MONTH) * daily, 2),
            'leave_total_payable': amount,
        }

    def _final_month_amounts(self):
        """The last month is already paid - the payslip pays the calendar
        month, the days after the period in advance - so the settlement
        only takes back what was paid and not worked: the absence days of
        that month at the day's rate. Overtime the punches show and no
        payslip paid is added; HR zeroes the hours when it was."""
        self.ensure_one()
        last_day = self.last_day_of_duty or fields.Date.context_today(self)
        days_in_month = self.fm_total_days or calendar.monthrange(last_day.year, last_day.month)[1]
        absence = self.total_absence_days or 0
        gross = self.fm_gross_salary or 0.0
        rate_per_day = gross / days_in_month if days_in_month else 0.0
        deduction = round(-absence * rate_per_day, 2)
        overtime = round((self.total_ot_regular or 0.0) * (self.fm_ot_rate_regular or 0.0)
                         + (self.total_ot_off or 0.0) * (self.fm_ot_rate_off or 0.0), 2)
        rows = [
            (self.env._("Month"), "%s (%s days, paid with the payslip)" % (last_day.strftime('%b %Y'), days_in_month)),
            (self.env._("Absence taken back"), "%s day(s) x %s / %s = %s" % (
                absence, gross, days_in_month, deduction)),
            (self.env._("Overtime"), "%g h x %s + %g h x %s = %s" % (
                self.total_ot_regular or 0.0, self.fm_ot_rate_regular or 0.0,
                self.total_ot_off or 0.0, self.fm_ot_rate_off or 0.0, overtime)),
        ]
        html = Markup("<table class='table table-sm'>%s</table>") % Markup('').join(
            Markup("<tr><th>%s</th><td>%s</td></tr>") % (k, v) for k, v in rows)
        return {
            'fm_rate_per_day': round(rate_per_day, 2),
            'fm_total_salary_month': deduction,
            'fm_overtime_salary': overtime,
            'fm_net_salary': round(deduction + overtime + (self.fm_salary_adjustment or 0.0), 2),
            'salary_html': html,
        }

    def _owed_html(self, employee, last_day):
        """What the system sees as still owed either way, for HR to read
        before writing Other Adjustments by hand: advances and loans still
        outstanding, unpaid attachments, amounts held from a leave allowance.
        Nothing here is added to the settlement.

        An advance is counted once, from what is left of it - never again
        from its installment rows, whatever type or link they carry. And an
        attachment is counted only from the month of the last day of duty
        on: an older one still reading unpaid was settled by its own payslip
        and is a record nobody closed, not a debt.
        """
        lines, total = [], 0.0
        first_of_month = last_day.replace(day=1)
        month_number = {abbr: number for number, abbr in MONTH_ABBR.items()}
        advance_type = self.env.ref('construction_payroll.attachment_type_advance_deduction',
                                    raise_if_not_found=False)
        for advance in self.env['construction.advance'].sudo().search(
                [('employee_id', '=', employee.id), ('state', '!=', 'closed'),
                 ('outstanding_amount', '>', 0)]):
            total -= advance.outstanding_amount
            lines.append((self.env._("Advance outstanding: %s") % advance.display_name,
                          -advance.outstanding_amount))
        # A loan is a Salary Adjustment on the employee: what is left on each
        # running one that takes money back is owed at the end, as an advance
        # is. The adjustment behind an advance was counted just above.
        counted = self.env['construction.advance'].sudo().search(
            [('employee_id', '=', employee.id)]).mapped('salary_adjustment_id')
        for adjustment in self.env['hr.salary.attachment'].sudo().search([
                ('employee_ids', 'in', employee.ids), ('state', '=', 'open'),
                ('is_refund', '=', True), ('id', 'not in', counted.ids)]):
            if not adjustment.has_total_amount or adjustment.remaining_amount <= 0:
                continue
            total -= adjustment.remaining_amount
            lines.append((self.env._(
                "Salary Adjustment outstanding: %s",
                adjustment.description or adjustment.other_input_type_id.name),
                -adjustment.remaining_amount))
        # The months of an adjustment still unpaid are part of what it has left.
        domain = [('employee_id', '=', employee.id), ('state', '!=', 'paid'),
                  ('advance_id', '=', False), ('installment_id', '=', False),
                  ('salary_adjustment_id', '=', False)]
        if advance_type:
            domain.append(('type_id', '!=', advance_type.id))
        skipped = 0
        for att in self.env['construction.attachment'].sudo().search(domain):
            try:
                period = first_of_month.replace(year=int(att.year), month=month_number[att.month])
            except (ValueError, KeyError, TypeError):
                period = first_of_month
            if period < first_of_month:
                skipped += 1
                continue
            total += att.signed_value
            lines.append(("%s (%s %s)" % (att.name, att.month, att.year), att.signed_value))
        if skipped:
            lines.append((self.env._(
                "%(count)s older attachment(s) still reading unpaid are left out: "
                "settled by their own payslips.", count=skipped), 0.0))
        for held in self.env['construction.on.hold'].sudo().search(
                [('employee_id', '=', employee.id), ('state', '=', 'held')]):
            total += held.amount
            lines.append((self.env._("Released from hold: %s") % held.name, held.amount))
        return Markup("<p><b>%s</b> %s</p><table class='table table-sm'>%s<tr><th>%s</th><td><b>%s</b></td></tr></table>") % (
            self.env._("For information - not added to the settlement."),
            self.env._("Write what applies in Other Adjustments."),
            Markup('').join(Markup("<tr><td>%s</td><td>%s</td></tr>") % (label, round(value, 2))
                            for label, value in lines),
            self.env._("Sum"), round(total, 2))

    def action_check(self):
        self.write({'state': 'checked'})

    def action_in_payment(self):
        self.write({'state': 'in_payment'})

    def action_set_paid(self):
        self.write({'state': 'paid'})

    def action_reset(self):
        self.write({'state': 'new'})


class ConstructionOnHold(models.Model):
    _name = 'construction.on.hold'
    _description = "On-Hold Amount"
    _inherit = ['mail.thread']
    _order = 'id desc'

    name = fields.Char(string="Description", required=True)
    employee_id = fields.Many2one('hr.employee', string="Employee", required=True, tracking=True)
    company_id = fields.Many2one(related='employee_id.company_id', store=True, index=True)
    currency_id = fields.Many2one(related='employee_id.currency_id', store=True)
    amount = fields.Monetary(string="Held Amount", tracking=True)
    reason = fields.Char(string="Reason")
    # Leave allowance this amount was held from.
    allowance_id = fields.Many2one('construction.leave.expense', string="Allowance link")
    attach = fields.Boolean(string="Attach with Salary")
    state = fields.Selection(
        [('held', 'Created'), ('released', 'Paid')],
        string="Status", default='held', tracking=True, index=True,
    )

    def action_release(self):
        self.write({'state': 'released'})

    def action_hold(self):
        self.write({'state': 'held'})
