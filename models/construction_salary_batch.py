# -*- coding: utf-8 -*-
import logging
import re
from datetime import date

from dateutil.relativedelta import relativedelta

from odoo import api, fields, models
from odoo.exceptions import UserError

from .construction_project_chart import render_project_chart

_logger = logging.getLogger(__name__)

# Batch names start with the period, e.g. "2030JUL/ACME/WPS/...".
_MONTH_ABBR = {
    'JAN': 1, 'FEB': 2, 'MAR': 3, 'APR': 4, 'MAY': 5, 'JUN': 6,
    'JUL': 7, 'AUG': 8, 'SEP': 9, 'OCT': 10, 'NOV': 11, 'DEC': 12,
}
_PERIOD_RE = re.compile(r'^\s*(\d{4})\s*([A-Za-z]{3})')


class ConstructionSalaryBatch(models.Model):
    _name = 'construction.salary.batch'
    _description = "Salary Batch"
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'period_date desc, id desc'

    name = fields.Char(string="Batch Description", required=True, index=True)
    # First day of the batch's month/year, parsed from the name prefix, so
    # batches sort chronologically (the month abbreviation alone would sort
    # alphabetically: APR, AUG, DEC...).
    period_date = fields.Date(
        string="Period", compute='_compute_period_date', store=True, index=True,
    )
    company_id = fields.Many2one(
        'res.company', string="Company", required=True, index=True,
        default=lambda self: self.env.company,
    )
    currency_id = fields.Many2one(related='company_id.currency_id', store=True)
    attendance_sheet_id = fields.Many2one('construction.attendance.sheet', string="Attendance Sheet")
    submitted_by = fields.Many2one('res.users', string="Att. Submitted by")

    batch_type = fields.Selection(
        [('wps', 'WPS'), ('cash', 'Cash')], string="Payment Type",
    )
    is_staff = fields.Boolean(string="Staff Batch")

    show_project_distribution = fields.Boolean(string="Show Project Distribution", default=True)

    payslip_ids = fields.One2many('construction.payslip', 'batch_id', string="Payslips")
    project_ids = fields.One2many('construction.salary.batch.project', 'batch_id', string="Projects")

    total_salaries = fields.Monetary(
        string="Total Salaries Amount", compute='_compute_totals', store=True,
    )
    total_overtime = fields.Monetary(
        string="Total Overtime Amount", compute='_compute_totals', store=True,
    )
    total_net = fields.Monetary(
        string="Total Net Amount", compute='_compute_totals', store=True,
    )
    payslip_count = fields.Integer(compute='_compute_totals', store=True)

    state = fields.Selection(
        [
            ('created', 'Created'),
            ('submitted', 'Submitted'),
            ('approved', 'Approved'),
            ('je_created', 'Journal Entry Created'),
            ('paid', 'Paid'),
        ],
        string="Status", default='created', tracking=True, index=True,
    )

    @api.depends('payslip_ids.total_salary', 'payslip_ids.overtime_salary',
                 'payslip_ids.net_amount')
    def _compute_totals(self):
        for batch in self:
            slips = batch.payslip_ids
            batch.total_salaries = sum(slips.mapped('total_salary'))
            batch.total_overtime = sum(slips.mapped('overtime_salary'))
            batch.total_net = sum(slips.mapped('net_amount'))
            batch.payslip_count = len(slips)

    @api.depends('name')
    def _compute_period_date(self):
        for batch in self:
            batch.period_date = self._parse_period(batch.name)

    @api.model
    def _parse_period(self, name):
        """First day of the month/year encoded at the start of a batch name
        ("2030JUL/..." -> 2030-07-01). Returns False when unparseable."""
        match = _PERIOD_RE.match(name or '')
        if not match:
            return False
        month = _MONTH_ABBR.get(match.group(2).upper())
        if not month:
            return False
        return date(int(match.group(1)), month, 1)

    # ------------------------------------------------------------------
    # Workflow
    # ------------------------------------------------------------------
    def action_submit(self):
        self.write({'state': 'submitted'})

    def action_approve(self):
        self.write({'state': 'approved'})
        # Approving the batch attaches its payslips automatically (no per-slip
        # button): each not-yet-attached slip links its pending salary
        # attachments and moves to 'attached'. Paid/attached slips are left.
        for batch in self:
            slips = batch.payslip_ids.filtered(
                lambda s: s.state in ('new', 'checked'))
            if slips:
                slips.action_attach()

    def action_set_paid(self):
        self.write({'state': 'paid'})
        self.mapped('payslip_ids').action_set_paid()

    project_chart = fields.Html(
        string="Worked Days", compute='_compute_project_chart',
        sanitize=False, readonly=True,
    )
    project_chart_allocated = fields.Html(
        string="Including Advanced Days", compute='_compute_project_chart',
        sanitize=False, readonly=True,
    )

    @api.depends('project_ids.total_amount', 'project_ids.share',
                 'project_ids.allocated_total_amount',
                 'project_ids.allocated_share')
    def _compute_project_chart(self):
        for batch in self:
            label = lambda l: l.project_id.display_name
            batch.project_chart = render_project_chart(
                batch.project_ids, label_of=label,
                amount_of=lambda l: l.total_amount,
                share_of=lambda l: l.share,
                currency=batch.currency_id,
            )
            batch.project_chart_allocated = render_project_chart(
                batch.project_ids, label_of=label,
                amount_of=lambda l: l.allocated_total_amount,
                share_of=lambda l: l.allocated_share,
                currency=batch.currency_id,
            )

    def action_compute_project_distribution(self):
        """Roll the payslips' project lines up per project.

        Every payslip already carries what each project cost it: attendance
        days at the daily rate, and overtime hours at their own rates. Summing
        those is exact, so nothing is apportioned here. Salary attachments are
        deliberately left out: a project carries worked time, not the sick
        leaves, fines or advances settled on the payslip. The total therefore
        matches the batch's Total Salaries + Total Overtime, not its Net.
        """
        for batch in self:
            batch.project_ids.unlink()
            totals = {}
            for slip in batch.payslip_ids:
                for pline in slip.project_ids:
                    bucket = totals.setdefault(pline.project_id.id, {
                        'days': 0.0, 'salary': 0.0,
                        'ot_reg_h': 0.0, 'ot_off_h': 0.0,
                        'ot_reg': 0.0, 'ot_off': 0.0,
                        'alloc_days': 0.0, 'alloc_salary': 0.0,
                    })
                    bucket['days'] += pline.total_attendance
                    bucket['salary'] += pline.salary_amount
                    bucket['ot_reg_h'] += pline.total_overtime_reg
                    bucket['ot_off_h'] += pline.total_overtime_off
                    bucket['ot_reg'] += pline.ot_reg_amount
                    bucket['ot_off'] += pline.ot_off_amount
                    bucket['alloc_days'] += pline.allocated_attendance
                    bucket['alloc_salary'] += pline.allocated_salary_amount
            batch.project_ids = [
                (0, 0, {
                    'project_id': pid,
                    'total_days': v['days'],
                    'total_salaries': v['salary'],
                    'total_overtime_reg': v['ot_reg_h'],
                    'total_overtime_off': v['ot_off_h'],
                    'ot_reg_amount': v['ot_reg'],
                    'ot_off_amount': v['ot_off'],
                    'allocated_days': v['alloc_days'],
                    'allocated_salaries': v['alloc_salary'],
                })
                for pid, v in totals.items()
            ]
        return True

    def action_view_payslips(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': self.env._("Payslips"),
            'res_model': 'construction.payslip',
            'view_mode': 'list,form',
            'domain': [('batch_id', '=', self.id)],
            'context': {'default_batch_id': self.id},
        }


    # ==================================================================
    # Salary journal entry
    #
    # One entry per batch, in the company's own salary journal:
    #
    #   Dr  Basic / Housing / Travelling / Other / Overtime
    #   Dr  every addition attachment, on its own account
    #   Cr  every deduction attachment, on its own account
    #   Cr  Salaries Payable                              = the batch's net
    #
    # It is an `entry`, not a vendor bill. The salary is owed to the employees;
    # the WPS agent is how it is paid, not who it is owed to. Booking it as a
    # bill to that agent put every month's payroll in the purchase journal, and
    # credited the supplier payable rather than Salary Payable - which is why
    # 203003 has been sitting idle while the payroll sat in 201002 among the
    # real suppliers. The payment against Salaries Payable is where the agent
    # belongs.
    #
    # Nothing here is recalculated: every figure is read off the payslips,
    # which are the only place the money is decided. An entry that recomputed
    # its own gross would drift from the Total Net shown on the batch, and no
    # two people would ever agree which of the two was right.
    # ==================================================================

    move_id = fields.Many2one(
        'account.move', string="Journal Entry", readonly=True, copy=False,
        index='btree_not_null',
    )
    move_state = fields.Selection(related='move_id.state', string="Entry Status")

    # Every leg of the entry, as (internal key, construction.expense.type code). The
    # codes are expense type keys, and each type says which account that code
    # posts to in each company - so a new company is lines added there, never
    # a branch added here. Account numbers differ between charts, so a number
    # written in Python would be a different account per company.
    _JE_SALARY_CODES = (
        ('basic', 'basic'),
        ('housing', 'housing'),
        ('transport', 'transport'),
        ('other', 'other'),
        ('overtime', 'overtime'),
    )

    def _je_account(self, code):
        """The account ``code`` posts to in this batch's company."""
        self.ensure_one()
        return self.env['construction.expense.type']._for_code(code)._line_for(
            self.company_id).account_id

    def _je_missing_configuration(self):
        """What this batch's entry needs and cannot find.

        Collected all at once, so the user is told everything that is missing
        in one message rather than discovering it one account at a time.
        """
        self.ensure_one()
        company = self.company_id
        missing = []
        if not company.construction_salary_journal_id:
            missing.append(self.env._("Salary Journal on %s", company.display_name))
        if not self._je_account('salary_payable'):
            missing.append(self.env._("Salaries Payable account"))
        # A leg only needs an account when this batch actually carries it: a
        # labour batch has no housing allowance to post.
        legs = self._je_salary_legs()
        for key, code in self._JE_SALARY_CODES:
            if legs.get(key) and not self._je_account(code):
                missing.append(self.env._("Account for expense type %s", code))
        for att_type, amount in self._je_attachment_legs().items():
            if amount and not att_type._posting_account(company):
                missing.append(
                    self.env._("Account for attachment type %s", att_type.name))
        if missing:
            missing.append(self.env._(
                "Set the accounts under Payroll > Configuration > Expense "
                "Types, one line per company."))
        return missing

    def _je_payslip_legs(self):
        """Each payslip's salary, split into its four parts, plus overtime.

        A payslip stores the monthly contract figures and one prorated
        ``total_salary``; the split is that same proportion applied to each
        part. The three smaller parts are rounded and Basic takes the
        remainder, so the four always add back to ``total_salary`` exactly - a
        rounding difference here is an unbalanced journal entry.

        Kept per payslip rather than pre-totalled, because the analytic split
        needs to know whose money each part is, and totalling first would throw
        that away.
        """
        self.ensure_one()
        currency = self.company_id.currency_id
        out = []
        for slip in self.payslip_ids:
            legs = {'basic': 0.0, 'housing': 0.0, 'transport': 0.0,
                    'other': 0.0, 'overtime': currency.round(slip.overtime_salary)}
            salary = slip.total_salary
            gross = slip.gross_salary
            if salary and gross > 0:
                ratio = salary / gross
                legs['housing'] = currency.round(slip.house_allowance * ratio)
                legs['transport'] = currency.round(slip.transport_allowance * ratio)
                legs['other'] = currency.round(slip.other_allowance * ratio)
                legs['basic'] = (currency.round(salary) - legs['housing']
                                 - legs['transport'] - legs['other'])
            elif salary:
                # A payslip can carry a total with no contract snapshot behind
                # it. There is nothing to split it by, so it all goes to Basic
                # rather than being lost.
                legs['basic'] = currency.round(salary)
            out.append((slip, legs))
        return out

    def _je_salary_legs(self):
        """The batch's salary legs, totalled across its payslips."""
        self.ensure_one()
        currency = self.company_id.currency_id
        totals = {key: 0.0 for key, _code in self._JE_SALARY_CODES}
        for _slip, legs in self._je_payslip_legs():
            for key in totals:
                totals[key] += legs[key]
        return {key: currency.round(value) for key, value in totals.items()}

    def _je_attachment_legs(self):
        """Every salary attachment on the batch's payslips, totalled per type.

        Amounts are unsigned; the type's ``kind`` decides which side of the
        entry it lands on. Grouping is by the type record and never by its
        name: two types can be renamed into each other, and an account is not
        something a label should be able to move.
        """
        self.ensure_one()
        currency = self.company_id.currency_id
        totals = {}
        for attachment in self.payslip_ids.attachment_ids:
            if not attachment.type_id or not attachment.value:
                continue
            totals[attachment.type_id] = (
                totals.get(attachment.type_id, 0.0) + attachment.value)
        return {t: currency.round(v)
                for t, v in totals.items() if currency.round(v)}

    # ------------------------------------------------------------------
    # Analytic distribution
    #
    # Two dimensions on one line, not two lines. Projects and employees sit
    # in different analytic plans, and Odoo writes ONE analytic line per key in
    # the distribution - so a key naming both accounts, "<project>,<employee>",
    # is a single line carrying both columns and counted once. Listing them as
    # separate keys writes the amount twice into the analytic ledger.
    #
    # And the amounts are accumulated, never assigned: an employee with two
    # sick leaves in one month must not have the second overwrite the first,
    # or the distribution adds up to less than 100%.
    # ------------------------------------------------------------------
    def _je_employee_account(self, slip):
        """The analytic account standing for this payslip's employee.

        Read through ``hr.employee``, which is where the link lives: the
        payslip already carries the native employee, and the analytic account
        hangs off that. Nothing is searched by name - two people can share one.
        """
        return slip.employee_id.construction_analytic_account_id

    def _je_dimension_amounts(self, leg_key):
        """{(project account, employee account): amount} for one salary leg.

        A payslip's leg is spread over the projects it was worked on, in the
        proportions that payslip's own project lines already carry - allocated
        salary for the four salary parts, overtime money for overtime. Nothing
        is apportioned across employees: each keeps its own money, and only its
        own money is split.
        """
        self.ensure_one()
        amounts = {}
        for slip, legs in self._je_payslip_legs():
            amount = legs.get(leg_key) or 0.0
            if not amount:
                continue
            employee = self._je_employee_account(slip)
            if leg_key == 'overtime':
                weight_of = lambda p: p.total_overtime_amount
            else:
                weight_of = lambda p: p.allocated_total_amount - p.total_overtime_amount
            weights = [(p.project_id.account_id, weight_of(p))
                       for p in slip.project_ids]
            total = sum(w for _acc, w in weights if w > 0)
            if not total:
                # No project lines, or none carrying this kind of money. The
                # employee still gets it; the project plan simply has nothing
                # to say about it.
                key = (False, employee)
                amounts[key] = amounts.get(key, 0.0) + amount
                continue
            for project, weight in weights:
                if weight <= 0:
                    continue
                key = (project, employee)
                amounts[key] = amounts.get(key, 0.0) + amount * weight / total
        return amounts

    def _je_attachment_dimension_amounts(self, att_type):
        """{(False, employee account): amount} for one attachment type.

        Attachments carry no project. A project is charged for the time worked
        on it; a sick leave, an advance or a fine is settled with the employee
        and belongs to no site. Charging them to a project would make the same
        job look dearer for reasons that have nothing to do with the job - and
        these project figures are what work is priced on.
        """
        self.ensure_one()
        amounts = {}
        for slip in self.payslip_ids:
            employee = self._je_employee_account(slip)
            for attachment in slip.attachment_ids:
                if attachment.type_id != att_type or not attachment.value:
                    continue
                key = (False, employee)
                amounts[key] = amounts.get(key, 0.0) + attachment.value
        return amounts

    @api.model
    def _je_distribution(self, amounts):
        """Turn {(project, employee): amount} into an analytic distribution.

        Returns the distribution and the money that could reach neither
        dimension, so the caller can say so rather than let it vanish.
        """
        placed = {}
        homeless = 0.0
        for (project, employee), amount in amounts.items():
            ids = [str(acc.id) for acc in (project, employee) if acc]
            if not ids:
                homeless += amount
                continue
            key = ",".join(ids)
            placed[key] = placed.get(key, 0.0) + amount
        total = sum(placed.values()) + homeless
        if not total or not placed:
            return {}, homeless
        # Percentages of the WHOLE leg, so money with no dimension shows up as
        # a distribution under 100 rather than being quietly redistributed onto
        # the employees who did have an account.
        pct = {key: round(100.0 * amount / total, 2)
               for key, amount in placed.items()}
        if not homeless:
            biggest = max(pct, key=lambda k: pct[k])
            pct[biggest] = round(pct[biggest] + (100.0 - sum(pct.values())), 2)
        return pct, homeless

    def _je_move_date(self):
        """The day the entry belongs to: the last day of the attendance period
        it pays for, falling back to the last day of the batch's own month."""
        self.ensure_one()
        if self.attendance_sheet_id.last_date:
            return self.attendance_sheet_id.last_date
        if self.period_date:
            return self.period_date + relativedelta(months=1, days=-1)
        return fields.Date.context_today(self)

    def action_create_journal_entry(self):
        for batch in self:
            batch._create_journal_entry()
        return self.action_view_journal_entry()

    def _create_journal_entry(self):
        self.ensure_one()
        if self.move_id and self.move_id.state != 'cancel':
            raise UserError(self.env._(
                "This batch already has journal entry %s. Reverse or cancel it "
                "before creating another.", self.move_id.display_name))
        if self.state not in ('approved', 'je_created'):
            raise UserError(self.env._(
                "Only an approved batch can be posted. This one is %s.",
                dict(self._fields['state'].selection)[self.state]))
        if not self.payslip_ids:
            raise UserError(self.env._("This batch has no payslips to post."))

        batch = self.with_company(self.company_id)
        company = batch.company_id
        currency = company.currency_id

        missing = batch._je_missing_configuration()
        if missing:
            raise UserError(self.env._(
                "The salary journal entry cannot be built for %(company)s "
                "until these are set:\n\n%(missing)s",
                company=company.display_name,
                missing="\n".join("  - %s" % m for m in missing)))

        # Derived from the payslips, so rebuilt here rather than trusted: an
        # entry whose project split predates the last payslip correction
        # charges the wrong site, and nothing on screen would say so.
        if batch.show_project_distribution:
            batch.action_compute_project_distribution()

        legs = batch._je_salary_legs()
        lines = []
        unplaced = 0.0
        for key, code in batch._JE_SALARY_CODES:
            amount = legs.get(key) or 0.0
            if not amount:
                continue
            expense_type = self.env['construction.expense.type']._for_code(code)
            distribution, homeless = batch._je_distribution(
                batch._je_dimension_amounts(key))
            unplaced += homeless
            lines.append(batch._je_line(
                expense_type._line_for(company).account_id,
                "%s - %s" % (expense_type.name, batch.name),
                debit=amount, distribution=distribution))

        unmapped = []
        for att_type, amount in sorted(
                batch._je_attachment_legs().items(), key=lambda kv: kv[0].name):
            if not att_type.expense_type_id:
                unmapped.append(att_type.name)
            distribution, homeless = batch._je_distribution(
                batch._je_attachment_dimension_amounts(att_type))
            unplaced += homeless
            name = "%s - %s" % (att_type.name, batch.name)
            account = att_type._posting_account(company)
            if att_type.factor >= 0:
                lines.append(batch._je_line(
                    account, name, debit=amount, distribution=distribution))
            else:
                lines.append(batch._je_line(
                    account, name, credit=amount, distribution=distribution))

        # The payable carries no analytic: it is what is owed, not what
        # anything cost.
        net = currency.round(sum(batch.payslip_ids.mapped('net_amount')))
        lines.append(batch._je_line(
            batch._je_account('salary_payable'),
            self.env._("Net Salaries - %s", batch.name), credit=net))

        debit = sum(line[2]['debit'] for line in lines)
        credit = sum(line[2]['credit'] for line in lines)
        if currency.compare_amounts(debit, credit) != 0:
            # Cannot happen while a payslip's net is its salary plus overtime
            # plus its signed attachments - which is what makes the two sides
            # equal by construction. If it ever does, the payslip figures
            # disagree with each other and posting would only bury that.
            raise UserError(self.env._(
                "The entry does not balance: debit %(debit)s, credit "
                "%(credit)s. The payslip figures behind this batch are "
                "inconsistent.", debit=debit, credit=credit))

        # sudo: the person who approves a payroll batch is a payroll manager,
        # not an accountant, and giving payroll the right to write anywhere in
        # the ledger to press one button would be the larger hole. The entry is
        # created in draft, carries the batch in its ref, and the chatter
        # records who pressed it.
        move = self.env['account.move'].sudo().with_company(company).create({
            'move_type': 'entry',
            'journal_id': company.construction_salary_journal_id.id,
            'company_id': company.id,
            'date': batch._je_move_date(),
            'ref': batch.name,
            'line_ids': lines,
        })
        batch.move_id = move
        batch.state = 'je_created'
        batch._je_log(move, unplaced, unmapped)
        return move

    def _je_line(self, account, name, debit=0.0, credit=0.0, distribution=None):
        """One journal item, as a create command."""
        vals = {
            'account_id': account.id,
            'name': name[:200],
            'debit': debit,
            'credit': credit,
        }
        if distribution:
            vals['analytic_distribution'] = distribution
        return (0, 0, vals)

    def _je_log(self, move, unplaced, unmapped):
        """Say in the chatter what the entry could not do properly.

        Both of these are quiet money problems: money with no analytic account
        to land on drops out of every cost report, and an attachment type
        nobody mapped posts under Salary Adjustment. Neither raises, because
        the salary still has to be paid - but neither may pass unrecorded.
        """
        self.ensure_one()
        body = [self.env._(
            "Journal entry %s created for this batch.", move.display_name)]
        if self.company_id.currency_id.compare_amounts(unplaced, 0) > 0:
            body.append(self.env._(
                "%(amount)s reached no project and no employee analytic "
                "account, so it carries no cost dimension. Employees are "
                "linked to their analytic account on the employee form.",
                amount=unplaced))
        if unmapped:
            body.append(self.env._(
                "These attachment types have no Journal Entry Leg set and "
                "posted under Salary Adjustment: %s.", ", ".join(unmapped)))
        self.message_post(body="<br/>".join(body))

    def action_view_journal_entry(self):
        self.ensure_one()
        if not self.move_id:
            raise UserError(self.env._("This batch has no journal entry yet."))
        return {
            'type': 'ir.actions.act_window',
            'name': self.env._("Journal Entry"),
            'res_model': 'account.move',
            'res_id': self.move_id.id,
            'view_mode': 'form',
        }


class ConstructionSalaryBatchProject(models.Model):
    _name = 'construction.salary.batch.project'
    _description = "Salary Batch Project Distribution"
    _order = 'batch_id, id'

    batch_id = fields.Many2one(
        'construction.salary.batch', string="Batch", required=True, ondelete='cascade', index=True,
    )
    project_id = fields.Many2one('project.project', string="Project", required=True)
    currency_id = fields.Many2one(related='batch_id.currency_id', store=True)

    total_days = fields.Float(string="Attendance Days")
    total_overtime_reg = fields.Float(string="OT hrs - Regular")
    total_overtime_off = fields.Float(string="OT hrs - Off")

    total_salaries = fields.Monetary(string="Total Salaries")
    ot_reg_amount = fields.Monetary(string="OT Regular Amount")
    ot_off_amount = fields.Monetary(string="OT Off Amount")
    total_overtime_amount = fields.Monetary(
        string="Total Overtime", compute='_compute_totals', store=True)
    total_amount = fields.Monetary(
        string="Total Cost", compute='_compute_totals', store=True)
    share = fields.Float(
        string="Share %", compute='_compute_share', store=True,
        help="This project's share of the batch's total project cost.",
    )

    # Same figures once the advanced days are attributed to the projects the
    # employees were on, so nothing worked in the period sits outside a project.
    allocated_days = fields.Float(string="Allocated Days")
    allocated_salaries = fields.Monetary(string="Allocated Salaries")
    allocated_total_amount = fields.Monetary(
        string="Allocated Cost", compute='_compute_totals', store=True)
    allocated_share = fields.Float(
        string="Allocated Share %", compute='_compute_share', store=True)

    @api.depends('total_salaries', 'allocated_salaries',
                 'ot_reg_amount', 'ot_off_amount')
    def _compute_totals(self):
        for rec in self:
            rec.total_overtime_amount = rec.ot_reg_amount + rec.ot_off_amount
            rec.total_amount = rec.total_salaries + rec.total_overtime_amount
            rec.allocated_total_amount = (
                rec.allocated_salaries + rec.total_overtime_amount)

    @api.depends('total_amount', 'allocated_total_amount',
                 'batch_id.project_ids.total_amount',
                 'batch_id.project_ids.allocated_total_amount')
    def _compute_share(self):
        for rec in self:
            lines = rec.batch_id.project_ids
            total = sum(lines.mapped('total_amount'))
            rec.share = 100.0 * rec.total_amount / total if total else 0.0
            allocated = sum(lines.mapped('allocated_total_amount'))
            rec.allocated_share = (
                100.0 * rec.allocated_total_amount / allocated if allocated else 0.0)
