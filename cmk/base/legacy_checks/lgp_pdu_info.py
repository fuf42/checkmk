#!/usr/bin/env python3
# Copyright (C) 2019 Checkmk GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.

# Check has been developed using a Emerson Network Power Rack PDU Card
# Agent App Firmware Version  4.840.0
# Agent Boot Firmware Version 4.540.3
# FDM Version 1209
# GDD Version 45585

# Example info:
# [['1', 'TEST-123-HOST', '1', '535055G103T2010JUN240295', '1']]


from cmk.base.check_api import LegacyCheckDefinition
from cmk.base.config import check_info
from cmk.base.plugins.agent_based.agent_based_api.v1 import SNMPTree

from cmk.plugins.lib.lgp import DETECT_LGP


def inventory_lgp_pdu_info(info):
    if info:
        inv = []
        for pdu in info:
            # Use the SysAssignLabel as item
            inv.append((pdu[2], None))
        return inv
    return []


def check_lgp_pdu_info(item, params, info):
    for pdu in info:
        if pdu[2] == item:
            return (0, "Entry-ID: %s, Label: %s (%s), S/N: %s, Num. RCs: %s" % tuple(pdu))

    return (3, "Device can not be found in SNMP output.")


check_info["lgp_pdu_info"] = LegacyCheckDefinition(
    detect=DETECT_LGP,
    fetch=SNMPTree(
        base=".1.3.6.1.4.1.476.1.42.3.8.20.1",
        oids=["5", "10", "15", "45", "50"],
    ),
    service_name="Liebert PDU Info %s",
    discovery_function=inventory_lgp_pdu_info,
    check_function=check_lgp_pdu_info,
)
