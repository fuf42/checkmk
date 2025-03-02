#!/usr/bin/env python3
# Copyright (C) 2019 Checkmk GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.

from cmk.gui.i18n import _
from cmk.gui.plugins.wato.utils import (
    CheckParameterRulespecWithItem,
    rulespec_registry,
    RulespecGroupCheckParametersEnvironment,
)
from cmk.gui.valuespec import Dictionary, Float, TextInput, Tuple


def _parameter_valuespec_ucs_bladecenter_chassis_voltage():
    return Dictionary(
        help=_("Here you can configure the 3.3V and 12V voltage levels for each chassis."),
        elements=[
            (
                "levels_3v_lower",
                Tuple(
                    title=_("3.3 Volt Output Lower Levels"),
                    elements=[
                        Float(title=_("warning if below or equal"), unit="V", default_value=3.25),
                        Float(title=_("critical if below or equal"), unit="V", default_value=3.20),
                    ],
                ),
            ),
            (
                "levels_3v_upper",
                Tuple(
                    title=_("3.3 Volt Output Upper Levels"),
                    elements=[
                        Float(title=_("warning if above or equal"), unit="V", default_value=3.4),
                        Float(title=_("critical if above or equal"), unit="V", default_value=3.45),
                    ],
                ),
            ),
            (
                "levels_12v_lower",
                Tuple(
                    title=_("12 Volt Output Lower Levels"),
                    elements=[
                        Float(title=_("warning if below or equal"), unit="V", default_value=11.9),
                        Float(title=_("critical if below or equal"), unit="V", default_value=11.8),
                    ],
                ),
            ),
            (
                "levels_12v_upper",
                Tuple(
                    title=_("12 Volt Output Upper Levels"),
                    elements=[
                        Float(title=_("warning if above or equal"), unit="V", default_value=12.1),
                        Float(title=_("critical if above or equal"), unit="V", default_value=12.2),
                    ],
                ),
            ),
        ],
    )


rulespec_registry.register(
    CheckParameterRulespecWithItem(
        check_group_name="ucs_bladecenter_chassis_voltage",
        group=RulespecGroupCheckParametersEnvironment,
        item_spec=lambda: TextInput(title=_("Chassis"), help=_("The identifier of the chassis.")),
        match_type="dict",
        parameter_valuespec=_parameter_valuespec_ucs_bladecenter_chassis_voltage,
        title=lambda: _("UCS Bladecenter Chassis Voltage Levels"),
    )
)
