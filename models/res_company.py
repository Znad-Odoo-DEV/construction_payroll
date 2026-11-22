# -*- coding: utf-8 -*-
from odoo import fields, models


class ResCompany(models.Model):
    _inherit = 'res.company'

    # Short code used in batch / document naming, e.g. ACME.
    construction_company_code = fields.Char(
        string="Payroll Code",
        help="Short company code used in salary batch and document names "
             "(e.g. ACME).",
    )
    # Abbreviation used in the WPS part of the batch name, e.g. ACMELLC.
    construction_company_abbr = fields.Char(
        string="Payroll Abbreviation",
        help="Company abbreviation used in WPS batch names "
             "(e.g. ACMELLC).",
    )
    # Labour payroll cycle start day. The cycle runs from this day of the
    # previous month to (start_day - 1) of the current month (default 21 -> 20).
    construction_payroll_start_day = fields.Integer(
        string="Payroll Start Day",
        default=21,
        help="Day of month the labour payroll cycle starts on. "
             "The cycle runs from this day of the previous month to the day "
             "before it in the current month (e.g. 21 -> 21st to the 20th).",
    )
    # Weekly off / overtime day. Overtime worked on this day is paid at the
    # "off days" rate; a missing record on this day is bridged from the
    # surrounding days instead of counting as absence.
    construction_weekly_off_day = fields.Selection(
        [
            ('0', 'Monday'), ('1', 'Tuesday'), ('2', 'Wednesday'),
            ('3', 'Thursday'), ('4', 'Friday'), ('5', 'Saturday'),
            ('6', 'Sunday'),
        ],
        string="Weekly Off Day", default='4',
        help="Weekly rest day treated as an overtime/off day in attendance.",
    )

    # The journal the salary batch entry is posted in. Deliberately NOT named
    # construction_payroll_journal_id: that name is taken on the expenses side for the
    # purchase journal payroll bills go to, and one field cannot be two
    # journals of two different types.
    #
    # Every account the entry touches lives in construction.expense.type instead, one
    # line per company. Charts of accounts differ between companies, so an
    # account number written into Python would be a different account in each.
    construction_salary_journal_id = fields.Many2one(
        'account.journal', string="Salary Journal", check_company=True,
        domain="[('type', '=', 'general')]",
        help="Journal the salary batch's journal entry is posted in "
             "(typically SLR).",
    )

    # ------------------------------------------------------------------
    # How an overtime hour is priced
    #
    # The hourly rate is the basic wage over a month of days and a day of
    # hours, and overtime is that rate times a premium: a quarter more on a
    # normal day, half as much again on the weekly off day and public
    # holidays. That is the UAE labour law and it is what this payroll has
    # always done.
    #
    # They are company settings rather than employee columns because they do
    # not vary per employee. Kept as fields rather than constants because 30 and 8 are a working
    # month and a working day, and those are facts about a company.
    # ------------------------------------------------------------------
    construction_ot_days_per_month = fields.Float(
        string="Payroll Days per Month", default=30.0,
        help="Days a monthly wage is divided by to reach a daily rate.",
    )
    construction_ot_hours_per_day = fields.Float(
        string="Working Hours per Day", default=8.0,
        help="Hours a daily rate is divided by to reach an hourly rate.",
    )
    construction_ot_factor_regular = fields.Float(
        string="Overtime Factor - Regular Days", default=1.25,
        help="Multiplier on the hourly rate for overtime worked on a normal "
             "working day.",
    )
    construction_ot_factor_off = fields.Float(
        string="Overtime Factor - Off Days", default=1.5,
        help="Multiplier on the hourly rate for overtime worked on the weekly "
             "off day or a public holiday.",
    )
