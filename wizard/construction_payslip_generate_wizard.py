# -*- coding: utf-8 -*-
"""One payslip, for one employee, over a period somebody chooses.

Payslips are made by the month, from an attendance sheet that lists the whole
company. Some are not: a labourer who was left off the sheet, somebody leaving
on the tenth who is paid to the tenth, a correction after the month was closed.
Until now those were typed by hand - the days counted on a calendar, the
absence priced on a calculator - which is the one way to get a payslip that
disagrees with every other payslip the company issues.

This does not count anything itself. It makes an attendance sheet for that one
employee over that period and sends it down the road every sheet takes:
generate, approve, generate payroll. So the missing check-out, the half day,
the stranded Friday, the holiday penalty, the sick leave paid back by its
attachment, the project split and the note on the payslip are all the monthly
engine's own, because it IS the monthly engine. The sheet stays on record
behind the payslip, which is where anybody asks "where did these days come
from".
"""
import calendar

from odoo import api, fields, models
from odoo.exceptions import UserError

from ..models.utils import MONTH_ABBR


class ConstructionPayslipGenerateWizard(models.TransientModel):
    _name = 'construction.payslip.generate.wizard'
    _description = "Generate One Payslip"

    employee_id = fields.Many2one(
        'hr.employee', string="Employee", required=True,
        help="Anybody. Labour and project staff go through the labour "
             "attendance sheet, Office / Engineer staff through the staff one - "
             "each the way their monthly payslip is made.")
    company_id = fields.Many2one(related='employee_id.company_id', readonly=True)
    is_office_staff = fields.Boolean(related='employee_id.construction_is_office_staff')
    date_from = fields.Date(string="From", required=True)
    date_to = fields.Date(string="To", required=True)

    no_advance_days = fields.Boolean(
        string="Pay the Period Only", default=True,
        help="On: the payslip pays the days of this period and nothing after "
             "it. Off: the days from the end of the period to the end of its "
             "month are paid in advance, as the monthly sheet does.")
    missing_checkout_is_absent = fields.Boolean(
        string="No Check-Out Is Absence", default=True,
        help="Same switch as on the monthly sheet.")
    last_month_start = fields.Date(string="Last Month From")
    last_month_end = fields.Date(
        string="Last Month To",
        help="Optional. A previous period to settle on this payslip - its "
             "absence deducted and its overtime added, as attachments - the "
             "way the monthly sheet settles the days it advanced.")

    existing_note = fields.Text(compute='_compute_existing_note')

    @api.onchange('date_from')
    def _onchange_date_from(self):
        if self.date_from and not self.date_to:
            last = calendar.monthrange(self.date_from.year, self.date_from.month)[1]
            self.date_to = self.date_from.replace(day=last)

    def _month_key(self):
        self.ensure_one()
        return MONTH_ABBR[self.date_to.month], str(self.date_to.year)

    def _open_payslips(self):
        """Unpaid payslips this employee already holds for the same month."""
        self.ensure_one()
        if not (self.employee_id and self.date_to):
            return self.env['construction.payslip']
        month, year = self._month_key()
        return self.env['construction.payslip'].sudo().search([
            ('employee_id', '=', self.employee_id.id),
            ('month', '=', month), ('year', '=', year),
            ('state', '!=', 'paid'),
        ])

    @api.depends('employee_id', 'date_to')
    def _compute_existing_note(self):
        for wizard in self:
            slips = wizard._open_payslips()
            wizard.existing_note = slips and self.env._(
                "%(name)s already holds an unpaid payslip for this month: "
                "%(slips)s. Generating another would move its generated "
                "attachments onto the new one. Pay or delete it first.",
                name=wizard.employee_id.name,
                slips=", ".join(slips.mapped('display_name'))) or False

    def action_generate(self):
        self.ensure_one()
        employee = self.employee_id.sudo()
        if self.date_from > self.date_to:
            raise UserError(self.env._("From must be before To."))
        if (self.date_from.year, self.date_from.month) != (
                self.date_to.year, self.date_to.month):
            raise UserError(self.env._(
                "A payslip belongs to one month: a day is priced at the gross "
                "over the days of ITS month. Make one payslip per month."))
        if not employee.company_id:
            raise UserError(self.env._("%s has no company.", employee.name))
        # The monthly engine replaces its own unpaid attachments for the month
        # and attaches whatever is pending. With an unpaid payslip already
        # there, that would strip it of its deductions in silence.
        if self.existing_note:
            raise UserError(self.existing_note)

        if employee.construction_is_office_staff:
            return self._generate_staff(employee)

        # As the system: the payroll officer pressing this need not hold the
        # attendance groups, and every step below is one the sheet's own
        # buttons take. Who pressed it is on the sheet's chatter.
        Sheet = self.env['construction.attendance.sheet'].sudo()
        sheet = Sheet.create({
            'company_id': employee.company_id.id,
            'start_date': self.date_from,
            'last_date': self.date_to,
            'last_month_start': self.last_month_start,
            'last_month_end': self.last_month_end,
            'no_advance_days': self.no_advance_days,
            'missing_checkout_is_absent': self.missing_checkout_is_absent,
            'only_employee_ids': [(6, 0, [employee.id])],
        })
        sheet.name = "%s - %s" % (sheet.name, employee.name)
        sheet.message_post(body=self.env._(
            "Made by %(user)s from Generate One Payslip, for %(name)s only.",
            user=self.env.user.name, name=employee.name))
        sheet.action_generate()

        summary = sheet.summary_ids
        if not summary:
            sheet.unlink()
            raise UserError(self.env._(
                "%s is not on the roster for this period - check the joining "
                "date and that the employee is active.", employee.name))
        if summary.needs_review and summary.advance_state == 'pending':
            # The engine holds the advance of somebody who attended under half
            # the period until a person decides. That person is the one
            # reading this, and the decision is made on the sheet.
            return self._open(sheet, self.env._(
                "Attendance is under half the period: approve or reject the "
                "advance on the sheet, then approve it and generate payroll."))
        sheet.action_submit()
        sheet.action_approve()
        sheet.action_generate_payroll()

        slip = sheet.payslip_ids
        if not slip:
            return self._open(sheet, self.env._(
                "No payslip was made: the period holds no paid day for %s. "
                "The sheet shows why.", employee.name))
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'construction.payslip',
            'res_id': slip[0].id,
            'view_mode': 'form',
            'target': 'current',
        }

    def _generate_staff(self, employee):
        """The same road, on the staff sheet: a monthly staff attendance
        listing this one person, generated over the chosen period, approved,
        and turned into its payslip by the staff generation itself."""
        Sheet = self.env['construction.staff.attendance'].sudo()
        sheet = Sheet.create({
            'company_id': employee.company_id.id,
            'month': str(self.date_to.month),
            'year': self.date_to.year,
            'only_employee_ids': [(6, 0, [employee.id])],
        })
        sheet.name = "%s - %s" % (sheet.name, employee.name)
        sheet.message_post(body=self.env._(
            "Made by %(user)s from Generate One Payslip, for %(name)s only.",
            user=self.env.user.name, name=employee.name))
        sheet._generate_lines(self.date_from, self.date_to,
                              self.last_month_start, self.last_month_end)
        sheet.write({
            'start_date': self.date_from, 'last_date': self.date_to,
            'last_month_start': self.last_month_start,
            'last_month_end': self.last_month_end,
            'state': 'generated',
        })
        line = sheet.line_ids
        if not line:
            sheet.unlink()
            raise UserError(self.env._(
                "%s could not be put on a staff sheet - check the company and "
                "that the employee is active.", employee.name))
        if self.no_advance_days:
            # The period and nothing after it: no days to advance, and so no
            # decision left pending to block the sheet.
            line.write({'advance_days': 0, 'advance_state': 'rejected'})
        else:
            # On the staff sheet the advance is always a person's decision.
            return self._open(sheet, self.env._(
                "Approve or reject the advance on the line, then submit, "
                "approve and generate the payslip."))
        sheet.action_submit()
        sheet.action_approve()
        sheet.action_generate_payslips()
        slip = sheet.payslip_ids
        if not slip:
            return self._open(sheet, self.env._(
                "No payslip was made for %s. The sheet shows why.", employee.name))
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'construction.payslip',
            'res_id': slip[0].id,
            'view_mode': 'form',
            'target': 'current',
        }

    def _open(self, sheet, message):
        sheet.message_post(body=message)
        return {
            'type': 'ir.actions.act_window',
            'res_model': sheet._name,
            'res_id': sheet.id,
            'view_mode': 'form',
            'target': 'current',
        }
