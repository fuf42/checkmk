#!/usr/bin/env python3
# Copyright (C) 2019 Checkmk GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.

import time
from collections.abc import Sequence
from typing import Any, Literal, Protocol

import livestatus

from cmk.utils.hostaddress import HostName
from cmk.utils.render import SecondsRenderer
from cmk.utils.servicename import ServiceName

import cmk.gui.sites as sites
import cmk.gui.utils as utils
import cmk.gui.utils.escaping as escaping
from cmk.gui.config import active_config
from cmk.gui.exceptions import MKUserError
from cmk.gui.htmllib.foldable_container import foldable_container
from cmk.gui.htmllib.html import html
from cmk.gui.http import request
from cmk.gui.i18n import _, _l, _u, ungettext
from cmk.gui.logged_in import user
from cmk.gui.permissions import (
    Permission,
    PermissionRegistry,
    PermissionSection,
    PermissionSectionRegistry,
)
from cmk.gui.type_defs import Choices, Row, Rows
from cmk.gui.utils.html import HTML
from cmk.gui.utils.speaklater import LazyString
from cmk.gui.utils.urls import makeuri_contextless
from cmk.gui.valuespec import AbsoluteDate, Age, Checkbox, DatePicker, Dictionary, TimePicker
from cmk.gui.watolib.downtime import determine_downtime_mode, DowntimeSchedule

from .base import Command, CommandActionResult, CommandConfirmDialogOptions, CommandSpec
from .group import CommandGroup, CommandGroupRegistry
from .registry import CommandRegistry


def register(
    command_group_registry: CommandGroupRegistry,
    command_registry: CommandRegistry,
    permission_section_registry: PermissionSectionRegistry,
    permission_registry: PermissionRegistry,
) -> None:
    command_group_registry.register(CommandGroupVarious)
    command_group_registry.register(CommandGroupFakeCheck)
    command_group_registry.register(CommandGroupAcknowledge)
    command_group_registry.register(CommandGroupDowntimes)
    command_registry.register(CommandReschedule)
    command_registry.register(CommandNotifications)
    command_registry.register(CommandToggleActiveChecks)
    command_registry.register(CommandTogglePassiveChecks)
    command_registry.register(CommandClearModifiedAttributes)
    command_registry.register(CommandFakeCheckResult)
    command_registry.register(CommandCustomNotification)
    command_registry.register(CommandAcknowledge)
    command_registry.register(CommandAddComment)
    command_registry.register(CommandScheduleDowntimes)
    command_registry.register(CommandRemoveDowntime)
    command_registry.register(CommandRemoveComments)
    permission_section_registry.register(PermissionSectionAction)
    permission_registry.register(PermissionActionReschedule)
    permission_registry.register(PermissionActionNotifications)
    permission_registry.register(PermissionActionEnableChecks)
    permission_registry.register(PermissionActionClearModifiedAttributes)
    permission_registry.register(PermissionActionFakeChecks)
    permission_registry.register(PermissionActionCustomNotification)
    permission_registry.register(PermissionActionAcknowledge)
    permission_registry.register(PermissionActionAddComment)
    permission_registry.register(PermissionActionDowntimes)
    permission_registry.register(PermissionRemoveAllDowntimes)


class CommandGroupVarious(CommandGroup):
    @property
    def ident(self) -> str:
        return "various"

    @property
    def title(self) -> str:
        return _("Various Commands")

    @property
    def sort_index(self) -> int:
        return 20


class PermissionSectionAction(PermissionSection):
    @property
    def name(self) -> str:
        return "action"

    @property
    def title(self) -> str:
        return _("Commands on host and services")

    @property
    def do_sort(self):
        return True


#   .--Reschedule----------------------------------------------------------.
#   |          ____                _              _       _                |
#   |         |  _ \ ___  ___  ___| |__   ___  __| |_   _| | ___           |
#   |         | |_) / _ \/ __|/ __| '_ \ / _ \/ _` | | | | |/ _ \          |
#   |         |  _ <  __/\__ \ (__| | | |  __/ (_| | |_| | |  __/          |
#   |         |_| \_\___||___/\___|_| |_|\___|\__,_|\__,_|_|\___|          |
#   |                                                                      |
#   '----------------------------------------------------------------------'

PermissionActionReschedule = Permission(
    section=PermissionSectionAction,
    name="reschedule",
    title=_l("Reschedule checks"),
    description=_l("Reschedule host and service checks"),
    defaults=["user", "admin"],
)


class CommandReschedule(Command):
    @property
    def ident(self) -> str:
        return "reschedule"

    @property
    def title(self) -> str:
        return _("Reschedule active checks")

    @property
    def confirm_title(self) -> str:
        return _("Reschedule active checks immediately?")

    @property
    def confirm_button(self) -> LazyString:
        return _l("Reschedule")

    @property
    def icon_name(self):
        return "service_duration"

    @property
    def permission(self) -> Permission:
        return PermissionActionReschedule

    @property
    def tables(self):
        return ["host", "service"]

    def confirm_dialog_additions(
        self,
        cmdtag: Literal["HOST", "SVC"],
        row: Row,
        len_action_rows: int,
    ) -> HTML:
        return HTML("<br><br>" + _("Spreading: %s minutes") % request.var("_resched_spread"))

    def render(self, what) -> None:  # type: ignore[no-untyped-def]
        html.open_div(class_="group")
        html.write_text(_("Spread over") + " ")
        html.text_input(
            "_resched_spread", default_value="5", size=3, cssclass="number", required=True
        )
        html.write_text(" " + _("minutes"))
        html.close_div()

        html.open_div(class_="group")
        html.button("_resched_checks", _("Reschedule"), cssclass="hot")
        html.button("_cancel", _("Cancel"))
        html.close_div()

    def _action(
        self, cmdtag: Literal["HOST", "SVC"], spec: str, row: Row, row_index: int, action_rows: Rows
    ) -> CommandActionResult:
        if request.var("_resched_checks"):
            spread = utils.saveint(request.var("_resched_spread"))

            t = time.time()
            if spread:
                t += spread * 60.0 * row_index / len(action_rows)

            command = "SCHEDULE_FORCED_" + cmdtag + "_CHECK;%s;%d" % (spec, int(t))
            return command, self.confirm_dialog_options(
                cmdtag,
                row,
                len(action_rows),
            )
        return None


# .
#   .--Enable/Disable Notifications----------------------------------------.
#   |           _____          ______  _           _     _                 |
#   |          | ____|_ __    / /  _ \(_)___  __ _| |__ | | ___            |
#   |          |  _| | '_ \  / /| | | | / __|/ _` | '_ \| |/ _ \           |
#   |          | |___| | | |/ / | |_| | \__ \ (_| | |_) | |  __/           |
#   |          |_____|_| |_/_/  |____/|_|___/\__,_|_.__/|_|\___|           |
#   |                                                                      |
#   |       _   _       _   _  __ _           _   _                        |
#   |      | \ | | ___ | |_(_)/ _(_) ___ __ _| |_(_) ___  _ __  ___        |
#   |      |  \| |/ _ \| __| | |_| |/ __/ _` | __| |/ _ \| '_ \/ __|       |
#   |      | |\  | (_) | |_| |  _| | (_| (_| | |_| | (_) | | | \__ \       |
#   |      |_| \_|\___/ \__|_|_| |_|\___\__,_|\__|_|\___/|_| |_|___/       |
#   |                                                                      |
#   '----------------------------------------------------------------------'

PermissionActionNotifications = Permission(
    section=PermissionSectionAction,
    name="notifications",
    title=_l("Enable/disable notifications"),
    description=_l("Enable and disable notifications on hosts and services"),
    defaults=[],
)


class CommandNotifications(Command):
    @property
    def ident(self) -> str:
        return "notifications"

    @property
    def title(self) -> str:
        return _("Enable/disable notifications")

    @property
    def confirm_title(self) -> str:
        return (
            _("Enable notifications?")
            if request.var("_enable_notifications")
            else _("Disable notifications?")
        )

    @property
    def confirm_button(self) -> LazyString:
        return _l("Enable") if request.var("_enable_notifications") else _l("Disable")

    @property
    def permission(self) -> Permission:
        return PermissionActionNotifications

    @property
    def tables(self):
        return ["host", "service"]

    def confirm_dialog_additions(
        self,
        cmdtag: Literal["HOST", "SVC"],
        row: Row,
        len_action_rows: int,
    ) -> HTML:
        return HTML(
            "<br><br>"
            + (
                _("Notifications will be sent according to the notification rules")
                if request.var("_enable_notifications")
                else _("This will suppress all notifications")
            )
        )

    def confirm_dialog_icon_class(self) -> Literal["question", "warning"]:
        if request.var("_enable_notifications"):
            return "question"
        return "warning"

    def render(self, what) -> None:  # type: ignore[no-untyped-def]
        html.open_div(class_="group")
        html.button("_enable_notifications", _("Enable"), cssclass="border_hot")
        html.button("_disable_notifications", _("Disable"), cssclass="border_hot")
        html.button("_cancel", _("Cancel"))
        html.close_div()

    def _action(
        self, cmdtag: Literal["HOST", "SVC"], spec: str, row: Row, row_index: int, action_rows: Rows
    ) -> CommandActionResult:
        if request.var("_enable_notifications"):
            return (
                "ENABLE_" + cmdtag + "_NOTIFICATIONS;%s" % spec,
                self.confirm_dialog_options(
                    cmdtag,
                    row,
                    len(action_rows),
                ),
            )
        if request.var("_disable_notifications"):
            return (
                "DISABLE_" + cmdtag + "_NOTIFICATIONS;%s" % spec,
                self.confirm_dialog_options(
                    cmdtag,
                    row,
                    len(action_rows),
                ),
            )
        return None


# .
#   .--Enable/Disable Active Checks----------------------------------------.
#   |           _____          ______  _           _     _                 |
#   |          | ____|_ __    / /  _ \(_)___  __ _| |__ | | ___            |
#   |          |  _| | '_ \  / /| | | | / __|/ _` | '_ \| |/ _ \           |
#   |          | |___| | | |/ / | |_| | \__ \ (_| | |_) | |  __/           |
#   |          |_____|_| |_/_/  |____/|_|___/\__,_|_.__/|_|\___|           |
#   |                                                                      |
#   |       _        _   _              ____ _               _             |
#   |      / \   ___| |_(_)_   _____   / ___| |__   ___  ___| | _____      |
#   |     / _ \ / __| __| \ \ / / _ \ | |   | '_ \ / _ \/ __| |/ / __|     |
#   |    / ___ \ (__| |_| |\ V /  __/ | |___| | | |  __/ (__|   <\__ \     |
#   |   /_/   \_\___|\__|_| \_/ \___|  \____|_| |_|\___|\___|_|\_\___/     |
#   |                                                                      |
#   '----------------------------------------------------------------------'

PermissionActionEnableChecks = Permission(
    section=PermissionSectionAction,
    name="enablechecks",
    title=_l("Enable/disable checks"),
    description=_l("Enable and disable active or passive checks on hosts and services"),
    defaults=[],
)


class CommandToggleActiveChecks(Command):
    @property
    def ident(self) -> str:
        return "toggle_active_checks"

    @property
    def title(self) -> str:
        return _("Enable/Disable active checks")

    @property
    def confirm_title(self) -> str:
        return (
            _("Enable active checks")
            if request.var("_enable_checks")
            else _("Disable active checks")
        )

    @property
    def confirm_button(self) -> LazyString:
        return _l("Enable") if request.var("_enable_checks") else _l("Disable")

    @property
    def permission(self) -> Permission:
        return PermissionActionEnableChecks

    @property
    def tables(self):
        return ["host", "service"]

    def confirm_dialog_icon_class(self) -> Literal["question", "warning"]:
        return "warning"

    @property
    def show_command_form(self):
        return False

    def render(self, what) -> None:  # type: ignore[no-untyped-def]
        html.open_div(class_="group")
        html.button("_enable_checks", _("Enable"), cssclass="border_hot")
        html.button("_disable_checks", _("Disable"), cssclass="border_hot")
        html.button("_cancel", _("Cancel"))
        html.close_div()

    def _action(
        self, cmdtag: Literal["HOST", "SVC"], spec: str, row: Row, row_index: int, action_rows: Rows
    ) -> CommandActionResult:
        if request.var("_enable_checks"):
            return (
                "ENABLE_" + cmdtag + "_CHECK;%s" % spec,
                self.confirm_dialog_options(
                    cmdtag,
                    row,
                    len(action_rows),
                ),
            )
        if request.var("_disable_checks"):
            return (
                "DISABLE_" + cmdtag + "_CHECK;%s" % spec,
                self.confirm_dialog_options(
                    cmdtag,
                    row,
                    len(action_rows),
                ),
            )
        return None


# .
#   .--Enable/Disable Passive Checks---------------------------------------.
#   |           _____          ______  _           _     _                 |
#   |          | ____|_ __    / /  _ \(_)___  __ _| |__ | | ___            |
#   |          |  _| | '_ \  / /| | | | / __|/ _` | '_ \| |/ _ \           |
#   |          | |___| | | |/ / | |_| | \__ \ (_| | |_) | |  __/           |
#   |          |_____|_| |_/_/  |____/|_|___/\__,_|_.__/|_|\___|           |
#   |                                                                      |
#   |   ____               _              ____ _               _           |
#   |  |  _ \ __ _ ___ ___(_)_   _____   / ___| |__   ___  ___| | _____    |
#   |  | |_) / _` / __/ __| \ \ / / _ \ | |   | '_ \ / _ \/ __| |/ / __|   |
#   |  |  __/ (_| \__ \__ \ |\ V /  __/ | |___| | | |  __/ (__|   <\__ \   |
#   |  |_|   \__,_|___/___/_| \_/ \___|  \____|_| |_|\___|\___|_|\_\___/   |
#   |                                                                      |
#   '----------------------------------------------------------------------'


class CommandTogglePassiveChecks(Command):
    @property
    def ident(self) -> str:
        return "toggle_passive_checks"

    @property
    def title(self) -> str:
        return _("Enable/Disable passive checks")

    @property
    def confirm_title(self) -> str:
        return (
            _("Enable passive checks")
            if request.var("_enable_passive_checks")
            else _("Disable passive checks")
        )

    @property
    def confirm_button(self) -> LazyString:
        return _l("Enable") if request.var("_enable_passive_checks") else _l("Disable")

    @property
    def permission(self) -> Permission:
        return PermissionActionEnableChecks

    @property
    def tables(self):
        return ["host", "service"]

    def confirm_dialog_icon_class(self) -> Literal["question", "warning"]:
        return "warning"

    def render(self, what) -> None:  # type: ignore[no-untyped-def]
        html.open_div(class_="group")
        html.button("_enable_passive_checks", _("Enable"), cssclass="border_hot")
        html.button("_disable_passive_checks", _("Disable"), cssclass="border_hot")
        html.button("_cancel", _("Cancel"))
        html.close_div()

    def _action(
        self, cmdtag: Literal["HOST", "SVC"], spec: str, row: Row, row_index: int, action_rows: Rows
    ) -> CommandActionResult:
        if request.var("_enable_passive_checks"):
            return (
                "ENABLE_PASSIVE_" + cmdtag + "_CHECKS;%s" % spec,
                self.confirm_dialog_options(
                    cmdtag,
                    row,
                    len(action_rows),
                ),
            )
        if request.var("_disable_passive_checks"):
            return (
                "DISABLE_PASSIVE_" + cmdtag + "_CHECKS;%s" % spec,
                self.confirm_dialog_options(
                    cmdtag,
                    row,
                    len(action_rows),
                ),
            )
        return None


# .
#   .--Clear Modified Attributes-------------------------------------------.
#   |            ____ _                   __  __           _               |
#   |           / ___| | ___  __ _ _ __  |  \/  | ___   __| |              |
#   |          | |   | |/ _ \/ _` | '__| | |\/| |/ _ \ / _` |              |
#   |          | |___| |  __/ (_| | |    | |  | | (_) | (_| |_             |
#   |           \____|_|\___|\__,_|_|    |_|  |_|\___/ \__,_(_)            |
#   |                                                                      |
#   |              _   _   _        _ _           _                        |
#   |             / \ | |_| |_ _ __(_) |__  _   _| |_ ___  ___             |
#   |            / _ \| __| __| '__| | '_ \| | | | __/ _ \/ __|            |
#   |           / ___ \ |_| |_| |  | | |_) | |_| | ||  __/\__ \            |
#   |          /_/   \_\__|\__|_|  |_|_.__/ \__,_|\__\___||___/            |
#   |                                                                      |
#   '----------------------------------------------------------------------'

PermissionActionClearModifiedAttributes = Permission(
    section=PermissionSectionAction,
    name="clearmodattr",
    title=_l("Reset modified attributes"),
    description=_l(
        "Reset all manually modified attributes of a host "
        "or service (like disabled notifications)"
    ),
    defaults=[],
)


class CommandClearModifiedAttributes(Command):
    @property
    def ident(self) -> str:
        return "clear_modified_attributes"

    @property
    def title(self) -> str:
        return _("Reset modified attributes")

    @property
    def confirm_button(self) -> LazyString:
        return _l("Reset")

    @property
    def permission(self) -> Permission:
        return PermissionActionClearModifiedAttributes

    @property
    def tables(self):
        return ["host", "service"]

    def render(self, what) -> None:  # type: ignore[no-untyped-def]
        html.open_div(class_="group")
        html.button("_clear_modattr", _("Reset attributes"), cssclass="hot")
        html.button("_cancel", _("Cancel"))
        html.close_div()

    def confirm_dialog_additions(
        self,
        cmdtag: Literal["HOST", "SVC"],
        row: Row,
        len_action_rows: int,
    ) -> HTML:
        return HTML(
            "<br><br>"
            + _("Resets the commands '%s', '%s' and '%s' to the default state")
            % (
                CommandToggleActiveChecks().title,
                CommandTogglePassiveChecks().title,
                CommandNotifications().title,
            )
        )

    def _action(
        self, cmdtag: Literal["HOST", "SVC"], spec: str, row: Row, row_index: int, action_rows: Rows
    ) -> CommandActionResult:
        if request.var("_clear_modattr"):
            return (
                "CHANGE_" + cmdtag + "_MODATTR;%s;0" % spec,
                self.confirm_dialog_options(
                    cmdtag,
                    row,
                    len(action_rows),
                ),
            )
        return None


# .
#   .--Fake Checks---------------------------------------------------------.
#   |         _____     _           ____ _               _                 |
#   |        |  ___|_ _| | _____   / ___| |__   ___  ___| | _____          |
#   |        | |_ / _` | |/ / _ \ | |   | '_ \ / _ \/ __| |/ / __|         |
#   |        |  _| (_| |   <  __/ | |___| | | |  __/ (__|   <\__ \         |
#   |        |_|  \__,_|_|\_\___|  \____|_| |_|\___|\___|_|\_\___/         |
#   |                                                                      |
#   '----------------------------------------------------------------------'

PermissionActionFakeChecks = Permission(
    section=PermissionSectionAction,
    name="fakechecks",
    title=_l("Fake check results"),
    description=_l("Manually submit check results for host and service checks"),
    defaults=["admin"],
)


class CommandGroupFakeCheck(CommandGroup):
    @property
    def ident(self) -> str:
        return "fake_check"

    @property
    def title(self) -> str:
        return _("Fake check results")

    @property
    def sort_index(self) -> int:
        return 15


class CommandFakeCheckResult(Command):
    @property
    def ident(self) -> str:
        return "fake_check_result"

    @property
    def title(self) -> str:
        return _("Fake check results")

    @property
    def confirm_title(self) -> str:
        return _("Manually set check results to %s?") % self._get_target_state()

    def _get_target_state(self) -> str:
        for var, value in list(request.itervars(prefix="_fake_")):
            if not var[-1].isdigit():
                continue
            return value
        return ""

    @property
    def confirm_button(self) -> LazyString:
        return _l("Set")

    @property
    def icon_name(self):
        return "fake_check_result"

    @property
    def permission(self) -> Permission:
        return PermissionActionFakeChecks

    @property
    def tables(self):
        return ["host", "service"]

    @property
    def group(self) -> type[CommandGroup]:
        return CommandGroupFakeCheck

    @property
    def is_show_more(self) -> bool:
        return True

    def render(self, what) -> None:  # type: ignore[no-untyped-def]
        html.open_table()

        html.open_tr()
        html.open_td()
        html.write_text(_("Plugin output"))
        html.close_td()
        html.open_td()
        html.text_input("_fake_output", "", size=60)
        html.close_td()
        html.close_tr()

        html.open_tr()
        html.open_td()
        html.write_text(_("Performance data"))
        html.close_td()
        html.open_td()
        html.text_input("_fake_perfdata", "", size=60)
        html.close_td()
        html.close_tr()

        html.open_tr()
        html.open_td()
        html.write_text(_("Result"))
        html.close_td()
        html.open_td()
        if what == "host":
            html.button("_fake_0", _("Up"))
            html.button("_fake_1", _("Down"))
        else:
            html.button("_fake_0", _("OK"))
            html.button("_fake_1", _("Warning"))
            html.button("_fake_2", _("Critical"))
            html.button("_fake_3", _("Unknown"))
        html.close_td()
        html.close_tr()

        html.close_table()

    def _action(
        self, cmdtag: Literal["HOST", "SVC"], spec: str, row: Row, row_index: int, action_rows: Rows
    ) -> CommandActionResult:
        for s in [0, 1, 2, 3]:
            statename = request.var("_fake_%d" % s)
            if statename:
                pluginoutput = request.get_str_input_mandatory("_fake_output").strip()
                if not pluginoutput:
                    pluginoutput = _("Manually set to %s by %s") % (
                        escaping.escape_attribute(statename),
                        user.id,
                    )
                perfdata = request.var("_fake_perfdata")
                if perfdata:
                    pluginoutput += "|" + perfdata
                command = "PROCESS_{}_CHECK_RESULT;{};{};{}".format(
                    "SERVICE" if cmdtag == "SVC" else cmdtag,
                    spec,
                    s,
                    livestatus.lqencode(pluginoutput),
                )
                return command, self.confirm_dialog_options(
                    cmdtag,
                    row,
                    len(action_rows),
                )
        return None


# .
#   .--Custom Notifications------------------------------------------------.
#   |                   ____          _                                    |
#   |                  / ___|   _ ___| |_ ___  _ __ ___                    |
#   |                 | |  | | | / __| __/ _ \| '_ ` _ \                   |
#   |                 | |__| |_| \__ \ || (_) | | | | | |                  |
#   |                  \____\__,_|___/\__\___/|_| |_| |_|                  |
#   |                                                                      |
#   |       _   _       _   _  __ _           _   _                        |
#   |      | \ | | ___ | |_(_)/ _(_) ___ __ _| |_(_) ___  _ __  ___        |
#   |      |  \| |/ _ \| __| | |_| |/ __/ _` | __| |/ _ \| '_ \/ __|       |
#   |      | |\  | (_) | |_| |  _| | (_| (_| | |_| | (_) | | | \__ \       |
#   |      |_| \_|\___/ \__|_|_| |_|\___\__,_|\__|_|\___/|_| |_|___/       |
#   |                                                                      |
#   '----------------------------------------------------------------------'

PermissionActionCustomNotification = Permission(
    section=PermissionSectionAction,
    name="customnotification",
    title=_l("Send custom notification"),
    description=_l(
        "Manually let the core send a notification to a host or service in order "
        "to test if notifications are setup correctly"
    ),
    defaults=["user", "admin"],
)


class CommandCustomNotification(Command):
    @property
    def ident(self) -> str:
        return "send_custom_notification"

    @property
    def title(self) -> str:
        return _("Send custom notification")

    @property
    def confirm_title(self) -> str:
        return "%s?" % self.title

    @property
    def confirm_button(self) -> LazyString:
        return _l("Send")

    @property
    def icon_name(self):
        return "notifications"

    @property
    def permission(self) -> Permission:
        return PermissionActionCustomNotification

    @property
    def tables(self):
        return ["host", "service"]

    @property
    def is_show_more(self) -> bool:
        return True

    def render(self, what) -> None:  # type: ignore[no-untyped-def]
        html.open_div(class_="group")
        html.text_input(
            "_cusnot_comment",
            id_="cusnot_comment",
            size=60,
            submit="_customnotification",
            label=_("Comment"),
            placeholder=_("Enter your message here"),
        )
        html.close_div()

        html.open_div(class_="group")
        html.checkbox(
            "_cusnot_forced",
            False,
            label=_(
                "Send regardless of restrictions, e.g. notification period or disabled notifications (forced)"
            ),
        )
        html.close_div()
        html.open_div(class_="group")
        html.checkbox(
            "_cusnot_broadcast",
            False,
            label=_("Send to all contacts of the selected hosts/services (broadcast)"),
        )
        html.close_div()

        html.open_div(class_="group")
        html.button("_customnotification", _("Send"), cssclass="hot")
        html.button("_cancel", _("Cancel"))
        html.close_div()

    def _action(
        self, cmdtag: Literal["HOST", "SVC"], spec: str, row: Row, row_index: int, action_rows: Rows
    ) -> CommandActionResult:
        if request.var("_customnotification"):
            comment = request.get_str_input_mandatory("_cusnot_comment")
            broadcast = 1 if html.get_checkbox("_cusnot_broadcast") else 0
            forced = 2 if html.get_checkbox("_cusnot_forced") else 0
            command = "SEND_CUSTOM_{}_NOTIFICATION;{};{};{};{}".format(
                cmdtag,
                spec,
                broadcast + forced,
                user.id,
                livestatus.lqencode(comment),
            )
            return command, self.confirm_dialog_options(
                cmdtag,
                row,
                len(action_rows),
            )
        return None


# .
#   .--Acknowledge---------------------------------------------------------.
#   |       _        _                        _          _                 |
#   |      / \   ___| | ___ __   _____      _| | ___  __| | __ _  ___      |
#   |     / _ \ / __| |/ / '_ \ / _ \ \ /\ / / |/ _ \/ _` |/ _` |/ _ \     |
#   |    / ___ \ (__|   <| | | | (_) \ V  V /| |  __/ (_| | (_| |  __/     |
#   |   /_/   \_\___|_|\_\_| |_|\___/ \_/\_/ |_|\___|\__,_|\__, |\___|     |
#   |                                                      |___/           |
#   '----------------------------------------------------------------------'

PermissionActionAcknowledge = Permission(
    section=PermissionSectionAction,
    name="acknowledge",
    title=_l("Acknowledge"),
    description=_l("Acknowledge host and service problems and remove acknowledgements"),
    defaults=["user", "admin"],
)


class CommandGroupAcknowledge(CommandGroup):
    @property
    def ident(self) -> str:
        return "acknowledge"

    @property
    def title(self) -> str:
        return _("Acknowledge")

    @property
    def sort_index(self) -> int:
        return 5


class CommandAcknowledge(Command):
    @property
    def ident(self) -> str:
        return "acknowledge"

    @property
    def title(self) -> str:
        return _("Acknowledge problems")

    @property
    def confirm_title(self) -> str:
        return (
            _("Acknowledge problems?")
            if request.var("_acknowledge")
            else _("Remove acknowledgement?")
        )

    @property
    def confirm_button(self) -> LazyString:
        return _l("Acknowledge") if request.var("_acknowledge") else _l("Remove")

    @property
    def icon_name(self):
        return "host_svc_problems"

    @property
    def is_shortcut(self) -> bool:
        return True

    @property
    def is_suggested(self) -> bool:
        return True

    @property
    def permission(self) -> Permission:
        return PermissionActionAcknowledge

    @property
    def group(self) -> type[CommandGroup]:
        return CommandGroupAcknowledge

    @property
    def tables(self):
        return ["host", "service", "aggr"]

    def render(self, what) -> None:  # type: ignore[no-untyped-def]
        html.open_div(class_="group")
        html.text_input(
            "_ack_comment",
            id_="ack_comment",
            size=60,
            submit="_acknowledge",
            label=_("Comment"),
            required=True,
        )
        html.close_div()

        html.open_div(class_="group")
        html.checkbox(
            "_ack_sticky", active_config.view_action_defaults["ack_sticky"], label=_("sticky")
        )
        html.checkbox(
            "_ack_notify",
            active_config.view_action_defaults["ack_notify"],
            label=_("send notification"),
        )
        html.checkbox(
            "_ack_persistent",
            active_config.view_action_defaults["ack_persistent"],
            label=_("persistent comment"),
        )
        html.close_div()

        html.open_div(class_="group")
        self._vs_expire().render_input(
            "_ack_expire", active_config.view_action_defaults.get("ack_expire", 0)
        )
        html.help(
            _("Note: Expiration of acknowledgements only works when using the Checkmk Micro Core.")
        )
        html.close_div()

        html.open_div(class_="group")
        html.button("_acknowledge", _("Acknowledge"), cssclass="hot")
        html.button("_remove_ack", _("Remove acknowledgement"), formnovalidate=True)
        html.close_div()

    def _action(  # pylint: disable=too-many-branches
        self, cmdtag: Literal["HOST", "SVC"], spec: str, row: Row, row_index: int, action_rows: Rows
    ) -> CommandActionResult:
        if "aggr_tree" in row:  # BI mode
            specs = []
            for site, host, service in _find_all_leaves(row["aggr_tree"]):
                if service:
                    spec = f"{host};{service}"
                    cmdtag = "SVC"
                else:
                    spec = host
                    cmdtag = "HOST"
                specs.append((site, spec, cmdtag))

        if request.var("_acknowledge"):
            comment = request.get_str_input("_ack_comment")
            if not comment:
                raise MKUserError("_ack_comment", _("You need to supply a comment."))
            if ";" in comment:
                raise MKUserError("_ack_comment", _("The comment must not contain semicolons."))
            non_empty_comment = comment

            sticky = 2 if request.var("_ack_sticky") else 0
            sendnot = 1 if request.var("_ack_notify") else 0
            perscomm = 1 if request.var("_ack_persistent") else 0

            expire_secs = self._vs_expire().from_html_vars("_ack_expire")
            if expire_secs:
                expire = int(time.time()) + expire_secs
                expire_text = ";%d" % expire
            else:
                expire_text = ""

            def make_command_ack(spec, cmdtag):
                return (
                    "ACKNOWLEDGE_"
                    + cmdtag
                    + "_PROBLEM;%s;%d;%d;%d;%s" % (spec, sticky, sendnot, perscomm, user.id)
                    + (";%s" % livestatus.lqencode(non_empty_comment))
                    + expire_text
                )

            if "aggr_tree" in row:  # BI mode
                commands = [
                    (site, make_command_ack(spec_, cmdtag_)) for site, spec_, cmdtag_ in specs
                ]
            else:
                commands = [make_command_ack(spec, cmdtag)]

            return commands, self.confirm_dialog_options(
                cmdtag,
                row,
                len(action_rows),
            )

        if request.var("_remove_ack"):

            def make_command_rem(spec, cmdtag):
                return "REMOVE_" + cmdtag + "_ACKNOWLEDGEMENT;%s" % spec

            if "aggr_tree" in row:  # BI mode
                commands = [
                    (site, make_command_rem(spec, cmdtag)) for site, spec_, cmdtag_ in specs
                ]
            else:
                commands = [make_command_rem(spec, cmdtag)]
            return commands, self.confirm_dialog_options(cmdtag, row, len(action_rows))

        return None

    def _vs_expire(self):
        return Age(
            display=["days", "hours", "minutes"],
            label=_("Expire acknowledgement after"),
        )


# .
#   .--Comments------------------------------------------------------------.
#   |           ____                                     _                 |
#   |          / ___|___  _ __ ___  _ __ ___   ___ _ __ | |_ ___           |
#   |         | |   / _ \| '_ ` _ \| '_ ` _ \ / _ \ '_ \| __/ __|          |
#   |         | |__| (_) | | | | | | | | | | |  __/ | | | |_\__ \          |
#   |          \____\___/|_| |_| |_|_| |_| |_|\___|_| |_|\__|___/          |
#   |                                                                      |
#   '----------------------------------------------------------------------'

PermissionActionAddComment = Permission(
    section=PermissionSectionAction,
    name="addcomment",
    title=_l("Add comments"),
    description=_l("Add comments to hosts or services, and remove comments"),
    defaults=["user", "admin"],
)


class CommandAddComment(Command):
    @property
    def ident(self) -> str:
        return "add_comment"

    @property
    def title(self) -> str:
        return _("Add comment")

    @property
    def confirm_title(self) -> str:
        return _("Add comment?")

    @property
    def confirm_button(self) -> LazyString:
        return _l("Add")

    @property
    def icon_name(self):
        return "comment"

    @property
    def permission(self) -> Permission:
        return PermissionActionAddComment

    @property
    def tables(self):
        return ["host", "service"]

    def render(self, what) -> None:  # type: ignore[no-untyped-def]
        html.open_div(class_="group")
        html.text_input(
            "_comment",
            id_="comment",
            size=60,
            submit="_add_comment",
            label=_("Comment"),
            required=True,
        )
        html.close_div()

        html.open_div(class_="group")
        html.button("_add_comment", _("Add comment"), cssclass="hot")
        html.button("_cancel", _("Cancel"))
        html.close_div()

    def _action(
        self, cmdtag: Literal["HOST", "SVC"], spec: str, row: Row, row_index: int, action_rows: Rows
    ) -> CommandActionResult:
        if request.var("_add_comment"):
            comment = request.get_str_input("_comment")
            if not comment:
                raise MKUserError("_comment", _("You need to supply a comment."))
            command = (
                "ADD_"
                + cmdtag
                + f"_COMMENT;{spec};1;{user.id}"
                + (";%s" % livestatus.lqencode(comment))
            )
            return command, self.confirm_dialog_options(cmdtag, row, len(action_rows))
        return None


# .
#   .--Downtimes-----------------------------------------------------------.
#   |         ____                      _   _                              |
#   |        |  _ \  _____      ___ __ | |_(_)_ __ ___   ___  ___          |
#   |        | | | |/ _ \ \ /\ / / '_ \| __| | '_ ` _ \ / _ \/ __|         |
#   |        | |_| | (_) \ V  V /| | | | |_| | | | | | |  __/\__ \         |
#   |        |____/ \___/ \_/\_/ |_| |_|\__|_|_| |_| |_|\___||___/         |
#   |                                                                      |
#   '----------------------------------------------------------------------'

PermissionActionDowntimes = Permission(
    section=PermissionSectionAction,
    name="downtimes",
    title=_l("Set/Remove downtimes"),
    description=_l("Schedule and remove downtimes on hosts and services"),
    defaults=["user", "admin"],
)

PermissionRemoveAllDowntimes = Permission(
    section=PermissionSectionAction,
    name="remove_all_downtimes",
    title=_l("Remove all downtimes"),
    description=_l('Allow the user to use the action "Remove all" downtimes'),
    defaults=["user", "admin"],
)


class CommandGroupDowntimes(CommandGroup):
    @property
    def ident(self) -> str:
        return "downtimes"

    @property
    def title(self) -> str:
        return _("Schedule downtimes")

    @property
    def sort_index(self) -> int:
        return 10


class RecurringDowntimes(Protocol):
    def choices(self) -> Choices:
        ...

    def show_input_elements(self, default: str) -> None:
        ...

    def number(self) -> int:
        ...

    def title_prefix(self, recurring_number: int) -> str:
        ...


class NoRecurringDowntimes:
    def choices(self) -> Choices:
        return []

    def show_input_elements(self, default: str) -> None:
        pass

    def number(self) -> int:
        return 0

    def title_prefix(self, recurring_number: int) -> str:
        return _("Schedule an immediate downtime")


class CommandScheduleDowntimes(Command):
    recurring_downtimes: RecurringDowntimes = NoRecurringDowntimes()

    @property
    def ident(self) -> str:
        return "schedule_downtimes"

    @property
    def title(self) -> str:
        return _("Schedule downtimes")

    @property
    def confirm_title(self) -> str:
        return _("Schedule downtime?")

    @property
    def confirm_button(self) -> LazyString:
        return _l("Schedule")

    @property
    def icon_name(self):
        return "downtime"

    @property
    def is_shortcut(self) -> bool:
        return True

    @property
    def is_suggested(self) -> bool:
        return True

    @property
    def permission(self) -> Permission:
        return PermissionActionDowntimes

    @property
    def group(self) -> type[CommandGroup]:
        return CommandGroupDowntimes

    @property
    def tables(self):
        return ["host", "service", "aggr"]

    def user_confirm_options(
        self, len_rows: int, cmdtag: Literal["HOST", "SVC"]
    ) -> list[tuple[str, str]]:
        if cmdtag == "SVC" and not request.var("_down_remove"):
            return [
                (
                    _("Schedule downtime for %d %s")
                    % (len_rows, ungettext("service", "services", len_rows)),
                    "_do_confirm_service_downtime",
                ),
                (_("Schedule downtime on host"), "_do_confirm_host_downtime"),
            ]
        return super().user_confirm_options(len_rows, cmdtag)

    def render(self, what) -> None:  # type: ignore[no-untyped-def]
        self._render_comment()
        self._render_date_and_time()
        self._render_advanced_options(what)
        self._render_confirm_buttons(what)

    def _render_comment(self) -> None:
        html.open_div(class_="group")
        html.text_input(
            "_down_comment",
            id_="down_comment",
            size=60,
            label=_("Comment"),
            required=not self._adhoc_downtime_configured(),
            placeholder=_("What is the occasion?"),
            submit="_down_custom",
        )
        html.close_div()

    def _render_date_and_time(self) -> None:  # pylint: disable=too-many-statements
        html.open_div(class_="group")
        html.heading("Date and time")
        html.br()

        html.open_table()

        # Duration section
        html.open_tr()
        html.open_td()
        html.write_text(_("Duration"))
        html.close_td()
        html.open_td()
        for time_range in active_config.user_downtime_timeranges:
            html.input(
                name=(varname := f'_downrange__{time_range["end"]}'),
                type_="button",
                id_=varname,
                class_=["button", "duration"],
                value=_u(time_range["title"]),
                onclick=self._get_onclick(time_range["end"]),
                submit="_set_date_and_time",
            )

        presets_url = makeuri_contextless(
            request,
            [("mode", "edit_configvar"), ("varname", "user_downtime_timeranges")],
            filename="wato.py",
        )
        html.a(
            _("(Edit presets)"),
            href=presets_url,
            class_="down_presets",
        )

        html.close_td()
        html.close_tr()

        html.open_tr()
        html.open_td()
        html.br()
        html.close_td()
        html.close_tr()

        # Start section
        html.open_tr()
        html.open_td()
        html.write_text(_("Start"))
        html.close_td()
        html.open_td()
        self._vs_date().render_input("_down_from_date", time.strftime("%Y-%m-%d"))
        self._vs_time().render_input("_down_from_time", time.strftime("%H:%M"))
        html.close_td()
        html.close_tr()

        html.open_tr()
        html.open_td()
        html.br()
        html.close_td()
        html.close_tr()

        # End section
        html.open_tr()
        html.open_td()
        html.write_text(_("End"))
        html.close_td()
        html.open_td()
        self._vs_date().render_input("_down_to_date", time.strftime("%Y-%m-%d"))
        self._vs_time().render_input(
            "_down_to_time", time.strftime("%H:%M", time.localtime(time.time() + 7200))
        )
        html.close_td()

        html.open_tr()
        html.open_td()
        html.br()
        html.close_td()
        html.close_tr()

        # Repeat section
        html.open_tr()
        html.open_td()
        html.write_text(_("Repeat"))
        html.close_td()
        html.open_td()
        self.recurring_downtimes.show_input_elements(default="0")
        html.close_td()
        html.close_table()

        html.open_tr()
        html.open_td()
        html.br()
        html.close_td()
        html.close_tr()
        html.close_div()

    def _vs_date(self) -> DatePicker:
        return DatePicker(
            title=_("Downtime datepicker"),
        )

    def _vs_time(self) -> TimePicker:
        return TimePicker(
            title=_("Downtime timepicker"),
        )

    def _get_onclick(
        self, time_range: int | Literal["next_day", "next_week", "next_month", "next_year"]
    ) -> str:
        start_time = self._current_local_time()
        end_time = time_interval_end(time_range, self._current_local_time())

        return (
            f'cmk.utils.update_time("date__down_from_date","{time.strftime("%Y-%m-%d",time.localtime(start_time))}");'
            f'cmk.utils.update_time("time__down_from_time","{time.strftime("%H:%M",time.localtime(start_time))}");'
            f'cmk.utils.update_time("date__down_to_date","{time.strftime("%Y-%m-%d",time.localtime(end_time))}");'
            f'cmk.utils.update_time("time__down_to_time","{time.strftime("%H:%M", time.localtime(end_time))}");'
        )

    def _render_advanced_options(self, what) -> None:  # type: ignore[no-untyped-def]
        with foldable_container(
            treename="advanced_down_options",
            id_="adv_down_opts",
            isopen=False,
            title=_("Advanced options"),
            indent=False,
        ):
            # TODO this can be removed? What about the global config option?
            # if self._adhoc_downtime_configured():
            #    adhoc_duration = active_config.adhoc_downtime.get("duration")
            #    adhoc_comment = active_config.adhoc_downtime.get("comment", "")
            #    html.open_div(class_="group")
            #    html.button("_down_adhoc", _("Adhoc for %d minutes") % adhoc_duration)
            #    html.nbsp()
            #    html.write_text(_("with comment") + ": ")
            #    html.write_text(adhoc_comment)
            #    html.close_div()

            if what == "host":
                html.open_div(class_="group")
                self._vs_host_downtime().render_input("_include_children", None)
                html.close_div()

            html.open_div(class_="group")
            self._vs_flexible_options().render_input("_down_duration", None)
            html.close_div()

    def _render_confirm_buttons(self, what) -> None:  # type: ignore[no-untyped-def]
        html.open_div(class_="group")
        html.button("_down_host", _("Schedule downtime on host"), cssclass="hot")
        if what == "service":
            html.button("_down_service", _("Schedule downtime on service"))
        html.button("_cancel", _("Cancel"), formnovalidate=True)
        html.close_div()

    def _vs_host_downtime(self) -> Dictionary:
        return Dictionary(
            title="Host downtime options",
            elements=[
                (
                    "_include_children",
                    Checkbox(
                        title=_("Only for hosts: Set child hosts in downtime"),
                        label=_("Include indirectly connected hosts (recursively)"),
                        help=_(
                            "Either verify the server certificate using the "
                            "site local CA or accept any certificate offered by "
                            "the server. It is highly recommended to leave this "
                            "enabled."
                        ),
                    ),
                ),
            ],
        )

    def _vs_flexible_options(self) -> Dictionary:
        return Dictionary(
            title=_("Flexible downtime options"),
            elements=[
                (
                    "_down_duration",
                    Age(
                        display=["hours"],
                        title=_(
                            "Only start downtime if host/service goes "
                            "DOWN/UNREACH during the defined start and end time "
                            "(flexible)"
                        ),
                        cssclass="inline",
                    ),
                ),
            ],
        )

    def _action(  # pylint: disable=too-many-arguments
        self,
        cmdtag: Literal["HOST", "SVC"],
        spec: str,
        row: Row,
        row_index: int,
        action_rows: Rows,
    ) -> CommandActionResult:
        """Prepares the livestatus command for any received downtime information through WATO"""
        if request.var("_down_remove"):
            return self._remove_downtime_details(cmdtag, row, action_rows)

        recurring_number = self.recurring_downtimes.number()
        if varprefix := request.var("_down_host", request.var("_down_service")):
            start_time = self._custom_start_time()
            end_time = self._custom_end_time(start_time)

            if recurring_number == 8 and not 1 <= time.localtime(start_time).tm_mday <= 28:
                raise MKUserError(
                    varprefix,
                    _(
                        "The start of a recurring downtime can only be set for "
                        "days 1-28 of a month."
                    ),
                )

            comment = self._comment()
            delayed_duration = self._flexible_option()
            mode = determine_downtime_mode(recurring_number, delayed_duration)
            downtime = DowntimeSchedule(start_time, end_time, mode, delayed_duration, comment)
            cmdtag, specs, len_action_rows = self._downtime_specs(cmdtag, row, action_rows, spec)
            if "aggr_tree" in row:  # BI mode
                node = row["aggr_tree"]
                return (
                    _bi_commands(downtime, node),
                    self.confirm_dialog_options(
                        cmdtag,
                        row,
                        len(action_rows),
                    ),
                )
            return (
                [downtime.livestatus_command(spec_, cmdtag) for spec_ in specs],
                self._confirm_dialog_options(
                    cmdtag,
                    row,
                    len_action_rows,
                    _("Schedule a downtime?"),
                ),
            )

        return None

    def _confirm_dialog_options(
        self,
        cmdtag: Literal["HOST", "SVC"],
        row: Row,
        len_action_rows: int,
        title: str,
    ) -> CommandConfirmDialogOptions:
        return CommandConfirmDialogOptions(
            title,
            self.affected(len_action_rows, cmdtag),
            self.confirm_dialog_additions(cmdtag, row, len_action_rows),
            self.confirm_dialog_icon_class(),
            self.confirm_button,
        )

    def confirm_dialog_additions(
        self,
        cmdtag: Literal["HOST", "SVC"],
        row: Row,
        len_action_rows: int,
    ) -> HTML:
        additions = (
            "<br><br>"
            + _("Start: %s") % time.asctime(time.localtime(start_time := self._custom_start_time()))
            + "<br>"
            + _("End: %s") % time.asctime(time.localtime(self._custom_end_time(start_time)))
            + "<br><br>"
        )

        attributes = ""

        recurring_number_from_html = self.recurring_downtimes.number()
        if recurring_number_from_html:
            attributes += (
                "<li>"
                + _("Repeats every %s")
                % self.recurring_downtimes.choices()[recurring_number_from_html][1]
                + "</li>"
            )

        vs_host_downtime = self._vs_host_downtime()
        included_from_html = vs_host_downtime.from_html_vars("_include_children")
        vs_host_downtime.validate_value(included_from_html, "_include_children")
        if "_include_children" in included_from_html:
            if included_from_html.get("_include_children") is True:
                attributes += "<li>" + _("Child hosts also go in downtime (recursively).") + "</li>"
            else:
                attributes += "<li>" + _("Child hosts also go in downtime.") + "</li>"

        if duration := self._flexible_option():
            attributes += (
                "<li>"
                + _("Starts if host/service goes DOWN/UNREACH with a max. duration of %d hours.")
                % (duration / 3600)
                + "</li>"
            )

        if attributes:
            additions = additions + _("Downtime attributes:") + "<ul>" + attributes + "</ul>"

        return HTML(
            additions
            + "<u>"
            + _("Info:")
            + "</u> "
            + (
                _("Downtime also applies to services.")
                if cmdtag == "HOST"
                else _("Downtime does not apply to host.")
            )
        )

    def _remove_downtime_details(
        self, cmdtag: Literal["HOST", "SVC"], row: Row, action_rows: Rows
    ) -> tuple[list[str], CommandConfirmDialogOptions] | None:
        if not user.may("action.remove_all_downtimes"):
            return None
        if request.var("_down_host"):
            raise MKUserError(
                "_on_hosts",
                _("The checkbox for setting host downtimes does not work when removing downtimes."),
            )
        downtime_ids = []
        if cmdtag == "HOST":
            prefix = "host_"
        else:
            prefix = "service_"
        for id_ in row[prefix + "downtimes"]:
            if id_ != "":
                downtime_ids.append(int(id_))
        commands = []
        for dtid in downtime_ids:
            commands.append(f"DEL_{cmdtag}_DOWNTIME;{dtid}\n")
        title = _("Remove all scheduled downtimes?")
        return commands, self._confirm_dialog_options(
            cmdtag,
            row,
            len(action_rows),
            title,
        )

    def _flexible_option(self) -> int:
        vs_flexible_options = self._vs_flexible_options()
        duration_from_html = vs_flexible_options.from_html_vars("_down_duration")
        vs_flexible_options.validate_value(duration_from_html, "_down_duration")
        if duration_from_html:
            self._vs_duration().validate_value(
                duration := duration_from_html.get("_down_duration", 0), "_down_duration"
            )
            delayed_duration = duration
        else:
            delayed_duration = 0
        return delayed_duration

    def _comment(self):
        comment = (
            active_config.adhoc_downtime.get("comment", "")
            if request.var("_down_adhoc")
            else request.get_str_input("_down_comment")
        )
        if not comment:
            raise MKUserError("_down_comment", _("You need to supply a comment for your downtime."))
        return comment

    def _current_local_time(self):
        return time.time()

    def _custom_start_time(self):
        vs_date = self._vs_date()
        raw_start_date = vs_date.from_html_vars("_down_from_date")
        vs_date.validate_value(raw_start_date, "_down_from_date")

        vs_time = self._vs_time()
        raw_start_time = vs_time.from_html_vars("_down_from_time")
        vs_time.validate_value(raw_start_time, "_down_from_time")

        down_from = time.mktime(
            time.strptime(f"{raw_start_date} {raw_start_time}", "%Y-%m-%d %H:%M")
        )
        self._vs_down_from().validate_value(down_from, "_down_from")
        return down_from

    def _custom_end_time(self, start_time):
        vs_date = self._vs_date()
        raw_end_date = vs_date.from_html_vars("_down_to_date")
        vs_date.validate_value(raw_end_date, "_down_to_date")

        vs_time = self._vs_time()
        raw_end_time = vs_time.from_html_vars("_down_to_time")
        vs_time.validate_value(raw_end_time, "_down_to_time")

        end_time = time.mktime(time.strptime(f"{raw_end_date} {raw_end_time}", "%Y-%m-%d %H:%M"))
        self._vs_down_to().validate_value(end_time, "_down_to")

        if end_time < time.time():
            raise MKUserError(
                "_down_to",
                _(
                    "You cannot set a downtime that ends in the past. "
                    "This incident will be reported."
                ),
            )

        if end_time < start_time:
            raise MKUserError("_down_to", _("Your end date is before your start date."))

        return end_time

    def _downtime_specs(
        self,
        cmdtag: Literal["HOST", "SVC"],
        row: Row,
        action_rows: Rows,
        spec: str,
    ) -> tuple[Literal["HOST", "SVC"], list[str], int]:
        len_action_rows = len(action_rows)

        vs_host_downtime = self._vs_host_downtime()
        included_from_html = vs_host_downtime.from_html_vars("_include_children")
        vs_host_downtime.validate_value(included_from_html, "_include_children")
        if "_include_children" in included_from_html:  # only for hosts
            if (recurse := included_from_html.get("_include_children")) is not None:
                specs = [spec] + self._get_child_hosts(row["site"], [spec], recurse=recurse)
        elif request.var("_down_host"):  # set on hosts instead of services
            specs = [spec.split(";")[0]]
            cmdtag = "HOST"
            len_action_rows = len({row["host_name"] for row in action_rows})
        else:
            specs = [spec]
        return cmdtag, specs, len_action_rows

    def _vs_down_from(self) -> AbsoluteDate:
        return AbsoluteDate(
            title=_("From"),
            include_time=True,
            submit_form_name="_down_custom",
        )

    def _vs_down_to(self) -> AbsoluteDate:
        return AbsoluteDate(
            title=_("Until"),
            include_time=True,
            submit_form_name="_down_custom",
        )

    def _vs_duration(self) -> Age:
        return Age(
            display=["hours", "minutes"],
            title=_("Duration"),
            cssclass="inline",
        )

    def _get_child_hosts(self, site, hosts, recurse):
        hosts = set(hosts)

        sites.live().set_only_sites([site])
        query = "GET hosts\nColumns: name\n"
        query += "".join([f"Filter: parents >= {host}\n" for host in hosts])
        query += f"Or: {len(hosts)}\n"
        children = sites.live().query_column(query)
        sites.live().set_only_sites(None)

        # Recursion, but try to avoid duplicate work
        new_children = set(children) - hosts
        if new_children and recurse:
            rec_childs = self._get_child_hosts(site, new_children, True)
            new_children.update(rec_childs)
        return list(new_children)

    def _adhoc_downtime_configured(self) -> bool:
        return bool(active_config.adhoc_downtime and active_config.adhoc_downtime.get("duration"))


def _bi_commands(downtime: DowntimeSchedule, node: Any) -> Sequence[CommandSpec]:
    """Generate the list of downtime command strings for the BI module"""
    commands_aggr = []
    for site, host, service in _find_all_leaves(node):
        if service:
            spec = f"{host};{service}"
            cmdtag: Literal["HOST", "SVC"] = "SVC"
        else:
            spec = host
            cmdtag = "HOST"
        commands_aggr.append((site, downtime.livestatus_command(spec, cmdtag)))
    return commands_aggr


def _find_all_leaves(  # type: ignore[no-untyped-def]
    node,
) -> list[tuple[livestatus.SiteId | None, HostName, ServiceName | None]]:
    # leaf node
    if node["type"] == 1:
        site, host = node["host"]
        return [(livestatus.SiteId(site), host, node.get("service"))]

    # rule node
    if node["type"] == 2:
        entries: list[Any] = []
        for n in node["nodes"]:
            entries += _find_all_leaves(n)
        return entries

    # place holders
    return []


def time_interval_end(
    time_value: int | Literal["next_day", "next_week", "next_month", "next_year"], start_time: float
) -> float | None:
    now = time.localtime(start_time)
    if isinstance(time_value, int):
        return start_time + 7200
    if time_value == "next_day":
        return (
            time.mktime((now.tm_year, now.tm_mon, now.tm_mday, 23, 59, 59, 0, 0, now.tm_isdst)) + 1
        )
    if time_value == "next_week":
        wday = now.tm_wday
        days_plus = 6 - wday
        res = (
            time.mktime((now.tm_year, now.tm_mon, now.tm_mday, 23, 59, 59, 0, 0, now.tm_isdst)) + 1
        )
        res += days_plus * 24 * 3600
        return res
    if time_value == "next_month":
        new_month = now.tm_mon + 1
        if new_month == 13:
            new_year = now.tm_year + 1
            new_month = 1
        else:
            new_year = now.tm_year
        return time.mktime((new_year, new_month, 1, 0, 0, 0, 0, 0, now.tm_isdst))
    if time_value == "next_year":
        return time.mktime((now.tm_year, 12, 31, 23, 59, 59, 0, 0, now.tm_isdst)) + 1
    return None


def time_interval_to_human_readable(next_time_interval, prefix):
    """Generate schedule downtime text from next time interval information

    Args:
        next_time_interval:
            string representing the next time interval. Can either be a periodic interval or the
            duration value
        prefix:
            prefix for the downtime title

    Examples:
        >>> time_interval_to_human_readable("next_day", "schedule an immediate downtime")
        '<b>schedule an immediate downtime until 24:00:00</b>?'
        >>> time_interval_to_human_readable("next_year", "schedule an immediate downtime")
        '<b>schedule an immediate downtime until end of year</b>?'

    Returns:
        string representing the schedule downtime title
    """
    downtime_titles = {
        "next_day": _("<b>%s until 24:00:00</b>?"),
        "next_week": _("<b>%s until sunday night</b>?"),
        "next_month": _("<b>%s until end of month</b>?"),
        "next_year": _("<b>%s until end of year</b>?"),
    }
    try:
        title = downtime_titles[next_time_interval]
    except KeyError:
        duration = int(next_time_interval)
        title = _("<b>%%s of %s length</b>?") % SecondsRenderer.detailed_str(duration)
    return title % prefix


class CommandRemoveDowntime(Command):
    @property
    def ident(self) -> str:
        return "remove_downtimes"

    @property
    def title(self) -> str:
        return _("Remove downtimes")

    @property
    def confirm_title(self) -> str:
        return _("Remove downtimes?")

    @property
    def confirm_button(self) -> LazyString:
        return _l("Remove")

    @property
    def permission(self) -> Permission:
        return PermissionActionDowntimes

    @property
    def tables(self):
        return ["downtime"]

    @property
    def is_shortcut(self) -> bool:
        return True

    @property
    def is_suggested(self) -> bool:
        return True

    def render(self, what) -> None:  # type: ignore[no-untyped-def]
        html.button("_remove_downtimes", _("Remove"))

    def _action(
        self, cmdtag: Literal["HOST", "SVC"], spec: str, row: Row, row_index: int, action_rows: Rows
    ) -> CommandActionResult:
        if request.has_var("_remove_downtimes"):
            return (
                f"DEL_{cmdtag}_DOWNTIME;{spec}",
                self.confirm_dialog_options(
                    cmdtag,
                    row,
                    len(action_rows),
                ),
            )
        return None


class CommandRemoveComments(Command):
    @property
    def ident(self) -> str:
        return "remove_comments"

    @property
    def title(self) -> str:
        return _("Delete comments")

    @property
    def confirm_title(self) -> str:
        return "%s?" % self.title

    @property
    def confirm_button(self) -> LazyString:
        return _l("Delete")

    @property
    def is_shortcut(self) -> bool:
        return True

    @property
    def is_suggested(self) -> bool:
        return True

    @property
    def permission(self) -> Permission:
        return PermissionActionAddComment

    @property
    def tables(self):
        return ["comment"]

    def affected(self, len_action_rows: int, cmdtag: Literal["HOST", "SVC"]) -> HTML:
        return HTML("")

    def confirm_dialog_additions(
        self,
        cmdtag: Literal["HOST", "SVC"],
        row: Row,
        len_action_rows: int,
    ) -> HTML:
        if len_action_rows > 1:
            return HTML(_("Total comments: %d") % len_action_rows)
        return HTML(_("Author: %s") % row["comment_author"])

    def render(self, what) -> None:  # type: ignore[no-untyped-def]
        html.open_div(class_="group")
        html.button("_delete_comments", _("Delete"), cssclass="hot")
        html.button("_cancel", _("Cancel"))
        html.close_div()

    def _action(
        self, cmdtag: Literal["HOST", "SVC"], spec: str, row: Row, row_index: int, action_rows: Rows
    ) -> CommandActionResult:
        if not request.has_var("_delete_comments"):
            return None
        # NOTE: To remove an acknowledgement, we have to use the specialized command, not only the
        # general one. The latter one only removes the comment itself, not the "acknowledged" state.
        # NOTE: We get the commend ID (an int) as a str via the spec parameter (why???), but we need
        # the specification of the host or service for REMOVE_FOO_ACKNOWLEDGEMENT.
        if row.get("comment_entry_type") != 4:  # not an acknowledgement
            rm_ack = []
        elif cmdtag == "HOST":
            rm_ack = [f"REMOVE_HOST_ACKNOWLEDGEMENT;{row['host_name']}"]
        else:
            rm_ack = [f"REMOVE_SVC_ACKNOWLEDGEMENT;{row['host_name']};{row['service_description']}"]
        # Nevertheless, we need the general command, too, even for acknowledgements: The
        # acknowledgement might be persistent, so REMOVE_FOO_ACKNOWLEDGEMENT leaves the comment
        # itself, that's the whole point of being persistent. The only way to get rid of such a
        # comment is via DEL_FOO_COMMENT.
        del_cmt = [f"DEL_HOST_COMMENT;{spec}" if cmdtag == "HOST" else f"DEL_SVC_COMMENT;{spec}"]
        return (rm_ack + del_cmt), self.confirm_dialog_options(
            cmdtag,
            row,
            len(action_rows),
        )
