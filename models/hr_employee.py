# -*- coding: utf-8 -*-
"""The analytic account that stands for one employee.

An analytic account per employee can sit in its own root plan (for example a
"Salaries & Wages" plan), alongside the projects in another one. Two root
plans, so a journal item can carry both at once and each still totals 100%.

Finding that account by searching for the employee's name works until two
people share a name, and then it charges one person's salary to the other. So
the link lives here, on hr.employee, as a link. The payslip already carries
the employee; the account hangs off that.
"""
import logging

from odoo import api, fields, models

from .utils import user_groups

_logger = logging.getLogger(__name__)


class HrEmployee(models.Model):
    _inherit = 'hr.employee'

    # Money fields kept from the HR Admin group on office staff: the
    # contract, the four allowances and the three rates that follow from it.
    _CONSTRUCTION_SALARY_FIELDS = (
        'wage', 'l10n_ae_housing_allowance', 'l10n_ae_transportation_allowance',
        'l10n_ae_other_allowances', 'construction_gross_salary', 'construction_rate_per_day',
        'construction_hourly_rate', 'construction_ot_rate_regular', 'construction_ot_rate_off',
    )

    construction_analytic_account_id = fields.Many2one(
        'account.analytic.account', string="Payroll Analytic Account",
        groups="hr.group_hr_user",
        help="Analytic account this employee's payroll cost is charged to. "
             "Set it and the salary batch's journal entry carries the employee "
             "as a cost dimension beside the project; leave it empty and that "
             "employee's share of the entry carries the project only.",
    )

    # ------------------------------------------------------------------
    # Workforce classification
    #
    # Office and engineer staff are paid from the monthly staff attendance
    # sheet and are not paid overtime. Project staff work on sites: they punch,
    # they are on the labour sheet, and they are paid like labour.
    #
    # One field with three values rather than two booleans, because "office
    # but not staff" would mean nothing.
    # ------------------------------------------------------------------
    construction_workforce = fields.Selection(
        [
            ('labour', 'Labour'),
            ('project_staff', 'Project Staff'),
            ('office_staff', 'Office / Engineer Staff'),
        ],
        string="Workforce", default='labour', tracking=True, index=True,
        help="Office/Engineer staff are paid from the monthly staff attendance "
             "sheet and are not paid overtime. Project staff are staff who work "
             "on sites: they punch, they are on the labour sheet, and they are "
             "paid overtime like labour. Labour is everybody else.",
    )
    # Reading the classification as a question rather than as a value, so the
    # rules and the routing say what they mean and stop caring how it is
    # stored.
    construction_is_office_staff = fields.Boolean(
        string="Office Staff", compute='_compute_construction_is_office_staff',
        store=True, index=True,
        help="Handled by the staff attendance sheet, and never paid overtime.",
    )

    @api.depends('construction_workforce')
    def _compute_construction_is_office_staff(self):
        for employee in self:
            employee.construction_is_office_staff = employee.construction_workforce == 'office_staff'

    # ------------------------------------------------------------------
    # Overtime
    #
    # Eligibility is a flag of its own and not derived from the workforce.
    # Some labourers are not staff and are still not paid overtime, so
    # "eligible" is not "not staff", and deriving it would start paying them
    # for hours nobody agreed to.
    #
    # The rates are not stored figures: they are the same arithmetic for every
    # employee - the basic wage over 30 days and 8 hours, times a premium on a
    # normal day and a higher one on an off day. Numbers that are all produced
    # by one rule go stale the moment somebody raises a wage and forgets them,
    # so the rule is here and the premiums are company settings. The basic
    # wage is `wage`, while the gross sits in l10n_ae_total_salary.
    # ------------------------------------------------------------------
    construction_overtime_eligible = fields.Boolean(
        string="Overtime Eligible", tracking=True,
        groups="construction_payroll.group_labour_payroll,construction_payroll.group_staff_payroll",
        help="Whether overtime is paid at all. Independent of the workforce: "
             "an employee can be labour and still not be paid overtime.",
    )
    construction_hourly_rate = fields.Float(
        string="Hourly Rate", compute='_compute_construction_overtime_rates', store=True,
        groups="construction_payroll.group_labour_payroll,construction_payroll.group_staff_payroll",
        help="The basic wage over a working month and a working day. Both "
             "divisors are set on the company.",
    )
    construction_ot_rate_regular = fields.Float(
        string="OT Rate - Regular Days",
        compute='_compute_construction_overtime_rates', store=True,
        groups="construction_payroll.group_labour_payroll,construction_payroll.group_staff_payroll",
        help="Hourly rate times the company's regular-day overtime factor.",
    )
    construction_ot_rate_off = fields.Float(
        string="OT Rate - Off / Holidays",
        compute='_compute_construction_overtime_rates', store=True,
        groups="construction_payroll.group_labour_payroll,construction_payroll.group_staff_payroll",
        help="Hourly rate times the company's off-day overtime factor.",
    )

    @api.depends('wage', 'construction_overtime_eligible',
                 'company_id.construction_ot_days_per_month',
                 'company_id.construction_ot_hours_per_day',
                 'company_id.construction_ot_factor_regular',
                 'company_id.construction_ot_factor_off')
    def _compute_construction_overtime_rates(self):
        for employee in self:
            # A rate on somebody who is not paid overtime is a number waiting
            # to be used by mistake: it reads as "this is what his hour costs"
            # on a form, in an export and in any rule that forgets to ask the
            # flag first. Nobody who is not eligible has one.
            if not employee.construction_overtime_eligible:
                employee.construction_hourly_rate = 0.0
                employee.construction_ot_rate_regular = 0.0
                employee.construction_ot_rate_off = 0.0
                continue
            company = employee.company_id
            divisor = ((company.construction_ot_days_per_month or 0)
                       * (company.construction_ot_hours_per_day or 0))
            hourly = (employee.wage or 0.0) / divisor if divisor else 0.0
            employee.construction_hourly_rate = hourly
            employee.construction_ot_rate_regular = hourly * (company.construction_ot_factor_regular or 0)
            employee.construction_ot_rate_off = hourly * (company.construction_ot_factor_off or 0)

    # The gross the payroll runs on: basic and the three allowances. Not the
    # localisation's l10n_ae_total_salary, which is a typed figure on the
    # version and can disagree with that sum - a day of absence priced off a
    # stale total is a wrong deduction. Stored, so the sheets and the requests
    # read it without recomputing.
    construction_gross_salary = fields.Monetary(
        string="Total / Gross Salary", compute='_compute_construction_gross_salary', store=True,
        currency_field='currency_id',
        groups="construction_payroll.group_labour_payroll,construction_payroll.group_staff_payroll",
        help="Basic salary plus the housing, transportation and other allowances.",
    )
    # The gross over a thirty-day month: what a day of absence or sick leave is
    # priced at. Derived,
    # never typed, so it cannot drift from the salary it is a thirtieth of.
    construction_rate_per_day = fields.Monetary(
        string="Rate per Day (30d)", compute='_compute_construction_gross_salary', store=True,
        currency_field='currency_id',
        groups="construction_payroll.group_labour_payroll,construction_payroll.group_staff_payroll",
        help="Total / gross salary divided by 30.",
    )

    @api.depends('wage', 'l10n_ae_housing_allowance', 'l10n_ae_transportation_allowance',
                 'l10n_ae_other_allowances')
    def _compute_construction_gross_salary(self):
        for employee in self:
            version = employee.sudo()
            gross = ((version.wage or 0.0) + (version.l10n_ae_housing_allowance or 0.0)
                     + (version.l10n_ae_transportation_allowance or 0.0)
                     + (version.l10n_ae_other_allowances or 0.0))
            employee.construction_gross_salary = gross
            employee.construction_rate_per_day = gross / 30.0

    # ------------------------------------------------------------------
    # Staff project distribution
    #
    # Office staff punch nowhere in particular, so their month is split over
    # projects by agreed percentages rather than by the days worked on each.
    # ------------------------------------------------------------------
    construction_staff_project_ids = fields.One2many(
        'construction.staff.project', 'employee_id', string="Staff Project Distribution")

    # ------------------------------------------------------------------
    # Advance eligibility
    #
    # The question has three answers the roster already keeps: the employee is
    # still active, has no departure date, and is not on an approved leave
    # today.
    # ------------------------------------------------------------------
    def _construction_advance_eligible(self):
        """{employee id: bool} - whether the end-of-month advance is given.

        One leave search for the whole batch, as the system: leaves are private
        to HR and the payroll officer generating a sheet need not be HR."""
        today = fields.Date.context_today(self)
        employees = self.sudo()
        on_leave = set()
        Leave = self.env.get('hr.leave')
        if Leave is not None and employees:
            day_start = fields.Datetime.to_datetime(today)
            day_end = day_start.replace(hour=23, minute=59, second=59)
            on_leave = set(Leave.sudo().search([
                ('employee_id', 'in', employees.ids),
                ('state', '=', 'validate'),
                ('date_from', '<=', day_end),
                ('date_to', '>=', day_start),
            ]).mapped('employee_id').ids)
        return {
            employee.id: bool(
                employee.active
                and not employee.departure_date
                and employee.id not in on_leave)
            for employee in employees
        }

    # ------------------------------------------------------------------
    # The Emirates ID
    #
    # Odoo's UAE payroll reads ONE field for the employee's WPS identifier:
    # Identification No (identification_id, on the version). That field must
    # hold the 14-digit person code of the labour card, which is what the bank
    # expects in a WPS file, so the Emirates ID gets a field of its own.
    #
    # On the employee and not on the version: it is the person's, it does not
    # change with the contract.
    # ------------------------------------------------------------------
    construction_emirates_id = fields.Char(
        string="Emirates ID", groups="hr.group_hr_user", tracking=True, copy=False,
        help="The Emirates ID number. Identification No is kept for the WPS "
             "person code, which is what the WPS export reads.",
    )

    # ------------------------------------------------------------------
    # The WPS code is the Employee ID
    #
    # The code the employees are paid under in WPS is the number already kept
    # as the Employee ID (registration_number). Odoo's export does not read that field, it reads
    # Identification No, so the number lives in both: typed once, as the
    # Employee ID, and carried to Identification No from there.
    #
    # Carried, not locked. A WPS code typed by hand that differs from the
    # Employee ID is somebody's decision and is left alone; only a code that
    # was empty, or was the previous Employee ID, follows the new one. It is
    # written on every version of the employee, because the field is the
    # version's and a payslip may read the version of its own dates.
    # ------------------------------------------------------------------
    def _construction_is_wps(self):
        """Whether this employee is paid through WPS: it has a WPS code.

        The payslip generation asks this to choose the batch.
        """
        self.ensure_one()
        return bool((self.sudo().identification_id or '').strip())

    def _construction_set_wps_code(self, value):
        """Write ``value`` as the WPS code on every version of ``self``."""
        versions = self.sudo().with_context(active_test=False).version_ids
        if versions:
            versions.write({'identification_id': (value or '').strip() or False})

    @api.model_create_multi
    def create(self, vals_list):
        employees = super().create(vals_list)
        for employee in employees.sudo():
            code = (employee.registration_number or '').strip()
            if code and not (employee.identification_id or '').strip():
                employee._construction_set_wps_code(code)
        return employees

    def write(self, vals):
        if 'registration_number' not in vals or 'identification_id' in vals:
            return super().write(vals)
        following = self.sudo().filtered(
            lambda e: (e.identification_id or '').strip()
            == (e.registration_number or '').strip())
        result = super().write(vals)
        following._construction_set_wps_code(vals.get('registration_number'))
        return result

    # ------------------------------------------------------------------
    # Salary confidentiality for the HR Admin group
    # ------------------------------------------------------------------
    # True for a user who may see the employee but not the salary of office
    # staff (the HR Admin group). Drives the view: the salary fields are
    # hidden, and read() below strips their values.
    construction_hide_salary = fields.Boolean(
        string="Salary Hidden", compute='_compute_construction_hide_salary')

    def _salary_restricted_user(self):
        """True when the current user must not see the salary of office staff.

        The trigger is membership of the attendance app's 'HR Admin' group - it
        applies even to a user who is also in the labour attendance/payroll
        groups (that overlap is exactly the case we must catch). Only the Staff
        Payroll groups are exempt, because they legitimately process office
        pay and payroll generation must read their real salary; the superuser
        and administrators are exempt too.

        The group lives in the companion attendance module (which depends on this
        module), so it is resolved at runtime rather than declared, to avoid a circular
        dependency."""
        env = self.env
        if env.su or env.is_admin():
            return False
        hr_admin = env.ref(
            'construction_attendance.group_construction_hr_admin', raise_if_not_found=False)
        if not hr_admin or hr_admin not in user_groups(env.user):
            return False
        staff_payroll_groups = (
            'construction_payroll.group_staff_payroll',
            'construction_payroll.group_staff_payroll_manager',
        )
        return not any(env.user.has_group(g) for g in staff_payroll_groups)

    @api.depends_context('uid')
    @api.depends('construction_is_office_staff')
    def _compute_construction_hide_salary(self):
        restricted = self._salary_restricted_user()
        for employee in self:
            employee.construction_hide_salary = bool(restricted and employee.construction_is_office_staff)

    def read(self, fields=None, load='_classic_read'):
        records = super().read(fields=fields, load=load)
        if not self._salary_restricted_user():
            return records
        # Strip the salary of office staff from the result. Read with sudo so
        # the flag itself is never masked, and never recurse (sudo is a
        # superuser context, so _salary_restricted_user is False there).
        office_ids = set(self.sudo().filtered('construction_is_office_staff').ids)
        for row in records:
            if row.get('id') in office_ids:
                for fname in self._CONSTRUCTION_SALARY_FIELDS:
                    if fname in row:
                        row[fname] = 0.0
        return records

    # ------------------------------------------------------------------
    # Name search
    #
    # The payroll forms find an employee by typing the Employee ID. The name
    # stays plain; the number and the badge are searched instead. Not through
    # _rec_names_search: both fields are behind hr.group_hr_user, and 19.0
    # refuses a domain on a field the user cannot read, so a payroll officer
    # outside HR could not have searched by name at all. Each is added only
    # for a user who may read it.
    # ------------------------------------------------------------------
    @api.model
    def _search_display_name(self, operator, value):
        domain = super()._search_display_name(operator, value)
        if not value or not operator.endswith('like') or operator.startswith('not'):
            return domain
        extra = [
            fname for fname in ('registration_number', 'barcode')
            if fname in self._fields
            and self._has_field_access(self._fields[fname], 'read')
        ]
        for fname in extra:
            domain = ['|', (fname, operator, value)] + list(domain)
        return domain
