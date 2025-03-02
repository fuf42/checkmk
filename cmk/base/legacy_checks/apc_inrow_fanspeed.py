#!/usr/bin/env python3
# Copyright (C) 2019 Checkmk GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.


from cmk.base.check_api import LegacyCheckDefinition, savefloat
from cmk.base.config import check_info
from cmk.base.plugins.agent_based.agent_based_api.v1 import SNMPTree

from cmk.plugins.lib.apc import DETECT


def inventory_apc_inrow_fanspeed(info):
    if info:
        return [(None, None)]
    return []


def check_apc_inrow_fanspeed(_no_item, _no_params, info):
    value = savefloat(info[0][0]) / 10
    return 0, "Current: %.2f%%" % value, [("fanspeed", value)]


check_info["apc_inrow_fanspeed"] = LegacyCheckDefinition(
    detect=DETECT,
    fetch=SNMPTree(
        base=".1.3.6.1.4.1.318.1.1.13.3.2.2.2",
        oids=["16"],
    ),
    service_name="Fanspeed",
    discovery_function=inventory_apc_inrow_fanspeed,
    check_function=check_apc_inrow_fanspeed,
)
