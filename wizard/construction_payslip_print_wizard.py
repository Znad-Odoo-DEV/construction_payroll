# -*- coding: utf-8 -*-
from odoo import api, fields, models

# The report carries its own two languages rather than leaning on installed
# translations: the Arabic copy has to print the same on a database where
# Arabic was never loaded.
LABELS = {
    'en': {
        'title': "PAY SLIP", 'paid': "PAID",
        'employee': "Employee", 'employee_id': "Employee ID",
        'attendance_id': "Attendance ID", 'designation': "Designation",
        'company': "Company", 'batch': "Batch", 'sheet': "Attendance Sheet",
        'period': "Period", 'days_in_month': "Days in Month",
        'contract': "Contract Details", 'basic': "Basic Salary",
        'house': "House Allowance", 'transport': "Travelling Allowance",
        'other': "Other Allowances", 'gross': "Total Gross Salary",
        'rate_day': "Rate per Day", 'ot_rate_reg': "OT Rate - Regular Days",
        'ot_rate_off': "OT Rate - Off Days",
        'work': "Salary & Work Entries", 'item': "Description",
        'qty': "Days / Hours", 'amount': "Amount",
        'attendance_days': "Total Attendance (days)",
        'advance_in': "of which advance days",
        'salary_month': "Total Salary of this Month",
        'ot_reg': "Overtime - Regular Days (h)",
        'ot_off': "Overtime - Off Days (h)",
        'ot_total': "Total Overtime Salary",
        'attachments': "Salary Attachments", 'type': "Type",
        'adjustment': "Salary Adjustment", 'net': "NET AMOUNT",
        'projects': "Project Distribution", 'project': "Project",
        'days': "Days", 'salary': "Salary", 'ot_reg_h': "OT Reg (h)",
        'ot_off_h': "OT Off (h)", 'overtime': "Overtime", 'total': "Total",
        'share': "Share", 'chart': "Attendance by Project",
        'notes': "Notes",
        'att_title': "ATTENDANCE REPORT", 'date': "Date", 'day': "Day",
        'check_in': "Check In", 'check_out': "Check Out",
        'worked_hours': "Worked Hours", 'day_earned': "Day Earned",
        'status': "Status", 'off_day': "Off Day",
        'missing': "Missing Check-Out", 'yes': "Yes",
        'present_days': "Days Attended", 'off_days': "Off Days",
        'absent_days': "Absent Days", 'missing_days': "Missing Check-Outs",
        'no_days': "No attendance lines for this period.",
    },
    'ar': {
        'title': "قسيمة راتب", 'paid': "مدفوع",
        'employee': "الموظف", 'employee_id': "رقم الموظف",
        'attendance_id': "رقم الحضور", 'designation': "المسمى الوظيفي",
        'company': "الشركة", 'batch': "الدفعة", 'sheet': "كشف الحضور",
        'period': "الفترة", 'days_in_month': "أيام الشهر",
        'contract': "تفاصيل العقد", 'basic': "الراتب الأساسي",
        'house': "بدل السكن", 'transport': "بدل التنقل",
        'other': "بدلات أخرى", 'gross': "إجمالي الراتب",
        'rate_day': "أجر اليوم", 'ot_rate_reg': "أجر ساعة الإضافي - أيام العمل",
        'ot_rate_off': "أجر ساعة الإضافي - أيام العطل",
        'work': "الراتب وأيام العمل", 'item': "البيان",
        'qty': "أيام / ساعات", 'amount': "المبلغ",
        'attendance_days': "إجمالي الحضور (أيام)",
        'advance_in': "منها أيام مدفوعة مقدماً",
        'salary_month': "راتب الشهر",
        'ot_reg': "العمل الإضافي - أيام العمل (ساعة)",
        'ot_off': "العمل الإضافي - أيام العطل (ساعة)",
        'ot_total': "إجمالي أجر العمل الإضافي",
        'attachments': "الإضافات والاستقطاعات", 'type': "النوع",
        'adjustment': "صافي الإضافات والاستقطاعات", 'net': "صافي الراتب",
        'projects': "التوزيع على المشاريع", 'project': "المشروع",
        'days': "الأيام", 'salary': "الراتب", 'ot_reg_h': "إضافي عمل (س)",
        'ot_off_h': "إضافي عطل (س)", 'overtime': "الإضافي", 'total': "الإجمالي",
        'share': "النسبة", 'chart': "الحضور حسب المشاريع",
        'notes': "ملاحظات",
        'att_title': "تقرير الحضور", 'date': "التاريخ", 'day': "اليوم",
        'check_in': "الدخول", 'check_out': "الخروج",
        'worked_hours': "ساعات العمل", 'day_earned': "اليوم المحتسب",
        'status': "الحالة", 'off_day': "يوم عطلة",
        'missing': "خروج غير مسجل", 'yes': "نعم",
        'present_days': "أيام الحضور", 'off_days': "أيام العطل",
        'absent_days': "أيام الغياب", 'missing_days': "خروج غير مسجل",
        'no_days': "لا توجد سجلات حضور لهذه الفترة.",
    },
}
AR_DAYS = ["الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]
AR_MONTHS = {
    'JAN': "يناير", 'FEB': "فبراير", 'MAR': "مارس", 'APR': "أبريل",
    'MAY': "مايو", 'JUN': "يونيو", 'JUL': "يوليو", 'AUG': "أغسطس",
    'SEP': "سبتمبر", 'OCT': "أكتوبر", 'NOV': "نوفمبر", 'DEC': "ديسمبر",
}
AR_STATUS = {
    'Present': "حاضر", 'Partial': "حضور جزئي", 'Absent': "غائب",
    'Off Day': "يوم عطلة", 'Holiday': "عطلة رسمية",
    'Sick Leave': "إجازة مرضية", 'On Leave': "إجازة",
    'Worked during leave': "عمل أثناء الإجازة", 'Not Counted': "غير محتسب",
}
PALETTE = [
    '#714B67', '#017E84', '#F06050', '#6EC1A7', '#F4A261',
    '#5B8FF9', '#B37FEB', '#E8684A', '#5AD8A6', '#945FB9',
]


class ConstructionPayslipPrintWizard(models.TransientModel):
    _name = 'construction.payslip.print.wizard'
    _description = "Print Payslip"

    payslip_ids = fields.Many2many('construction.payslip', string="Payslips")
    content = fields.Selection(
        [('payslip', "Payslip only"),
         ('attendance', "Payslip with attendance report")],
        string="Print", required=True, default='payslip')
    lang = fields.Selection(
        [('en', "English"), ('ar', "العربية")],
        string="Language", required=True, default='en')

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        ctx = self.env.context
        if 'payslip_ids' in fields_list and ctx.get('active_model') == 'construction.payslip':
            res['payslip_ids'] = [(6, 0, ctx.get('active_ids') or [])]
        return res

    def action_print(self):
        self.ensure_one()
        # The ids travel in data: a report called with data reaches
        # _get_report_values with no docids of its own.
        return self.env.ref('construction_payroll.action_report_construction_payslip').report_action(
            self.payslip_ids, data={
                'ids': self.payslip_ids.ids,
                'with_attendance': self.content == 'attendance',
                'lang': self.lang,
            })


class ReportConstructionPayslip(models.AbstractModel):
    _name = 'report.construction_payroll.report_construction_payslip'
    _description = "Payslip Report"

    @api.model
    def _get_report_values(self, docids, data=None):
        data = data or {}
        docs = self.env['construction.payslip'].browse(docids or data.get('ids') or [])
        # Read access is the gate: the ids in data come from the client.
        docs.check_access('read')
        lang = 'ar' if data.get('lang') == 'ar' else 'en'
        with_attendance = bool(data.get('with_attendance'))
        return {
            'doc_ids': docs.ids,
            'doc_model': 'construction.payslip',
            'docs': docs,
            'L': LABELS[lang],
            'ar': lang == 'ar',
            'with_attendance': with_attendance,
            'info': {slip.id: self._slip_info(slip, lang, with_attendance)
                     for slip in docs},
        }

    def _slip_info(self, slip, lang, with_attendance):
        ar = lang == 'ar'
        month = dict(slip._fields['month'].selection).get(slip.month) or ''
        if ar:
            month = AR_MONTHS.get(month[:3].upper(), month)
        projects = slip.project_ids.sorted('total_amount', reverse=True)
        widest = max(projects.mapped('total_amount') or [0.0])
        chart = [{
            'name': p.project_id.display_name,
            'days': p.total_attendance,
            'share': p.share,
            'amount': p.total_amount,
            'width': round(100.0 * p.total_amount / widest, 2) if widest else 0.0,
            'colour': PALETTE[i % len(PALETTE)],
        } for i, p in enumerate(projects) if p.total_amount]
        info = {
            'period_title': ("%s %s" % (month, slip.year or '')).strip(),
            'projects': projects,
            'chart': chart,
            'days': [],
            'totals': {},
        }
        if not with_attendance:
            return info
        tz_rec = slip.with_context(
            tz=slip.employee_id.tz or self.env.user.tz or 'UTC')

        def hhmm(dt):
            return (fields.Datetime.context_timestamp(tz_rec, dt).strftime('%H:%M')
                    if dt else '')

        days = slip._period_days().sorted('date')
        for d in days:
            info['days'].append({
                'date': d.date,
                'day': (AR_DAYS[d.date.weekday()] if ar and d.date
                        else d.day_name or (d.date and d.date.strftime('%A')) or ''),
                'project': d.project_id.display_name or '',
                'check_in': hhmm(d.check_in),
                'check_out': '' if d.missing_checkout else hhmm(d.check_out),
                'worked_hours': d.worked_hours,
                'overtime': d.overtime,
                'day_ratio': d.day_ratio,
                'status': (AR_STATUS.get(d.status, d.status) if ar else d.status) or '',
                'is_off_day': d.is_off_day,
                'missing_checkout': d.missing_checkout,
            })
        info['totals'] = {
            'worked_hours': sum(days.mapped('worked_hours')),
            'overtime': sum(days.mapped('overtime')),
            'day_ratio': sum(days.mapped('day_ratio')),
            'present': len(days.filtered(lambda d: d.day_ratio > 0)),
            'off': len(days.filtered('is_off_day')),
            'absent': len(days.filtered(lambda d: d.status == 'Absent')),
            'missing': len(days.filtered('missing_checkout')),
        }
        return info
