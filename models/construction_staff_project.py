# -*- coding: utf-8 -*-
from odoo import api, fields, models
from odoo.exceptions import ValidationError


class ConstructionStaffProject(models.Model):
    _name = 'construction.staff.project'
    _description = "Staff Project Distribution"
    _order = 'employee_id, id'

    employee_id = fields.Many2one(
        'hr.employee', string="Employee",
        required=True, ondelete='cascade', index=True,
    )
    # The site this share of the salary belongs to.
    project_id = fields.Many2one(
        'project.project', string="Project",
        required=True, ondelete='cascade',
    )
    percentage = fields.Float(
        string="Percentage",
        help="Share of the salary / attendance / overtime allocated to this "
             "project. Leave the whole distribution empty to split equally "
             "over all on-going construction projects.",
    )
    company_id = fields.Many2one(
        related='employee_id.company_id', string="Company", store=True,
    )

    @api.constrains('percentage')
    def _check_percentage(self):
        for line in self:
            if line.percentage < 0 or line.percentage > 100:
                raise ValidationError(
                    self.env._("Project percentage must be between 0 and 100.")
                )
