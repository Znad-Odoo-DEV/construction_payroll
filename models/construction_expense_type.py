# -*- coding: utf-8 -*-
"""What a payroll expense is posted to, per company.

Searching for an account by its code and a journal by its name in the body of
each rule, once per company and once per kind of expense, would mean editing
Python to add a company. A chart of accounts is configuration, and
configuration belongs in records.

So: one record per kind of expense, and one line per company under it saying
which account and which product that company posts it to. Adding a company is
adding lines; nothing here is edited to do it.
"""
from odoo import api, fields, models


class ConstructionExpenseType(models.Model):
    _name = 'construction.expense.type'
    _description = "Payroll Expense Type"
    _order = 'sequence, id'

    name = fields.Char(string="Name", required=True, translate=True)
    # The key the code asks for. Stable, and never shown to anybody.
    code = fields.Char(
        string="Code", required=True, index=True,
        help="Key the payroll code asks for: leave_allowance, gratuity, "
             "basic, housing and so on. Do not change it on a live database.",
    )
    sequence = fields.Integer(string="Sequence", default=10)
    active = fields.Boolean(string="Active", default=True)
    line_ids = fields.One2many(
        'construction.expense.type.account', 'expense_type_id', string="Per Company",
    )
    company_count = fields.Integer(
        string="Companies", compute='_compute_company_count',
    )

    # 19.0 dropped _sql_constraints and 18.0 has no models.Constraint, so both
    # spellings are here behind the same switch the rest of this module uses.
    # The branch has to keep booting on 18 until the database is upgraded.
    if hasattr(models, 'Constraint'):
        _code_unique = models.Constraint(
            'unique(code)', "That expense code is already used.")
    else:
        _sql_constraints = [
            ('code_unique', 'unique(code)', "That expense code is already used."),
        ]

    @api.depends('line_ids')
    def _compute_company_count(self):
        for rec in self:
            rec.company_count = len(rec.line_ids)

    def _line_for(self, company):
        """The posting line for one company, or an empty recordset."""
        self.ensure_one()
        if not company:
            return self.line_ids.browse([])
        return self.line_ids.filtered(lambda l: l.company_id == company)[:1]

    @api.model
    def _for_code(self, code):
        return self.search([('code', '=', code)], limit=1)


class ConstructionExpenseTypeAccount(models.Model):
    _name = 'construction.expense.type.account'
    _description = "Payroll Expense Account per Company"
    _order = 'company_id, id'

    expense_type_id = fields.Many2one(
        'construction.expense.type', string="Expense Type",
        required=True, ondelete='cascade', index=True,
    )
    company_id = fields.Many2one(
        'res.company', string="Company", required=True, index=True,
        default=lambda self: self.env.company,
    )
    account_id = fields.Many2one(
        'account.account', string="Expense Account", required=True,
        domain="[('company_ids', 'in', company_id)]",
        help="Account the bill line is posted to for this company.",
    )
    product_id = fields.Many2one(
        'product.product', string="Product",
        help="Product put on the bill line. Optional: the line carries its own "
             "label and account either way.",
    )

    if hasattr(models, 'Constraint'):
        _one_per_company = models.Constraint(
            'unique(expense_type_id, company_id)',
            "That expense type already has a line for this company.")
    else:
        _sql_constraints = [
            ('one_per_company', 'unique(expense_type_id, company_id)',
             "That expense type already has a line for this company."),
        ]
