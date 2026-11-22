# -*- coding: utf-8 -*-
"""Shared helpers for the Construction Payroll module."""
MONTH_ABBR = {
    1: 'JAN', 2: 'FEB', 3: 'MAR', 4: 'APR', 5: 'MAY', 6: 'JUN',
    7: 'JUL', 8: 'AUG', 9: 'SEP', 10: 'OCT', 11: 'NOV', 12: 'DEC',
}


def ordinal(day):
    """Return the English ordinal suffix for a day number (1 -> st, 2 -> nd)."""
    if 10 <= day % 100 <= 20:
        return 'th'
    return {1: 'st', 2: 'nd', 3: 'rd'}.get(day % 10, 'th')


# ---------------------------------------------------------------------------
# 18.0 / 19.0 compatibility
# ---------------------------------------------------------------------------
# The renamed access fields are resolved by whichever name the running series
# has, rather than hard-coded to one of them.

def group_users(group):
    """Members of ``group``, implied membership included.

    19.0 splits this in two: user_ids is direct membership and all_user_ids
    also covers the groups that imply this one. 18.0 materialises implied
    groups onto the user when they are assigned, so its plain ``users`` already
    answers the same question.
    """
    return group['all_user_ids' if 'all_user_ids' in group._fields else 'users']


def user_groups(user):
    """Every group ``user`` belongs to, implied groups included."""
    return user['all_group_ids' if 'all_group_ids' in user._fields else 'groups_id']


def group_members_field(env):
    """Name of the writable member field on res.groups for this series."""
    return 'user_ids' if 'user_ids' in env['res.groups']._fields else 'users'


def link_groups_to_category(env, category_xmlid, group_xmlids, privilege_name,
                            privilege_sequence=20):
    """Put the given groups under a heading on the user access form.

    19.0 took ir.module.category off res.groups and put a res.groups.privilege
    in between; 18.0 has no such model and links the category directly. This
    cannot be expressed in XML, because a manifest is read with
    ast.literal_eval and so cannot pick a different security file per series.

    It is purely presentational - it decides which heading the access
    checkboxes appear under - so resolving it here, at install and on every
    upgrade, costs nothing and keeps one set of group records for both series.
    """
    category = env.ref(category_xmlid, raise_if_not_found=False)
    if not category:
        return

    groups = env['res.groups'].browse()
    for xmlid in group_xmlids:
        group = env.ref(xmlid, raise_if_not_found=False)
        if group:
            groups |= group
    if not groups:
        return

    if 'privilege_id' not in groups._fields:            # 18.0
        groups.write({'category_id': category.id})
        return

    Privilege = env['res.groups.privilege']             # 19.0
    privilege = Privilege.search([('name', '=', privilege_name)], limit=1)
    if not privilege:
        privilege = Privilege.create({
            'name': privilege_name,
            'sequence': privilege_sequence,
            'category_id': category.id,
        })
    groups.write({'privilege_id': privilege.id})


# System parameter naming the project stage that counts as "on-going".
ONGOING_STAGE_PARAM = 'construction_payroll.ongoing_project_stage_id'


def ongoing_project_stage_id(env):
    """The project stage that counts as on-going, or False when none is set."""
    value = env['ir.config_parameter'].sudo().get_param(ONGOING_STAGE_PARAM)
    return int(value) if value else False
