# Construction Payroll

Attendance-driven payroll for construction companies, for Odoo 19 Enterprise.
It sits on top of Odoo Payroll and the UAE payroll localisation and adds the
monthly flow construction labour and staff need: attendance in, payslips and
salary batches out.

## What it does

- **Attendance sheets** for labour (built from `hr.attendance` and `hr.leave`)
  and for office staff, with public holidays, the weekly off day, sick-leave
  pay ratios and a review step for advance days.
- **Payslips** with daily lines, regular and off-day overtime, and a
  per-project cost split of salary and overtime.
- **Overtime rates** derived from the basic wage and company-level factors,
  not typed per employee.
- **Salary batches** (WPS or cash) that post one journal entry, carrying the
  project and, optionally, an employee analytic account.
- **Employee expenses**: advances with installments, fines and deductions,
  leave and ticket expenses, on-hold amounts and end-of-service settlements.
- **Salary attachment types** (additions and deductions) and Salary
  Adjustments picked up by the payslip.
- **Payroll dashboard** with KPI cards and charts.
- **Security**: separate Officer and Manager groups for labour and staff
  payroll and attendance, plus multi-company record rules.

## Requirements

- Odoo 19.0 Enterprise
- Modules: `hr_payroll`, `l10n_ae_hr_payroll`, `project`, `account`

## Installation

1. Put the `construction_payroll` folder in your addons path.
2. Update the apps list and install **Construction Payroll**.
3. On each company, set the payroll code, period start day and salary journal.
4. Under *Configuration*, set the expense types' accounts per company.

## License

LGPL-3
