#!/usr/bin/env python3
# Copyright (C) 2019 Checkmk GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.


# mypy: disable-error-code="arg-type"

import cmk.base.plugins.agent_based.kernel
from cmk.base.check_api import check_levels, LegacyCheckDefinition
from cmk.base.config import check_info
from cmk.base.plugins.agent_based.agent_based_api.v1 import get_rate, get_value_store

#   .--kernel--Counters----------------------------------------------------.
#   |                ____                  _                               |
#   |               / ___|___  _   _ _ __ | |_ ___ _ __ ___                |
#   |              | |   / _ \| | | | '_ \| __/ _ \ '__/ __|               |
#   |              | |__| (_) | |_| | | | | ||  __/ |  \__ \               |
#   |               \____\___/ \__,_|_| |_|\__\___|_|  |___/               |
#   |                                                                      |
#   +----------------------------------------------------------------------+
#   |  Check page faults, context switches and process creations           |
#   '----------------------------------------------------------------------'

kernel_counter_names = cmk.base.plugins.agent_based.kernel.KERNEL_COUNTER_NAMES

kernel_metrics_names = {
    "ctxt": "context_switches",
    "processes": "process_creations",
    "pgmajfault": "major_page_faults",
    "pswpin": "page_swap_in",
    "pswpout": "page_swap_out",
}


# item is one of the keys in /proc/stat or /proc/vmstat
def check_kernel(item, params, parsed):
    timestamp, items = parsed

    if timestamp is None:
        return None

    item_values = items.get(item)

    if item_values is None:
        return None

    if len(item_values) > 1:
        return 3, "item '%s' not unique (found %d times)" % (item, len(item_values))

    counter, value = item_values[0]
    per_sec = get_rate(get_value_store(), "counter", timestamp, value)
    return check_levels(per_sec, counter, params["levels"], unit="/s", boundaries=(0, None))


# This check is deprecated. Please have a look at werk #8969.
check_info["kernel"] = LegacyCheckDefinition(
    service_name="Kernel %s",
    check_function=check_kernel,
    check_ruleset_name="vm_counter",
    check_default_parameters={"levels": None},
)

# .
#   .--kernel.performance--Counters----------------------------------------.
#   |    ____            __                                                |
#   |   |  _ \ ___ _ __ / _| ___  _ __ _ __ ___   __ _ _ __   ___ ___      |
#   |   | |_) / _ \ '__| |_ / _ \| '__| '_ ` _ \ / _` | '_ \ / __/ _ \     |
#   |   |  __/  __/ |  |  _| (_) | |  | | | | | | (_| | | | | (_|  __/     |
#   |   |_|   \___|_|  |_|  \___/|_|  |_| |_| |_|\__,_|_| |_|\___\___|     |
#   |                                                                      |
#   +----------------------------------------------------------------------+
#   |  Check kernel performance counters                                   |
#   '----------------------------------------------------------------------'


def inventory_kernel_performance(parsed):
    _, items = parsed
    for _, name in kernel_counter_names.items():
        data = items.get(name)
        if data is not None and len(data) > 0:
            return [(None, {})]
    return []


def check_kernel_performance(_no_item, params, parsed):
    timestamp, items = parsed
    if timestamp is None:
        return

    for _, item_name in kernel_counter_names.items():
        item_values = items.get(item_name)
        if item_values is None:
            continue

        if len(item_values) > 1:
            yield 3, "item '%s' not unique (found %d times)" % (item_name, len(item_values))

        counter, value = item_values[0]
        rate = get_rate(get_value_store(), counter, timestamp, value)

        if counter in ["pswpin", "pswpout"]:
            levels = params.get(
                "%s_levels" % kernel_metrics_names[counter], (None, None)
            ) + params.get("%s_levels_lower" % kernel_metrics_names[counter], (None, None))
        else:
            levels = params.get(counter)

        yield check_levels(
            rate,
            kernel_metrics_names[counter],
            levels,
            unit="/s",
            infoname=item_name,
            boundaries=(0, None),
        )


check_info["kernel.performance"] = LegacyCheckDefinition(
    service_name="Kernel Performance",
    sections=["kernel"],
    discovery_function=inventory_kernel_performance,
    check_function=check_kernel_performance,
    check_ruleset_name="kernel_performance",
)
