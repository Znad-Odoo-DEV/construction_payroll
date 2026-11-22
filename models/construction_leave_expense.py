# -*- coding: utf-8 -*-
import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class ConstructionLeaveExpense(models.Model):
    _name = 'construction.leave.expense'
    _description = "Leave / Ticket Expense"
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'id desc'

    name = fields.Char(string="Description", compute='_compute_name', store=True, readonly=False)
    employee_id = fields.Many2one('hr.employee', string="Employee", required=True, tracking=True)
    company_id = fields.Many2one(related='employee_id.company_id', store=True, index=True)
    currency_id = fields.Many2one(related='employee_id.currency_id', store=True)
    # groups=False: see construction.advance.employee_code.
    employee_code = fields.Char(related='employee_id.registration_number', string="Employee ID", store=True, groups=False)
    request_link = fields.Char(string="Request Link")

    expense_type = fields.Selection(
        [
            ('leave_allowance', 'Leave Allowance'),
            ('ticket_expense', 'Ticket Expense'),
            ('ticket_reimbursement', 'Ticket Reimbursement'),
        ],
        string="Type", default='leave_allowance', required=True, tracking=True,
    )
    payable_amount = fields.Monetary(string="Payable Amount", tracking=True)
    approval_date = fields.Date(string="Approval Date")
    eid_expiry_date = fields.Date(string="E.I.D Expiry date")
    hold_500 = fields.Boolean(string="Hold Part of Amount?")

    # Supporting documents.
    approved_ticket = fields.Binary(string="Approved Ticket", attachment=True)
    approved_ticket_filename = fields.Char(string="Approved Ticket Filename")
    ticket_file = fields.Binary(string="Ticket File", attachment=True)
    ticket_file_filename = fields.Char(string="Ticket File Filename")

    payment_method = fields.Selection(
        [('cash', 'Cash'), ('bank', 'Bank'), ('salary', 'Attached with Salary')],
        string="Payment Method", readonly=True,
    )
    state = fields.Selection(
        [
            ('created', 'Created'),
            ('in_payment', 'In Payments'),
            ('attached', 'Attached W/Salary'),
            ('paid', 'Paid'),
        ],
        string="Status", default='created', tracking=True, index=True,
    )

    TYPE_LABELS = {
        'leave_allowance': 'Leave Allowance',
        'ticket_expense': 'Ticket Allowance',
        'ticket_reimbursement': 'Ticket Reimbursement',
    }

    @api.depends('expense_type', 'employee_id')
    def _compute_name(self):
        for rec in self:
            label = rec.TYPE_LABELS.get(rec.expense_type, 'Leave Allowance')
            rec.name = "%s for %s" % (label, rec.employee_id.name or '')

    def action_pay_cash(self):
        self.write({'payment_method': 'cash', 'state': 'in_payment'})

    def action_pay_bank(self):
        self.write({'payment_method': 'bank', 'state': 'in_payment'})

    def action_attach_salary(self):
        self.write({'payment_method': 'salary', 'state': 'attached'})

    def action_set_paid(self):
        self.write({'state': 'paid'})

    def action_reset(self):
        self.write({'state': 'created', 'payment_method': False})

    def _create_hold(self):
        """Hold back part of a leave allowance until the employee is back.

        One ``construction.on.hold`` row per expense flagged ``hold_500``; the
        amount is the ``construction_payroll.leave_hold_amount`` system
        parameter (500 by default). Running it twice creates nothing new.
        """
        Hold = self.env['construction.on.hold']
        held = Hold.browse([])
        flagged = self.filtered('hold_500')
        if not flagged:
            return held
        amount = float(self.env['ir.config_parameter'].sudo().get_param(
            'construction_payroll.leave_hold_amount', 500.0))
        existing = Hold.search([('allowance_id', 'in', flagged.ids)])
        for rec in flagged:
            hold = existing.filtered(lambda h: h.allowance_id == rec)
            if hold:
                held |= hold
                continue
            name = self.env._("Hold Amount for %s", rec.employee_id.name or '')
            if rec.request_link:
                name = "%s (%s)" % (name, rec.request_link)
            held |= Hold.create({
                'name': name,
                'employee_id': rec.employee_id.id,
                'amount': amount,
                'allowance_id': rec.id,
            })
        return held
