# -*- coding: utf-8 -*-
from datetime import date

from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, new_test_user, tagged

from odoo.addons.construction_payroll.models.construction_attachment import ADJUSTMENTS_FROM_PARAM


@tagged('post_install', '-at_install', 'construction_salary_adjustments')
class TestSalaryAdjustmentLines(TransactionCase):
    """A Salary Adjustment on the employee puts its month on construction.payslip and
    records what the payslip paid."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env['ir.config_parameter'].sudo().set_param(ADJUSTMENTS_FROM_PARAM, False)
        cls.company = cls.env.company
        cls.employee = cls.env['hr.employee'].create({
            'name': "Adjustment Test Employee", 'company_id': cls.company.id})
        InputType = cls.env['hr.payslip.input.type']
        cls.advance_input = InputType.create({'name': "Test Advanced Salary", 'code': 'TSTADV'})
        cls.other_input = InputType.create({'name': "Test Allowance", 'code': 'TSTALW'})
        cls.deduction = cls.env.ref('construction_payroll.attachment_type_advance_deduction')
        cls.deduction.input_type_ids = [Command.set(cls.advance_input.ids)]
        cls.Row = cls.env['construction.attachment']

    def _adjustment(self, **vals):
        return self.env['hr.salary.attachment'].create(dict({
            'employee_ids': [Command.set(self.employee.ids)],
            'company_id': self.company.id,
            'other_input_type_id': self.advance_input.id,
            'monthly_amount': 300.0,
            'total_amount': 1000.0,
            'duration_type': 'limited',
            'is_refund': True,
            'date_start': date(2026, 11, 1),
        }, **vals))

    def _month(self, month, year='2026'):
        return self.Row._construction_lines_from_adjustments(self.employee, month, year)

    def test_limited_adjustment_is_paid_back_and_closes(self):
        adjustment = self._adjustment()
        nov = self._month('NOV')
        self.assertEqual(len(nov), 1)
        self.assertEqual(nov.value, 300.0)
        self.assertEqual(nov.type_id, self.deduction)
        self.assertEqual(nov.signed_value, -300.0)
        self.assertEqual(nov.salary_adjustment_id, adjustment)
        self.assertFalse(self._month('NOV'), "a second call must not double the month")

        nov.state = 'paid'
        self.assertEqual(adjustment.paid_amount, 300.0)
        for month in ('DEC',):
            self._month(month).state = 'paid'
        for month in ('JAN', 'FEB'):
            row = self._month(month, '2027')
            if month == 'FEB':
                self.assertEqual(row.value, 100.0, "the last month takes only what is left")
            row.state = 'paid'
        self.assertEqual(adjustment.paid_amount, 1000.0)
        self.assertEqual(adjustment.state, 'close')
        self.assertFalse(self._month('MAR', '2027'))

    def test_a_rounding_leftover_is_no_line_and_closes(self):
        adjustment = self._adjustment(monthly_amount=333.33, total_amount=1000.0)
        for month, year in (('NOV', '2026'), ('DEC', '2026'), ('JAN', '2027')):
            row = self._month(month, year)
            self.assertEqual(row.value, 333.33)
            row.state = 'paid'
        self.assertEqual(adjustment.state, 'close', "0.01 left is rounding, not a debt")
        self.assertFalse(self._month('FEB', '2027'))

    def test_unpaid_months_count_against_the_total(self):
        self._adjustment(total_amount=500.0)
        self.assertEqual(self._month('NOV').value, 300.0)
        self.assertEqual(self._month('DEC').value, 200.0)
        self.assertFalse(self._month('JAN', '2027'))

    def test_unlimited_addition_without_a_mapping(self):
        adjustment = self._adjustment(
            other_input_type_id=self.other_input.id, duration_type='unlimited',
            total_amount=0.0, is_refund=False, monthly_amount=50.0)
        adjustment.description = "test allowance"
        nov = self._month('NOV')
        self.assertEqual(nov.name, "Test Allowance - NOV-2026", "a note that repeats the type is said once")
        self.assertEqual(nov.type_id, self.env.ref('construction_payroll.attachment_type_salary_addition'))
        self.assertEqual(nov.signed_value, 50.0)
        nov.state = 'paid'
        self.assertEqual(adjustment.state, 'open', "an unlimited adjustment never closes by payment")
        self.assertEqual(self._month('DEC').value, 50.0)

    def test_negative_adjustment_on_an_addition_type(self):
        pension = self.env['construction.attachment.type'].create({
            'name': "Test Pension", 'kind': 'addition',
            'input_type_ids': [Command.set(self.other_input.ids)]})
        adjustment = self._adjustment(
            other_input_type_id=self.other_input.id, duration_type='unlimited',
            total_amount=0.0, is_refund=True, monthly_amount=660.0)
        nov = self._month('NOV')
        self.assertEqual(nov.type_id, pension)
        self.assertEqual(nov.signed_value, -660.0, "it must deduct, whatever the type's sign")
        nov.state = 'paid'
        self.assertEqual(adjustment.paid_amount, 660.0)

    def test_a_month_changed_by_hand_survives_regeneration(self):
        adjustment = self._adjustment()
        nov = self._month('NOV')
        nov.value = 0.0
        self.Row._clear_generated_for(self.employee, 'NOV', '2026')
        self.assertTrue(nov.exists(), "the skipped month is a decision, not the engine's row")
        self.assertFalse(self._month('NOV'), "and no full installment comes back beside it")
        nov.state = 'paid'
        self.assertEqual(adjustment.paid_amount, 0.0)
        self.assertEqual(self._month('DEC').value, 300.0, "what was skipped is still owed")

    def _manager(self):
        return new_test_user(
            self.env, login='construction_adj_manager', company_id=self.company.id,
            groups='base.group_user,construction_payroll.group_labour_payroll_manager')

    def test_a_paid_line_is_locked_for_people(self):
        self._adjustment()
        nov = self._month('NOV')
        nov.state = 'paid'
        mine = nov.with_user(self._manager())
        with self.assertRaises(UserError):
            mine.write({'value': 100.0})
        with self.assertRaises(UserError):
            mine.write({'state': 'attached'})
        with self.assertRaises(UserError):
            mine.unlink()
        mine.write({'value': 300.0, 'state': 'paid'})  # saying what it already says is no change

    def test_an_adjustment_month_keeps_its_month_and_is_eased_not_deleted(self):
        adjustment = self._adjustment()
        mine = self._month('NOV').with_user(self._manager())
        with self.assertRaises(UserError):
            mine.write({'month': 'DEC'})
        with self.assertRaises(UserError):
            mine.unlink()
        mine.write({'value': 120.0})
        mine.write({'state': 'paid'})
        self.assertEqual(adjustment.paid_amount, 120.0)

    def test_the_advance_follows_its_adjustment(self):
        advance = self.env['construction.advance'].create({
            'employee_id': self.employee.id, 'total_amount': 1000.0,
            'installment_ids': [Command.create({
                'name': "Installment no.%02d" % i, 'amount': 500.0,
                'due_date': date(2026, 10 + i, 1)}) for i in (1, 2)],
        })
        advance._construction_open_salary_adjustment()
        nov = self._month('NOV')
        nov.value = 200.0
        nov.state = 'paid'
        self.assertEqual(advance.outstanding_amount, 800.0,
                         "the month took 200, whatever the installment's tick says")
        self.assertEqual(advance.state, 'running')
        self._month('DEC').state = 'paid'
        row = self._month('JAN', '2027')
        self.assertEqual(row.value, 300.0)
        row.state = 'paid'
        self.assertEqual(advance.outstanding_amount, 0.0)
        self.assertEqual(advance.state, 'closed')
        row.state = 'attached'
        self.assertEqual(advance.state, 'running', "a payment taken back opens it again")
        self.assertEqual(advance.outstanding_amount, 300.0)

    def test_one_time_lands_once(self):
        self._adjustment(duration_type='one', monthly_amount=100.0, total_amount=100.0)
        self.assertEqual(len(self._month('NOV')), 1)
        self.assertFalse(self._month('DEC'))

    def test_nothing_before_the_start_month(self):
        self.env['ir.config_parameter'].sudo().set_param(ADJUSTMENTS_FROM_PARAM, '2026-12')
        self._adjustment()
        self.assertFalse(self._month('NOV'))
        self.assertEqual(len(self._month('DEC')), 1)

    def test_a_row_typed_by_hand_is_taken_not_doubled(self):
        adjustment = self._adjustment()
        hand = self.Row.create({
            'name': "Advance deduction typed by hand", 'employee_id': self.employee.id,
            'type_id': self.deduction.id, 'month': 'NOV', 'year': '2026', 'value': 300.0})
        self.assertEqual(self._month('NOV'), hand)
        self.assertEqual(hand.salary_adjustment_id, adjustment)
        self.assertEqual(self.Row.search_count([
            ('employee_id', '=', self.employee.id), ('month', '=', 'NOV'), ('year', '=', '2026')]), 1)

    def test_undoing_a_payment_reopens(self):
        adjustment = self._adjustment(total_amount=300.0)
        nov = self._month('NOV')
        nov.state = 'paid'
        self.assertEqual(adjustment.state, 'close')
        nov.state = 'attached'
        self.assertEqual(adjustment.paid_amount, 0.0)
        self.assertEqual(adjustment.state, 'open')

    def test_advance_is_deducted_through_its_adjustment(self):
        advance = self.env['construction.advance'].create({
            'employee_id': self.employee.id,
            'total_amount': 1500.0,
            'installment_ids': [Command.create({
                'name': "Installment no.%02d" % i, 'amount': 500.0,
                'due_date': date(2026, 11 + i, 1) if i < 2 else date(2027, i - 1, 1)})
                for i in (1, 2, 3)],
        })
        self.assertFalse(advance._construction_open_salary_adjustment())
        adjustment = advance.salary_adjustment_id
        self.assertEqual(adjustment.total_amount, 1500.0)
        self.assertEqual(adjustment.monthly_amount, 500.0)
        self.assertTrue(adjustment.is_refund)
        self.assertEqual(adjustment.date_start, date(2026, 12, 1))
        self.assertEqual(advance.action_generate_deductions(), 0,
                         "the adjustment deducts; nothing is scheduled ahead")
        self.assertFalse(self._month('NOV'), "nothing before the first installment's month")
        dec = self._month('DEC')
        self.assertEqual(dec.advance_id, advance)
        self.assertEqual(dec.installment_id, advance.installment_ids.sorted('due_date')[0])
        dec.state = 'paid'
        self.assertTrue(dec.installment_id.paid)
        self.assertEqual(adjustment.paid_amount, 500.0)

    def test_advance_without_a_type_is_handed_back(self):
        self.deduction.input_type_ids = [Command.clear()]
        advance = self.env['construction.advance'].create({
            'employee_id': self.employee.id, 'total_amount': 600.0, 'no_of_installments': 2})
        self.assertEqual(advance._construction_open_salary_adjustment(), advance)
        self.assertFalse(advance.salary_adjustment_id)

    def test_payroll_officer_without_payroll_rights(self):
        officer = new_test_user(
            self.env, login='construction_adj_officer', company_id=self.company.id,
            groups='base.group_user,construction_payroll.group_labour_payroll')
        self.assertFalse(officer.has_group('hr_payroll.group_hr_payroll_user'))
        adjustment = self._adjustment(total_amount=300.0)
        nov = self.Row.with_user(officer)._construction_lines_from_adjustments(self.employee, 'NOV', '2026')
        self.assertEqual(nov.env.uid, officer.id)
        nov.write({'state': 'paid'})
        self.assertEqual(adjustment.paid_amount, 300.0)
        self.assertEqual(adjustment.state, 'close')
