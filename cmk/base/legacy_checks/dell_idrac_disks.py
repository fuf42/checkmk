#!/usr/bin/env python3
# Copyright (C) 2019 Checkmk GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.


from cmk.base.check_api import get_bytes_human_readable, LegacyCheckDefinition
from cmk.base.config import check_info
from cmk.base.plugins.agent_based.agent_based_api.v1 import SNMPTree

from cmk.plugins.lib.dell import DETECT_IDRAC_POWEREDGE

# .1.3.6.1.4.1.674.10892.5.5.1.20.130.4.1.2.1 Physical Disk 0:1:0 --> IDRAC-MIB::physicalDiskName.1
# .1.3.6.1.4.1.674.10892.5.5.1.20.130.4.1.2.2 Physical Disk 0:1:1 --> IDRAC-MIB::physicalDiskName.2
# .1.3.6.1.4.1.674.10892.5.5.1.20.130.4.1.4.1 8 --> IDRAC-MIB::physicalDiskState.1
# .1.3.6.1.4.1.674.10892.5.5.1.20.130.4.1.4.2 8 --> IDRAC-MIB::physicalDiskState.2
# .1.3.6.1.4.1.674.10892.5.5.1.20.130.4.1.11.1 1144064 --> IDRAC-MIB::physicalDiskCapacityInMB.1
# .1.3.6.1.4.1.674.10892.5.5.1.20.130.4.1.11.2 1144064 --> IDRAC-MIB::physicalDiskCapacityInMB.2
# .1.3.6.1.4.1.674.10892.5.5.1.20.130.4.1.22.1 1 --> IDRAC-MIB::physicalDiskSpareState.1
# .1.3.6.1.4.1.674.10892.5.5.1.20.130.4.1.22.2 1 --> IDRAC-MIB::physicalDiskSpareState.2
# .1.3.6.1.4.1.674.10892.5.5.1.20.130.4.1.24.1 3 --> IDRAC-MIB::physicalDiskComponentStatus.1
# .1.3.6.1.4.1.674.10892.5.5.1.20.130.4.1.24.2 3 --> IDRAC-MIB::physicalDiskComponentStatus.2
# .1.3.6.1.4.1.674.10892.5.5.1.20.130.4.1.31.1 0 --> IDRAC-MIB::physicalDiskSmartAlertIndication.1
# .1.3.6.1.4.1.674.10892.5.5.1.20.130.4.1.31.2 0 --> IDRAC-MIB::physicalDiskSmartAlertIndication.2
# .1.3.6.1.4.1.674.10892.5.5.1.20.130.4.1.55.1 Disk 0 in Backplane 1 of Integrated RAID Controller 1 --> IDRAC-MIB::physicalDiskDisplayName.1
# .1.3.6.1.4.1.674.10892.5.5.1.20.130.4.1.55.2 Disk 1 in Backplane 1 of Integrated RAID Controller 1 --> IDRAC-MIB::physicalDiskDisplayName.2


def inventory_dell_idrac_disks(info):
    inventory = []
    for line in info:
        inventory.append((line[0], None))
    return inventory


def check_dell_idrac_disks(item, _no_params, info):
    map_states = {
        "disk_states": {
            "1": (1, "unknown"),
            "2": (0, "ready"),
            "3": (0, "online"),
            "4": (1, "foreign"),
            "5": (2, "offline"),
            "6": (2, "blocked"),
            "7": (2, "failed"),
            "8": (0, "non-raid"),
            "9": (0, "removed"),
        },
        "component_states": {
            "1": (0, "other"),
            "2": (1, "unknown"),
            "3": (0, "OK"),
            "4": (0, "non-critical"),
            "5": (2, "critical"),
            "6": (1, "non-recoverable"),
        },
        "diskpower_states": {
            "1": (0, "no-operation"),
            "2": (1, "REBUILDING"),
            "3": (1, "data-erased"),
            "4": (1, "COPY-BACK"),
        },
    }

    map_spare_state_info = {
        "1": "not a spare",
        "2": "dedicated hotspare",
        "3": "global hotspare",
    }

    for (
        disk_name,
        disk_state,
        capacity_MB,
        spare_state,
        component_state,
        smart_alert,
        diskpower_state,
        display_name,
    ) in info:
        if disk_name == item:
            yield 0, "[{}] Size: {}".format(
                display_name,
                get_bytes_human_readable(int(capacity_MB) * 1024 * 1024),
            )

            for what, what_key, what_text in [
                (disk_state, "disk_states", "Disk state"),
                (component_state, "component_states", "Component state"),
            ]:
                state, state_readable = map_states[what_key][what]
                yield state, f"{what_text}: {state_readable}"

            if smart_alert != "0":
                yield 2, "Smart alert on disk"

            if spare_state != "1":
                yield 0, map_spare_state_info[spare_state]

            if diskpower_state != "1":
                state, state_readable = map_states["diskpower_states"][diskpower_state]
                yield state, "%s" % (state_readable)


check_info["dell_idrac_disks"] = LegacyCheckDefinition(
    detect=DETECT_IDRAC_POWEREDGE,
    fetch=SNMPTree(
        base=".1.3.6.1.4.1.674.10892.5.5.1.20.130.4.1",
        oids=["2", "4", "11", "22", "24", "31", "50", "55"],
    ),
    service_name="Disk %s",
    discovery_function=inventory_dell_idrac_disks,
    check_function=check_dell_idrac_disks,
)
