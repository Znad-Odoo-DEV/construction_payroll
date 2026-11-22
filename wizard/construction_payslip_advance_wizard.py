# -*- coding: utf-8 -*-
from odoo import api, fields, models
from odoo.exceptions import UserError

from ..models.construction_attendance_sheet import ADVANCE_REVIEW_RATIO


class ConstructionPayslipAdvanceWizard(models.TransientModel):
    """Grant, change or take back the advance days on one payslip.

    The sheet holds an advance automatically when the employee attended less
    than half the period. That is arithmetic, and it is deliberately not the
    final word: the reason for a low attendance lives in Time Off and in what
    the payroll officer knows, so the decision belongs to a person.

    WHAT THIS WRITES, AND WHY IT IS NOT THE PAYSLIP

    advance_days and advance_amount on the payslip are stored computes reading
    construction.attendance.summary. Writing them directly would hold until the next
    recompute and then vanish leaving nothing behind. So this writes the
    summary - advance_days and advance_state - and lets the payslip follow:

        summary.advance_days   ->  payslip.advance_days
                               ->  payslip.total_attendance (earned + advanced)
        x payslip.rate_per_day ->  payslip.advance_amount   ->  net_amount

    which is why the wizard shows the amount and never writes one.

    WHAT IT WILL NOT REFUSE

    Two things stop it, and neither is a judgement about the employee: a
    payslip with no attendance summary, which has nowhere to put an advance,
    and a negative number of days.

    Nothing else. has_advance = False on the sheet does not block - the flag is
    derived once at generation from three booleans on the employee master, it
    can outlive its reason, and it was asked to stop being a wall. Nor does a
    payslip already marked Paid. Both are still real warnings, so every one
    that is crossed is named in the note and in the chatter, beside the money
    it let through: a decision left on the record rather than in a dialog
    nobody sees again.
    """
    _name = 'construction.payslip.advance.wizard'
    _description = "Grant Advance Days on a Payslip"

    payslip_id = fields.Many2one(
        'construction.payslip', string="Payslip", required=True, readonly=True,
        ondelete='cascade',
    )
    employee_id = fields.Many2one(
        related='payslip_id.employee_id', string="Employee", readonly=True)
    currency_id = fields.Many2one(related='payslip_id.currency_id', readonly=True)
    summary_id = fields.Many2one(
        related='payslip_id.summary_id', string="Attendance Summary", readonly=True)

    # The decision needs its evidence in front of it, not on another screen.
    attended_days = fields.Float(
        related='summary_id.attended_days', string="Attended Days", readonly=True)
    period_days = fields.Integer(
        related='summary_id.period_days', string="Period Days", readonly=True)
    attendance_ratio = fields.Float(
        related='summary_id.attendance_ratio', string="Attendance %", readonly=True)
    current_days = fields.Integer(
        related='summary_id.advance_days', string="Days Written Now", readonly=True)
    currently_granted = fields.Boolean(
        related='summary_id.advance_granted', string="Granted Now", readonly=True)
    reason = fields.Text(
        string="Why it is held", compute='_compute_reason', readonly=True)

    advance_days = fields.Integer(
        string="Advance Days", required=True,
        help="Days after the period to pay in advance. Zero takes the advance "
             "back and records the refusal.",
    )
    rate_per_day = fields.Monetary(
        related='payslip_id.rate_per_day', string="Rate per Day", readonly=True)
    advance_amount = fields.Monetary(
        string="Advance Amount", compute='_compute_advance_amount', readonly=True,
        help="What these days are worth at this payslip's own rate per day.",
    )
    note = fields.Text(
        string="Note", required=True,
        help="The reason for the decision. Appended to the payslip's Notes & "
             "Remarks, stamped with the date and who decided.",
    )

    # ------------------------------------------------------------------
    @api.model
    def default_get(self, fields_list):
        values = super().default_get(fields_list)
        slip_id = values.get('payslip_id') \
            or self.env.context.get('default_payslip_id')
        slip = self.env['construction.payslip'].browse(slip_id) if slip_id \
            else self.env['construction.payslip']
        if slip.exists() and 'advance_days' in fields_list:
            # Start from what the sheet already worked out. Somebody opening
            # this to approve a held advance wants the sheet's own number, not
            # an empty box to guess in.
            values['advance_days'] = slip.summary_id.advance_days or 0
        return values

    @api.depends('advance_days', 'rate_per_day')
    def _compute_advance_amount(self):
        for wizard in self:
            wizard.advance_amount = wizard.advance_days * (wizard.rate_per_day or 0.0)

    @api.depends('summary_id')
    def _compute_reason(self):
        for wizard in self:
            summary = wizard.summary_id
            sheet = wizard.payslip_id.attendance_sheet_id
            if not summary:
                wizard.reason = "This payslip has no attendance summary."
                continue
            lines = []
            if not summary.has_advance:
                flags = wizard._eligibility_flags(summary)
                if flags:
                    lines.append(
                        "Not advance-eligible on the sheet - the employee "
                        "carries %s today. Validating grants it anyway and "
                        "records that in the note." % ", ".join(flags))
                else:
                    lines.append(
                        "Marked not advance-eligible, but NONE of the three "
                        "flags (Approved NOR, On Leave, Submitted Cancellation) "
                        "is set on the employee today - a stale photograph. "
                        "Validating clears it.")
            elif summary.needs_review:
                lines.append(
                    "Attendance %.0f%% of the period, below %g%% - the advance "
                    "is held for a decision." % (
                        summary.attendance_ratio, ADVANCE_REVIEW_RATIO))
            else:
                lines.append("Not held - this advance is granted automatically.")
            if sheet:
                lines.extend(sheet._advance_leave_lines(summary))
            wizard.reason = "\n".join(line.strip() for line in lines)

    @api.model
    def _eligibility_flags(self, summary):
        """What withholds the advance on the employee TODAY.

        has_advance is not typed anywhere: _employee_profile derives it once,
        at generation, from hr.employee._construction_advance_eligible - archived, a
        departure date, or an approved leave covering the day. It is therefore
        a photograph, and it can be stale in either direction - a leave that
        ended last week is still in the picture the sheet took. Saying what
        holds NOW is what lets a stale photograph be recognised instead of
        obeyed.
        """
        employee = summary.employee_id.sudo()
        if not employee:
            return []
        flags = []
        if not employee.active:
            flags.append("Archived")
        if employee.departure_date:
            flags.append("Departure Date %s" % employee.departure_date)
        if employee.active and not employee.departure_date \
                and not employee._construction_advance_eligible().get(employee.id):
            flags.append("On Leave")
        return flags

    # ------------------------------------------------------------------
    def action_validate(self):
        self.ensure_one()
        slip = self.payslip_id
        summary = self.summary_id
        if not summary:
            raise UserError(self.env._(
                "This payslip has no attendance summary, so there is no advance "
                "to grant. The advance is stored on the summary, not on the "
                "payslip."))
        if self.advance_days < 0:
            raise UserError(self.env._("Advance days cannot be negative."))

        # Nothing else refuses. The eligibility flag and the paid state are
        # both real warnings, and both were asked to stop being blocks, so they
        # become a record instead of a wall: whatever was crossed is written
        # into the note and the chatter, where it stays readable beside the
        # money it let through.
        overrides = []
        if slip.state == 'paid':
            overrides.append(
                "the payslip was already marked Paid, so this moves a figure"
                " after the payment")
        if not summary.has_advance:
            flags = self._eligibility_flags(summary)
            # has_advance is not typed by anyone: _employee_profile derives it
            # at generation from hr.employee._construction_advance_eligible, so it is a
            # photograph and can outlive its reason. Turning it on here is what makes the days worth
            # anything - advance_granted is computed from it - and whether the
            # picture was stale or still true is the difference the note keeps.
            summary.has_advance = True
            overrides.append(
                "Advance Eligible was OFF on the sheet and was turned on here"
                " (%s)" % (
                    "employee still carries " + ", ".join(flags) if flags
                    else "nothing withholds it on the employee today - a"
                         " stale photograph"))

        if self.advance_days:
            summary.advance_days = self.advance_days
            summary.advance_state = 'approved'
        else:
            # A refusal is a decision, not an erasure: the days the sheet
            # worked out stay on the record so what was refused is still
            # readable, exactly as action_reject_advance leaves them.
            #
            # That only holds while the line is under review, because
            # advance_granted follows advance_state ONLY then; off review it
            # ignores the state and reads True. Writing zero days is itself
            # what ends the review - needs_review is has_advance AND
            # advance_days AND a low ratio - so zeroing days to refuse an
            # advance flips the very flag that made the refusal mean anything.
            # Hence: refuse first, and take the days away only if the record
            # still comes back granted.
            summary.advance_state = 'rejected'
            if summary.advance_granted:
                summary.advance_days = 0
        # Generation writes zero days whenever the sheet carries this flag, so
        # regenerating the sheet later would silently undo the decision made
        # here.
        sheet = slip.attendance_sheet_id
        if sheet and sheet.no_advance_days:
            sheet.no_advance_days = False
        if sheet:
            slip.auto_note = sheet._build_payslip_note(summary)

        stamp = "[%s %s] Advance %s day(s), %s %s: %s" % (
            fields.Date.context_today(self), self.env.user.name,
            self.advance_days,
            slip.currency_id.symbol or slip.currency_id.name or '',
            "{:,.2f}".format(self.advance_amount),
            (self.note or '').strip(),
        )
        for line in overrides:
            stamp += " [%s]" % line
        slip.note = (slip.note.rstrip() + "\n" + stamp) if slip.note else stamp
        slip.message_post(body=stamp)
        return {'type': 'ir.actions.act_window_close'}
