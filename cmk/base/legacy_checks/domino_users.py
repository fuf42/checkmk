#!/usr/bin/env python3
# Copyright (C) 2019 Checkmk GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.


from cmk.base.check_api import LegacyCheckDefinition
from cmk.base.config import check_info
from cmk.base.plugins.agent_based.agent_based_api.v1 import SNMPTree

from cmk.plugins.lib.domino import DETECT

domino_users_default_levels = (1000, 1500)


def inventory_domino_users(info):
    if info:
        yield None, domino_users_default_levels


def check_domino_users(_no_item, params, info):
    if info:
        users = int(info[0][0])
        warn, crit = params
        infotext = "%d Domino Users on Server" % users
        levels = f" (Warn/Crit at {warn}/{crit})"
        perfdata = [("users", users, warn, crit)]
        state = 0
        if users >= crit:
            state = 2
            infotext += levels
        elif users >= warn:
            state = 1
            infotext += levels
        yield state, infotext, perfdata


check_info["domino_users"] = LegacyCheckDefinition(
    detect=DETECT,
    fetch=SNMPTree(
        base=".1.3.6.1.4.1.334.72.1.1.6.3",
        oids=["6"],
    ),
    service_name="Domino Users",
    discovery_function=inventory_domino_users,
    check_function=check_domino_users,
    check_ruleset_name="domino_users",
)
