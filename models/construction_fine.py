# -*- coding: utf-8 -*-
import logging

from dateutil.relativedelta import relativedelta

from odoo import api, fields, models
from odoo.exceptions import UserError

from .utils import MONTH_ABBR

_logger = logging.getLogger(__name__)


class ConstructionFine(models.Model):
    _name = 'construction.fine'
    _description = "Fine / Deduction"
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'id desc'

    name = fields.Char(string="Description", compute='_compute_name', store=True, readonly=False)
    reference = fields.Char(string="Reference", copy=False, readonly=True)
    employee_id = fields.Many2one('hr.employee', string="Employee", required=True, tracking=True)
    company_id = fields.Many2one(related='employee_id.company_id', store=True, index=True)
    currency_id = fields.Many2one(related='employee_id.currency_id', store=True)

    total_fine_amount = fields.Monetary(string="Total Fine Amount", tracking=True)
    fine_description = fields.Char(string="Fine Description")
    requested_by = fields.Many2one('res.users', string="Requested by")
    no_of_installments = fields.Integer(string="No. of Installments", default=1)
    fine_date = fields.Date(string="Fine Date", default=fields.Date.context_today)
    supporting_document = fields.Binary(string="Supporting Document")
    supporting_document_name = fields.Char(string="Supporting Document Name")
    first_installment_due_date = fields.Date(string="1st Installment due date")

    installment_ids = fields.One2many('construction.attachment', 'fine_id', string="Installments")
    claimed_amount = fields.Monetary(
        string="Claimed Amount", compute='_compute_amounts', store=True,
        help="Amount already attached/paid through payslips.",
    )
    outstanding_amount = fields.Monetary(
        string="Outstanding Amount", compute='_compute_amounts', store=True,
    )
    state = fields.Selection(
        [('draft', 'Draft'), ('running', 'Running'), ('closed', 'Closed')],
        string="Status", default='draft', tracking=True, index=True,
    )

    @api.depends('reference', 'employee_id')
    def _compute_name(self):
        for rec in self:
            rec.name = "Fine for %s - %s" % (
                rec.employee_id.name or '', rec.reference or '')

    @api.depends('total_fine_amount', 'installment_ids.value', 'installment_ids.state')
    def _compute_amounts(self):
        for rec in self:
            claimed = sum(
                rec.installment_ids.filtered(lambda a: a.state in ('attached', 'paid'))
                .mapped('value'))
            rec.claimed_amount = claimed
            rec.outstanding_amount = rec.total_fine_amount - claimed

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('reference'):
                vals['reference'] = self.env['ir.sequence'].next_by_code('construction.fine') or '/'
        return super().create(vals_list)

    def action_generate_installments(self):
        """Split the fine into monthly salary deductions, one attachment per
        installment, starting at the first installment due date."""
        deduction_type = self.env.ref(
            'construction_payroll.attachment_type_salary_deduction')
        for rec in self:
            if rec.installment_ids:
                raise UserError(self.env._(
                    "%s already has installments.", rec.display_name))
            count = max(rec.no_of_installments, 1)
            first = (rec.first_installment_due_date or fields.Date.context_today(rec)).replace(day=1)
            base = round(rec.total_fine_amount / count, 2)
            allocated = 0.0
            vals_list = []
            for i in range(count):
                amount = base if i < count - 1 else round(rec.total_fine_amount - allocated, 2)
                allocated += amount
                due = first + relativedelta(months=i)
                vals_list.append({
                    'name': self.env._("Fine %(ref)s - %(number)s of %(count)s",
                                       ref=rec.reference, number=i + 1, count=count),
                    'employee_id': rec.employee_id.id,
                    'type_id': deduction_type.id,
                    'month': MONTH_ABBR[due.month],
                    'year': str(due.year),
                    'value': amount,
                    'fine_id': rec.id,
                    'state': 'new',
                })
            self.env['construction.attachment'].create(vals_list)
            if rec.state == 'draft':
                rec.state = 'running'
        return True

    def action_close(self):
        self.write({'state': 'closed'})

    def action_reset(self):
        self.write({'state': 'draft'})
