# -*- coding: utf-8 -*-
{
    'name': 'Construction Payroll',
    'version': '19.0.1.0.0',
    'summary': 'Attendance-driven payroll for construction labour and staff: '
               'payslips, overtime, project cost split, salary batches and '
               'employee expenses',
    'description': """
Construction Payroll
====================
Attendance-driven payroll for construction companies, built on Odoo
Enterprise Payroll and the UAE payroll localisation.

* Company payroll configuration (code, abbreviation, period start day, weekly
  off day, overtime premiums, salary journal)
* Employee classification (labour, project staff, office staff), overtime
  terms and an analytic account per employee
* Monthly attendance sheets for labour (from hr.attendance and hr.leave) and
  for office staff, with advance-day review
* Payslips with daily lines, overtime and a per-project cost split
* Salary batches with a journal entry carrying project and employee analytics
* Advances, fines, leave and ticket expenses, on-hold amounts, end of service
* Salary attachment types and public holidays
* Payroll dashboard, security groups and multi-company record rules
""",
    'author': 'Znad-Odoo-DEV',
    'website': 'https://github.com/Znad-Odoo-DEV/construction_payroll',
    'category': 'Human Resources/Payroll',
    'license': 'LGPL-3',
    # hr_payroll defines registration_number on hr.employee and
    # l10n_ae_hr_payroll the four allowance columns on hr.version; both are
    # read by related fields here, which the ORM resolves at load, so both
    # must be loaded first. ``project`` and ``account`` are needed because
    # fields point at project.project, account.move, account.journal and
    # account.account, which cannot be resolved from an optional module.
    'depends': ['base', 'mail', 'hr', 'hr_payroll', 'l10n_ae_hr_payroll', 'project', 'account'],
    'data': [
        'security/construction_payroll_groups.xml',
        'security/ir.model.access.csv',
        'security/construction_payroll_record_rules.xml',
        # Before the attachment types: each of those points at an expense
        # type by xmlid, and a ref cannot resolve forwards.
        'data/construction_expense_type_data.xml',
        'data/construction_attachment_type_data.xml',
        'data/construction_sequence_data.xml',
        'views/res_company_views.xml',
        'views/hr_employee_views.xml',
        'views/construction_public_holiday_views.xml',
        'views/construction_expense_type_views.xml',
        'views/construction_attachment_type_views.xml',
        'views/construction_attendance_sheet_views.xml',
        'views/construction_staff_attendance_views.xml',
        'views/construction_attachment_views.xml',
        'wizard/construction_payslip_generate_wizard_views.xml',
        'wizard/construction_payslip_print_wizard_views.xml',
        'views/construction_payslip_views.xml',
        'wizard/construction_payslip_advance_wizard_views.xml',
        'views/construction_salary_batch_views.xml',
        'views/construction_expenses_views.xml',
        'views/construction_dashboard_views.xml',
        'report/construction_payslip_report.xml',
        'report/construction_salary_batch_report.xml',
        'views/construction_payroll_menus.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'construction_payroll/static/src/scss/dashboard.scss',
            'construction_payroll/static/src/js/dashboard.js',
            'construction_payroll/static/src/xml/dashboard.xml',
        ],
    },
    'post_init_hook': 'post_init_hook',
    'application': True,
    'installable': True,
}
