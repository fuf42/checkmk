#!/usr/bin/env python3
# Copyright (C) 2019 Checkmk GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.

# Example output from agent:
# 1  Raid Set # 00        3 2250.5GB    0.0GB 123                Normal
# ( # Name Disks TotalCap  FreeCap DiskChannels State )


from cmk.base.check_api import LegacyCheckDefinition, saveint
from cmk.base.config import check_info


def inventory_arc_raid_status(info):
    return [(x[0], saveint(x[-5])) for x in info]


def check_arc_raid_status(item, params, info):
    for line in info:
        if line[0] == item:
            messages = []
            state = 0

            raid_state = line[-1]
            label = ""
            if raid_state in ["Degrade", "Incompleted"]:
                state = 2
                label = "(!!)"
            elif raid_state == "Rebuilding":
                state = 1
                label = "(!)"
            elif raid_state == "Checking":
                state = 0
                label = ""
            elif raid_state != "Normal":
                state = 2
                label = "(!!)"
            messages.append(f"Raid in state: {raid_state}{label}")

            # Check the number of disks
            i_disks = params
            c_disks = saveint(line[-5])
            if i_disks != c_disks:
                messages.append(
                    "Number of disks has changed from %d to %d(!!)" % (i_disks, c_disks)
                )
                state = 2

            return state, ", ".join(messages)

    return 3, "Array not found"


check_info["arc_raid_status"] = LegacyCheckDefinition(
    service_name="Raid Array #%s",
    discovery_function=inventory_arc_raid_status,
    check_function=check_arc_raid_status,
)
