# -*- coding: utf-8 -*-
import logging
from datetime import date

from dateutil.relativedelta import relativedelta

from odoo import Command, api, fields, models
from odoo.exceptions import UserError
from odoo.tools.misc import formatLang

from .utils import MONTH_ABBR

_logger = logging.getLogger(__name__)


class ConstructionAdvance(models.Model):
    _name = 'construction.advance'
    _description = "Advance Salary"
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'id desc'

    name = fields.Char(string="Description", compute='_compute_name', store=True, readonly=False)
    employee_id = fields.Many2one('hr.employee', string="Employee", required=True, tracking=True)
    company_id = fields.Many2one(related='employee_id.company_id', store=True, index=True)
    currency_id = fields.Many2one(related='employee_id.currency_id', store=True)
    # groups=False on purpose: a related field inherits the target's groups,
    # and registration_number is behind hr.group_hr_user. This copy sits on a
    # payroll record and is read by the payroll groups, as it always was.
    employee_code = fields.Char(related='employee_id.registration_number', string="Employee ID", store=True, groups=False)

    total_amount = fields.Monetary(string="Total Amount", tracking=True)
    no_of_installments = fields.Integer(string="No. of Installments", default=1)
    date = fields.Date(string="Date", default=fields.Date.context_today)
    approval_date = fields.Date(string="Approval Date")
    request_link = fields.Char(string="Request Link")

    installment_ids = fields.One2many('construction.advance.installment', 'advance_id', string="Installments")
    # The Salary Adjustment on the employee that takes this advance back,
    # month by month, through the payslip. Once it exists it is what deducts:
    # no installment row is scheduled ahead.
    salary_adjustment_id = fields.Many2one(
        'hr.salary.attachment', string="Salary Adjustment", readonly=True, copy=False,
        index=True, ondelete='set null',
        help="The employee's Salary Adjustment that deducts this advance.")
    outstanding_amount = fields.Monetary(
        string="Outstanding Amount", compute='_compute_outstanding', store=True,
    )

    state = fields.Selection(
        [
            ('new', 'New'),
            ('in_payment', 'In Payments'),
            ('running', 'Running'),
            ('closed', 'Closed'),
        ],
        string="Status", default='new', tracking=True, index=True,
    )

    @api.depends('employee_id')
    def _compute_name(self):
        for rec in self:
            rec.name = self.env._("Advance for %s") % (rec.employee_id.name or '')

    @api.depends('total_amount', 'installment_ids.paid', 'installment_ids.amount',
                 'salary_adjustment_id.paid_amount', 'salary_adjustment_id.state')
    def _compute_outstanding(self):
        for rec in self:
            adjustment = rec.salary_adjustment_id
            if adjustment:
                # One figure for one advance: what its Salary Adjustment has
                # left. An installment ticked for a month that took less than
                # its amount would otherwise say the advance is further along.
                left = max(rec.total_amount - adjustment.paid_amount, 0.0)
                rec.outstanding_amount = 0.0 if (adjustment.state == 'close' and left < 1.0) else left
            else:
                paid = sum(rec.installment_ids.filtered('paid').mapped('amount'))
                rec.outstanding_amount = rec.total_amount - paid

    def action_generate_installments(self):
        for rec in self:
            if not rec.no_of_installments:
                raise UserError(self.env._("Set the number of installments first."))
            if rec.installment_ids.filtered(lambda i: i.paid or i.attachment_id):
                raise UserError(self.env._(
                    "%s already has installments being deducted; they cannot be "
                    "rebuilt.", rec.display_name))
            rec.installment_ids.unlink()
            rec.installment_ids = rec._build_installments(
                rec.total_amount, rec.no_of_installments)
        return True

    def _build_installments(self, total, count):
        """Split ``total`` into ``count`` installments, rounding the last one
        to absorb any remainder, due month by month from next month - the
        same schedule the request offers the employee."""
        self.ensure_one()
        base = round(total / count, 2) if count else 0.0
        first = fields.Date.context_today(self).replace(day=1) + relativedelta(months=1)
        commands = []
        allocated = 0.0
        for i in range(1, count + 1):
            amount = base if i < count else round(total - allocated, 2)
            allocated += amount
            commands.append((0, 0, {
                'name': self.env._("Installment no.%02d") % i,
                'amount': amount,
                'due_date': first + relativedelta(months=i - 1),
            }))
        return commands

    # ------------------------------------------------------------------
    # The deductions: one salary attachment per installment, made up front.
    #
    # The whole series is made the day the advance is approved - a row per
    # month - and the payslip of each month picks its row up. Each row knows
    # which installment it settles.
    # ------------------------------------------------------------------
    def action_generate_deductions(self):
        """A Salary Advance-Deduction attachment for every installment that
        has none yet, in the month its due date names. Idempotent."""
        Attachment = self.env['construction.attachment']
        deduction_type = self.env.ref('construction_payroll.attachment_type_advance_deduction')
        made = 0
        for rec in self:
            if rec.salary_adjustment_id:
                # Its Salary Adjustment makes each month's row when the
                # payslip is made; one scheduled here would be a second.
                continue
            pending = rec.installment_ids.filtered(
                lambda i: not i.attachment_id and not i.paid and i.amount)
            if not pending:
                continue
            if pending.filtered(lambda i: not i.due_date):
                raise UserError(self.env._(
                    "%s has an installment with no due month. Set it first.",
                    rec.display_name))
            count = len(rec.installment_ids)
            for index, installment in enumerate(
                    rec.installment_ids.sorted(lambda i: (i.due_date or fields.Date.today(), i.id)), 1):
                if installment not in pending:
                    continue
                attachment = Attachment.create({
                    'name': self.env._("Advance %(ref)s - %(name)s of %(count)s",
                                       ref=rec.request_link or rec.name,
                                       name=installment.name, count=count),
                    'employee_id': rec.employee_id.id,
                    'type_id': deduction_type.id,
                    'month': MONTH_ABBR[installment.due_date.month],
                    'year': str(installment.due_date.year),
                    'value': installment.amount,
                    'advance_id': rec.id,
                    'installment_id': installment.id,
                    'state': 'new',
                    'auto_generated': False,
                })
                installment.attachment_id = attachment.id
                made += 1
            rec.message_post(body=self.env._(
                "%(count)s deduction(s) generated, one per installment, from "
                "%(first)s to %(last)s.",
                count=len(pending),
                first=min(pending.mapped('due_date')).strftime('%b %Y'),
                last=max(pending.mapped('due_date')).strftime('%b %Y')))
        return made

    @api.model
    def _construction_salary_adjustment_type(self):
        """The Salary Adjustment type advances are deducted under: the first
        one listed on the Salary Advance-Deduction attachment type."""
        deduction = self.env.ref(
            'construction_payroll.attachment_type_advance_deduction', raise_if_not_found=False)
        return deduction.sudo().input_type_ids[:1] if deduction else \
            self.env['hr.payslip.input.type'].sudo().browse()

    def _construction_open_salary_adjustment(self, date_start=None):
        """Open, once, the Salary Adjustment that deducts each advance: the
        whole amount, a negative value, at the first installment a month,
        from the first installment's month (``date_start`` overrides it).

        Returns the advances it could not open one for, because no Salary
        Adjustment type is set for advances yet; the caller decides what
        those fall back to.
        """
        input_type = self._construction_salary_adjustment_type()
        if not input_type:
            return self.filtered(lambda r: not r.salary_adjustment_id)
        # sudo: payroll officers approve advances without holding rights on
        # Payroll's own model; only this advance's employee is written for.
        Adjustment = self.env['hr.salary.attachment'].sudo()
        next_month = fields.Date.context_today(self).replace(day=1) + relativedelta(months=1)
        for rec in self.filtered(lambda r: not r.salary_adjustment_id and r.total_amount > 0):
            schedule = rec.installment_ids.sorted(lambda i: (i.due_date or date.max, i.id))
            monthly = schedule[:1].amount if schedule else \
                rec.total_amount / max(rec.no_of_installments, 1)
            first_due = schedule.filtered('due_date')[:1].due_date
            start = date_start or (first_due.replace(day=1) if first_due else next_month)
            adjustment = Adjustment.create({
                'employee_ids': [Command.set(rec.employee_id.ids)],
                'company_id': rec.employee_id.company_id.id,
                'other_input_type_id': input_type.id,
                'description': rec.request_link or rec.name,
                'monthly_amount': rec.currency_id.round(monthly) or rec.total_amount,
                'total_amount': rec.total_amount,
                'duration_type': 'limited',
                'is_refund': True,
                'date_start': start,
            })
            adjustment.message_post(body=self.env._(
                "Opened from the advance %s.", rec.display_name))
            rec.salary_adjustment_id = adjustment.id
            rec.message_post(body=self.env._(
                "Deducted through the Salary Adjustment on the employee: %(monthly)s a "
                "month from %(start)s until %(total)s is paid.",
                monthly=formatLang(self.env, adjustment.monthly_amount, currency_obj=rec.currency_id),
                start=start.strftime('%b %Y'),
                total=formatLang(self.env, rec.total_amount, currency_obj=rec.currency_id)))
        return self.browse()

    def _refresh_state(self):
        """Follow the deductions: running once one is on a payslip, closed
        once every installment is paid. The buttons stay for a person to
        move it by hand; this only moves it where the money already did."""
        for rec in self:
            adjustment = rec.sudo().salary_adjustment_id
            if adjustment:
                # Deducted through a Salary Adjustment: it says where the
                # advance stands, in both directions.
                if adjustment.state == 'close':
                    state = 'closed'
                elif adjustment.paid_amount or rec.state == 'closed':
                    state = 'running'
                else:
                    state = rec.state
                if rec.state != state:
                    rec.state = state
                continue
            if rec.state == 'closed':
                continue
            installments = rec.installment_ids
            if installments and all(installments.mapped('paid')):
                rec.state = 'closed'
            elif rec.state in ('new', 'in_payment') and installments.filtered(
                    lambda i: i.attachment_id and i.attachment_id.state != 'new'):
                rec.state = 'running'

    def unlink(self):
        for rec in self:
            if rec.installment_ids.filtered('paid') or rec.installment_ids.mapped(
                    'attachment_id').filtered(lambda a: a.state == 'paid'):
                raise UserError(self.env._(
                    "%s has installments already deducted from a paid salary; it "
                    "cannot be deleted.", rec.display_name))
        return super().unlink()

    def action_confirm_payment(self):
        self.write({'state': 'in_payment'})

    def action_run(self):
        self.write({'state': 'running'})

    def action_close(self):
        self.write({'state': 'closed'})

    def action_reset(self):
        self.write({'state': 'new'})


class ConstructionAdvanceInstallment(models.Model):
    _name = 'construction.advance.installment'
    _description = "Advance Installment"
    _order = 'advance_id, id'

    advance_id = fields.Many2one(
        'construction.advance', string="Advance", required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(related='advance_id.company_id', store=True)
    currency_id = fields.Many2one(related='advance_id.currency_id', store=True)
    name = fields.Char(string="Installment Description", required=True)
    amount = fields.Monetary(string="Amount")
    due_date = fields.Date(
        string="Due", help="The month this installment is deducted in.")
    attachment_id = fields.Many2one(
        'construction.attachment', string="Deduction", readonly=True, copy=False,
        ondelete='set null',
        help="The salary attachment that deducts this installment.")
    paid = fields.Boolean(
        string="Paid",
        help="Set by payroll when the payslip carrying the deduction is paid; "
             "may be ticked by hand for an advance settled outside payroll.")

    def write(self, vals):
        res = super().write(vals)
        if 'paid' in vals:
            self.mapped('advance_id')._refresh_state()
        return res
