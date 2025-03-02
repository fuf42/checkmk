#!/usr/bin/env python3
# Copyright (C) 2019 Checkmk GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.

from collections.abc import Collection

from cmk.utils.password_store import Password

from cmk.gui.groups import load_contact_group_information
from cmk.gui.htmllib.html import html
from cmk.gui.i18n import _
from cmk.gui.logged_in import user
from cmk.gui.table import Table
from cmk.gui.type_defs import PermissionName
from cmk.gui.valuespec import (
    Alternative,
    DictionaryEntry,
    DropdownChoice,
    DualListChoice,
    FixedValue,
)
from cmk.gui.valuespec import Password as PasswordValuespec
from cmk.gui.valuespec import ValueSpec
from cmk.gui.watolib.config_domain_name import ABCConfigDomain
from cmk.gui.watolib.config_domains import ConfigDomainCore
from cmk.gui.watolib.mode import ModeRegistry, WatoMode
from cmk.gui.watolib.password_store import PasswordStore
from cmk.gui.watolib.passwords import sorted_contact_group_choices

from ._simple_modes import SimpleEditMode, SimpleListMode, SimpleModeType


def register(mode_registry: ModeRegistry) -> None:
    mode_registry.register(ModePasswords)
    mode_registry.register(ModeEditPassword)


class PasswordStoreModeType(SimpleModeType[Password]):
    def type_name(self) -> str:
        return "password"

    def name_singular(self) -> str:
        return _("password")

    def is_site_specific(self) -> bool:
        return False

    def can_be_disabled(self) -> bool:
        return False

    def affected_config_domains(self) -> list[type[ABCConfigDomain]]:
        return [ConfigDomainCore]


class ModePasswords(SimpleListMode):
    @classmethod
    def name(cls) -> str:
        return "passwords"

    @staticmethod
    def static_permissions() -> Collection[PermissionName]:
        return ["passwords"]

    def __init__(self) -> None:
        super().__init__(
            mode_type=PasswordStoreModeType(),
            store=PasswordStore(),
        )
        self._contact_groups = load_contact_group_information()

    def title(self) -> str:
        return _("Passwords")

    def _table_title(self) -> str:
        return _("Passwords")

    def _delete_confirm_message(self) -> str:
        return " ".join(
            [
                _(
                    "<b>Beware:</b> The password may be used in checks. If you "
                    "delete the password, the checks won't be able to "
                    "authenticate with this password anymore."
                ),
                super()._delete_confirm_message(),
            ]
        )

    def page(self) -> None:
        html.p(
            _(
                "This password management module stores the passwords you use in your checks and "
                "special agents in a central place. Please note that this password store is no "
                "kind of password safe. Your passwords will not be encrypted."
            )
        )
        html.p(
            _(
                "All the passwords you store in your monitoring configuration, "
                "including this password store, are needed in plain text to contact remote systems "
                "for monitoring. So all those passwords have to be stored readable by the monitoring."
            )
        )
        super().page()

    def _show_entry_cells(self, table: Table, ident: str, entry: Password) -> None:
        table.cell(_("ID"), ident)
        table.cell(_("Title"), entry["title"])
        table.cell(_("Editable by"))
        if entry["owned_by"] is None:
            html.write_text(
                _('Administrators (having the permission "Write access to all passwords")')
            )
        else:
            html.write_text(self._contact_group_alias(entry["owned_by"]))
        table.cell(_("Shared with"))
        if not entry["shared_with"]:
            html.write_text(_("Not shared"))
        else:
            html.write_text(", ".join([self._contact_group_alias(g) for g in entry["shared_with"]]))

    def _contact_group_alias(self, name: str) -> str:
        return self._contact_groups.get(name, {"alias": name})["alias"]


class ModeEditPassword(SimpleEditMode):
    @classmethod
    def name(cls) -> str:
        return "edit_password"

    @staticmethod
    def static_permissions() -> Collection[PermissionName]:
        return ["passwords"]

    @classmethod
    def parent_mode(cls) -> type[WatoMode] | None:
        return ModePasswords

    def __init__(self) -> None:
        super().__init__(
            mode_type=PasswordStoreModeType(),
            store=PasswordStore(),
        )

    def _vs_individual_elements(self) -> list[DictionaryEntry]:
        if user.may("wato.edit_all_passwords"):
            admin_element: list[ValueSpec] = [
                FixedValue(
                    value=None,
                    title=_("Administrators"),
                    totext=_(
                        'Administrators (having the permission "Write access to all passwords")'
                    ),
                )
            ]
        else:
            admin_element = []

        elements: list[DictionaryEntry] = [
            (
                "password",
                PasswordValuespec(
                    title=_("Password"),
                    allow_empty=False,
                    size=32,
                ),
            ),
            (
                "owned_by",
                Alternative(
                    title=_("Editable by"),
                    help=_(
                        "Each password is owned by a group of users which are able to edit, "
                        "delete and use existing passwords."
                    ),
                    elements=admin_element
                    + [
                        DropdownChoice(
                            title=_("Members of the contact group:"),
                            choices=lambda: sorted_contact_group_choices(only_own=True),
                            invalid_choice="complain",
                            empty_text=_(
                                "You need to be member of at least one contact group to be able to "
                                "create a password."
                            ),
                            invalid_choice_title=_("Group not existant or not member"),
                            invalid_choice_error=_(
                                "The choosen group is either not existant "
                                "anymore or you are not a member of this "
                                "group. Please choose another one."
                            ),
                        ),
                    ],
                ),
            ),
            (
                "shared_with",
                DualListChoice(
                    title=_("Share with"),
                    help=_(
                        "By default only the members of the owner contact group are permitted "
                        "to use a a configured password. It is possible to share a password with "
                        "other groups of users to make them able to use a password in checks."
                    ),
                    choices=sorted_contact_group_choices,
                    autoheight=False,
                    size=43,
                ),
            ),
        ]

        return elements
