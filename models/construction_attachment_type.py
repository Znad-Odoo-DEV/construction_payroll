# -*- coding: utf-8 -*-
from odoo import api, fields, models


class ConstructionAttachmentType(models.Model):
    _name = 'construction.attachment.type'
    _description = "Salary Attachment Type"
    _order = 'name'

    name = fields.Char(string="Name", required=True)
    kind = fields.Selection(
        [('addition', 'Addition'), ('deduction', 'Deduction')],
        string="Kind", required=True, default='addition',
        help="Whether amounts of this type add to or subtract from the salary.",
    )
    # +1 for additions, -1 for deductions. Kept as a stored field so payslip
    # adjustment = sum(factor * value) stays a simple, readable computation.
    factor = fields.Integer(
        string="Factor", compute='_compute_factor', store=True,
        help="Sign applied to the value in the payslip adjustment "
             "(+1 for additions, -1 for deductions).",
    )
    active = fields.Boolean(string="Active", default=True)

    # Which expense type this attachment posts under in the salary batch's
    # journal entry. A link, not a name: the account per company hangs off the
    # expense type, so renaming a type cannot move its money,
    # and a new company is a line added there rather than a field added here.
    expense_type_id = fields.Many2one(
        'construction.expense.type', string="Journal Entry Leg",
        help="Expense type the salary batch posts this attachment under. Each "
             "expense type carries its account per company. Left empty, the "
             "attachment posts under Salary Adjustment and the batch says so.",
    )

    # Which Salary Adjustment types (the Type on an employee's Salary
    # Adjustment) land on the payslip as this attachment type. A link, kept
    # here on our model, so the adjustment's own model is never inherited.
    input_type_ids = fields.Many2many(
        'hr.payslip.input.type', 'construction_attachment_type_input_type_rel',
        'attachment_type_id', 'input_type_id', string="Salary Adjustment Types",
        help="A Salary Adjustment of one of these types reaches the payslip as "
             "this attachment type. An adjustment whose type is listed nowhere "
             "lands as Salary Deductions when it is a negative value, else as "
             "Salary Addition.",
    )

    @api.model
    def _for_salary_adjustment(self, adjustment):
        """The attachment type a Salary Adjustment's monthly line is made as."""
        input_type = adjustment.other_input_type_id
        if input_type:
            mapped = self.search([('input_type_ids', 'in', input_type.ids)], limit=1)
            if mapped:
                return mapped
        return self.env.ref(
            'construction_payroll.attachment_type_salary_deduction' if adjustment.is_refund
            else 'construction_payroll.attachment_type_salary_addition')

    # Well-known type names mapped to an expense code, so a type created by
    # name arrives already pointing at the right account. Only ever consulted at creation: once the link is set, the name
    # is free to change.
    _CODE_BY_NAME = {
        'salary addition': 'salary_addition',
        'salary additions': 'salary_addition',
        'salary deduction': 'salary_deduction',
        'salary deductions': 'salary_deduction',
        'phone bill reimbursement': 'phone_bill',
        'sick leave reimbursement': 'sick_leave',
        'medical bill reimbursement': 'medical_bill',
        'salary advance': 'advance',
        'salary advance-deduction': 'advance',
        'salary advance deduction': 'advance',
        'leave salary': 'leave_allowance',
        'salary bonus': 'bonus',
        'air ticket reimbursement': 'air_ticket',
        'penalty fine': 'penalty',
    }

    @api.model
    def _expense_type_for_name(self, name):
        """The expense type a salary attachment of this name has always posted
        under, or an empty recordset when the name is new to us."""
        ExpenseType = self.env['construction.expense.type']
        code = self._CODE_BY_NAME.get((name or '').strip().lower())
        return ExpenseType._for_code(code) if code else ExpenseType.browse()

    def _posting_account(self, company):
        """The account this type posts to in ``company``.

        Falls back to the Salary Adjustment type, because dropping a line would
        lose real money in silence, whereas a balance in Salary Adjustment is a
        question somebody will ask. Returns an empty recordset when even that
        is unconfigured; the batch turns that into a message naming what to set.
        """
        self.ensure_one()
        expense_type = (
            self.expense_type_id
            or self.env['construction.expense.type']._for_code('adjustment')
        )
        return expense_type._line_for(company).account_id


    @api.depends('kind')
    def _compute_factor(self):
        for rec in self:
            rec.factor = 1 if rec.kind == 'addition' else -1
