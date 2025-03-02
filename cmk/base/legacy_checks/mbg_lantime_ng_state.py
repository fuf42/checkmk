#!/usr/bin/env python3
# Copyright (C) 2019 Checkmk GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.


from cmk.base.check_api import LegacyCheckDefinition
from cmk.base.check_legacy_includes.mbg_lantime import (
    check_mbg_lantime_state_common,
    MBG_LANTIME_STATE_CHECK_DEFAULT_PARAMETERS,
)
from cmk.base.config import check_info
from cmk.base.plugins.agent_based.agent_based_api.v1 import SNMPTree

from cmk.plugins.lib.mbg_lantime import DETECT_MBG_LANTIME_NG


def inventory_mbg_lantime_ng_state(info):
    if info:
        return [(None, {})]
    return []


def check_mbg_lantime_ng_state(_no_item, params, info):
    states = {
        "0": (2, "not available"),
        "1": (2, "not synchronized"),
        "2": (0, "synchronized"),
    }
    ntp_state, stratum, refclock_name = info[0][:-1]
    # Convert to microseconds
    refclock_offset = float(info[0][-1]) * 1000
    newinfo = [[ntp_state, stratum, refclock_name, refclock_offset]]
    return check_mbg_lantime_state_common(states, _no_item, params, newinfo)


check_info["mbg_lantime_ng_state"] = LegacyCheckDefinition(
    detect=DETECT_MBG_LANTIME_NG,
    fetch=SNMPTree(
        base=".1.3.6.1.4.1.5597.30.0.2",
        oids=["1", "2", "3", "4"],
    ),
    service_name="LANTIME State",
    discovery_function=inventory_mbg_lantime_ng_state,
    check_function=check_mbg_lantime_ng_state,
    check_ruleset_name="mbg_lantime_state",
    check_default_parameters=MBG_LANTIME_STATE_CHECK_DEFAULT_PARAMETERS,
)
