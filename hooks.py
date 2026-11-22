# -*- coding: utf-8 -*-
import logging

from .models.utils import link_groups_to_category

_logger = logging.getLogger(__name__)

# The eight groups declared in security/construction_payroll_groups.xml. Their
# heading on the user form is set from here rather than in the XML, because
# 18.0 and 19.0 spell that link differently - see link_groups_to_category.
PAYROLL_GROUP_XMLIDS = (
    'construction_payroll.group_labour_attendance',
    'construction_payroll.group_labour_attendance_manager',
    'construction_payroll.group_staff_attendance',
    'construction_payroll.group_staff_attendance_manager',
    'construction_payroll.group_labour_payroll',
    'construction_payroll.group_labour_payroll_manager',
    'construction_payroll.group_staff_payroll',
    'construction_payroll.group_staff_payroll_manager',
)


def categorise_payroll_groups(env):
    """Put the eight payroll groups under the Construction Payroll heading."""
    link_groups_to_category(
        env,
        'construction_payroll.module_category_construction_payroll',
        PAYROLL_GROUP_XMLIDS,
        "Construction Payroll",
    )


def post_init_hook(env):
    """Group the payroll access rights under one heading on the user form."""
    try:
        categorise_payroll_groups(env)
    except Exception:  # pragma: no cover
        _logger.exception("construction_payroll: grouping the access rights failed.")
