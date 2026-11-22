# -*- coding: utf-8 -*-
from odoo import fields, models


class ConstructionPublicHoliday(models.Model):
    _name = 'construction.public.holiday'
    _description = "Payroll Public Holiday"
    _order = 'date desc'

    name = fields.Char(string="Name", required=True)
    date = fields.Date(string="Date", required=True, index=True)
    # Empty company means the holiday applies to every company.
    company_id = fields.Many2one(
        'res.company', string="Company",
        help="Leave empty to apply the holiday to all companies.",
    )
    note = fields.Char(string="Note")

    # One spelling per series (19.0 has models.Constraint, 18.0 has
    # _sql_constraints and no such class), same resulting
    # constraint, construction_public_holiday_date_company_uniq.
    if hasattr(models, 'Constraint'):
        _date_company_uniq = models.Constraint(
            'unique(date, company_id)',
            "A public holiday already exists for this date and company.",
        )
    else:
        _sql_constraints = [(
            'date_company_uniq',
            'unique(date, company_id)',
            "A public holiday already exists for this date and company.",
        )]
