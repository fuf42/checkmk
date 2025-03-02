#!/usr/bin/env python3
# Copyright (C) 2020 Checkmk GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.

from dataclasses import asdict, dataclass
from enum import Enum, unique
from typing import Final

from typing_extensions import TypedDict

from cmk.agent_based.v2alpha import (
    any_of,
    check_levels,
    equals,
    render,
    Result,
    Service,
    startswith,
    State,
    type_defs,
)
from cmk.agent_based.v2alpha.type_defs import CheckResult

DETECT_UPS_GENERIC = any_of(
    equals(".1.3.6.1.2.1.1.2.0", ".1.3.6.1.4.1.232.165.3"),
    equals(".1.3.6.1.2.1.1.2.0", ".1.3.6.1.4.1.476.1.42"),
    equals(".1.3.6.1.2.1.1.2.0", ".1.3.6.1.4.1.534.1"),
    equals(".1.3.6.1.2.1.1.2.0", ".1.3.6.1.4.1.935"),
    equals(".1.3.6.1.2.1.1.2.0", ".1.3.6.1.4.1.8072.3.2.10"),
    equals(".1.3.6.1.2.1.1.2.0", ".1.3.6.1.4.1.2254.2.5"),
    equals(".1.3.6.1.2.1.1.2.0", ".1.3.6.1.4.1.12551.4.0"),
    startswith(".1.3.6.1.2.1.1.2.0", ".1.3.6.1.2.1.33"),
    startswith(".1.3.6.1.2.1.1.2.0", ".1.3.6.1.4.1.534.2"),
    startswith(".1.3.6.1.2.1.1.2.0", ".1.3.6.1.4.1.5491"),
    startswith(".1.3.6.1.2.1.1.2.0", ".1.3.6.1.4.1.705.1"),
    startswith(".1.3.6.1.2.1.1.2.0", ".1.3.6.1.4.1.818.1.100.1"),
    startswith(".1.3.6.1.2.1.1.2.0", ".1.3.6.1.4.1.935"),
)


DETECT_UPS_CPS = startswith(".1.3.6.1.2.1.1.2.0", ".1.3.6.1.4.1.3808.1.1.1")


class UpsParameters(TypedDict, total=False):
    battime: tuple[int, int]
    capacity: tuple[int, int]


CHECK_DEFAULT_PARAMETERS: Final[UpsParameters] = {
    "battime": (0, 0),
    "capacity": (95, 90),
}


@dataclass
class Battery:
    seconds_on_bat: int | None = None
    seconds_left: int | None = None
    percent_charged: int | None = None
    on_battery: bool | None = None
    fault: bool | None = None
    replace: bool | None = None
    low: bool | None = None
    not_charging: bool | None = None
    low_condition: bool | None = None
    on_bypass: bool | None = None
    backup: bool | None = None
    overload: bool | None = None


@unique
class BatteryState(Enum):
    on_battery = "UPS is on battery"
    fault = "Battery fault"
    replace = "Battery to be replaced"
    low = "Low Battery"
    not_charging = "Battery is not charging"
    low_condition = "Battery is at low condition"
    on_bypass = "UPS is on bypass"
    backup = "UPS is on battery backup time"
    overload = "Overload"


def optional_int(value: str, *, factor: int = 1) -> int | None:
    if value.strip() == "":
        return None
    return int(value.strip()) * factor


def optional_yes_or_no(value: str) -> bool | None:
    return True if value.strip() == "1" else False if value.strip() == "2" else None


def discover_ups_capacity(
    section_ups_battery_capacity: Battery | None,
    section_ups_on_battery: Battery | None,
    section_ups_seconds_on_battery: Battery | None,
) -> type_defs.DiscoveryResult:
    yield Service()


def check_ups_capacity(
    params: UpsParameters,
    section_ups_battery_capacity: Battery | None,
    section_ups_on_battery: Battery | None,
    section_ups_seconds_on_battery: Battery | None,
) -> type_defs.CheckResult:
    """Check battery capacity in percent and minutes remaining.
    Apply WARN/CRIT levels and minutes-remaining metric only if on battery.
    """
    battery = _assemble_battery(
        section_ups_battery_capacity,
        section_ups_on_battery,
        section_ups_seconds_on_battery,
    )

    on_battery = _is_on_battery(battery)

    yield from _output_time_remaining(battery.seconds_left, on_battery, params["battime"])
    yield from _output_percent_charged(battery.percent_charged, on_battery, params["capacity"])
    yield from _output_seconds_on_battery(battery.seconds_on_bat)


def _assemble_battery(*sections: Battery | None) -> Battery:
    merged_dict = {
        key: value  #
        for section in sections
        if section is not None  #
        for key, value in asdict(section).items()
        if value is not None  #
    }
    return Battery(**merged_dict)


def _is_on_battery(battery: Battery) -> bool:
    if battery.on_battery is not None:
        return battery.on_battery

    # `seconds_left` can be 0 which not always means that there's no time left but the device might
    # also just be on main power supply
    return battery.seconds_left is not None and bool(battery.seconds_on_bat)


def _output_time_remaining(
    seconds_left: int | None,
    on_battery: bool,
    levels: tuple[int, int],
) -> type_defs.CheckResult:
    # Metric for time left on battery always - check remaining time only when on battery
    ignore_levels = seconds_left == 0 and not on_battery
    if seconds_left is not None:
        yield from check_levels(
            seconds_left,
            metric_name="battery_seconds_remaining",
            levels_lower=None if ignore_levels else (levels[0] * 60, levels[1] * 60),
            render_func=render.timespan,
        )

    if not on_battery:
        yield Result(
            state=State.OK,
            summary="On mains",
        )


def _output_percent_charged(
    percent_charged: int | None,
    on_battery: bool,
    levels: tuple[int, int],
) -> type_defs.CheckResult:
    if percent_charged is None:
        return

    yield from check_levels(
        percent_charged,
        metric_name="battery_capacity",
        levels_lower=levels if on_battery else None,
        render_func=render.percent,
    )


def _output_seconds_on_battery(seconds_on_bat: int | None) -> type_defs.CheckResult:
    # Output time on battery
    if seconds_on_bat is not None and seconds_on_bat > 0:
        yield Result(
            state=State.OK,
            summary=f"Time running on battery: {render.timespan(seconds_on_bat)}",
        )


def discover_ups_battery_state(
    section_ups_battery_warnings: Battery | None,
    section_ups_on_battery: Battery | None,
    section_ups_seconds_on_battery: Battery | None,
) -> type_defs.DiscoveryResult:
    yield Service()


def check_ups_battery_state(
    section_ups_battery_warnings: Battery | None,
    section_ups_on_battery: Battery | None,
    section_ups_seconds_on_battery: Battery | None,
) -> CheckResult:
    battery = _assemble_battery(
        section_ups_battery_warnings,
        section_ups_on_battery,
        section_ups_seconds_on_battery,
    )

    battery.on_battery = _is_on_battery(battery)

    battery_dict = asdict(battery)

    if not any(battery_dict.values()):
        yield Result(state=State.OK, summary="No battery warnings reported")
        return

    yield from (
        Result(state=State.CRIT, summary=entry.value)  #
        for entry in BatteryState  #
        if battery_dict[entry.name] is True  #
    )
