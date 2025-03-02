#!/usr/bin/env python3
# Copyright (C) 2019 Checkmk GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.

# .1.3.6.1.4.1.9.9.176.1.1.1.0   0  --> CISCO-RF-MIB::cRFStatusUnitId.0
# .1.3.6.1.4.1.9.9.176.1.1.2.0   14 --> CISCO-RF-MIB::cRFStatusUnitState.0
# .1.3.6.1.4.1.9.9.176.1.1.3.0   0  --> CISCO-RF-MIB::cRFStatusPeerUnitId.0
# .1.3.6.1.4.1.9.9.176.1.1.4.0   2  --> CISCO-RF-MIB::cRFStatusPeerUnitState.0
# .1.3.6.1.4.1.9.9.176.1.1.6.0   2  --> CISCO-RF-MIB::cRFStatusDuplexMode.0
# .1.3.6.1.4.1.9.9.176.1.1.8.0   1  --> CISCO-RF-MIB::cRFStatusLastSwactReasonCode.0


from cmk.base.check_api import LegacyCheckDefinition
from cmk.base.config import check_info
from cmk.base.plugins.agent_based.agent_based_api.v1 import all_of, contains, exists, SNMPTree


def inventory_cisco_redundancy(info):
    try:
        swact_reason = info[0][5]
    except IndexError:
        pass
    else:
        if swact_reason != "1":
            return [(None, {"init_states": info[0][:5]})]
    return []


def check_cisco_redundancy(_no_item, params, info):
    map_states = {
        "unit_state": {
            "0": "not found",
            "1": "not known",
            "2": "disabled",
            "3": "initialization",
            "4": "negotiation",
            "5": "standby cold",
            "6": "standby cold config",
            "7": "standby cold file sys",
            "8": "standby cold bulk",
            "9": "standby hot",
            "10": "active fast",
            "11": "active drain",
            "12": "active pre-config",
            "13": "active post-config",
            "14": "active",
            "15": "active extra load",
            "16": "active handback",
        },
        "duplex_mode": {
            "2": "False (SUB-Peer not detected)",
            "1": "True (SUB-Peer detected)",
        },
        "swact_reason": {
            "1": "unsupported",
            "2": "none",
            "3": "not known",
            "4": "user initiated",
            "5": "user forced",
            "6": "active unit failed",
            "7": "active unit removed",
        },
    }

    infotexts = {}
    for what, states in [("now", info[0][:5]), ("init", params["init_states"])]:
        unit_id, unit_state, peer_id, peer_state, duplex_mode = states
        infotexts[what] = "Unit ID: {} ({}), Peer ID: {} ({}), Duplex mode: {}".format(
            unit_id,
            map_states["unit_state"][unit_state],
            peer_id,
            map_states["unit_state"][peer_state],
            map_states["duplex_mode"][duplex_mode],
        )

    unit_id, unit_state, peer_id, peer_state, duplex_mode, _swact_reason = info[0]

    if params["init_states"] == info[0][:5]:
        state = 0
        infotext = "{}, Last swact reason code: {}".format(
            infotexts["now"],
            map_states["swact_reason"][info[0][5]],
        )
    else:
        if unit_state in ["2", "9", "14"] or peer_state in ["2", "9", "14"]:
            state = 1
        else:
            state = 2

        infotext = "Switchover - Old status: {}, New status: {}".format(
            infotexts["init"],
            infotexts["now"],
        )

    if peer_state == "1":
        state = 2

    return state, infotext


check_info["cisco_redundancy"] = LegacyCheckDefinition(
    detect=all_of(contains(".1.3.6.1.2.1.1.1.0", "cisco"), exists(".1.3.6.1.4.1.9.9.176.1.1.*")),
    fetch=SNMPTree(
        base=".1.3.6.1.4.1.9.9.176.1.1",
        oids=["1", "2", "3", "4", "6", "8"],
    ),
    service_name="Redundancy Framework Status",
    discovery_function=inventory_cisco_redundancy,
    check_function=check_cisco_redundancy,
)
