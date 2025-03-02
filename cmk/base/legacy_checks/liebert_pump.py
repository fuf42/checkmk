#!/usr/bin/env python3
# Copyright (C) 2019 Checkmk GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.


from cmk.base.check_api import check_levels, LegacyCheckDefinition
from cmk.base.config import check_info
from cmk.base.plugins.agent_based.agent_based_api.v1 import SNMPTree

from cmk.plugins.lib.liebert import DETECT_LIEBERT, parse_liebert_float

# example output
# .1.3.6.1.4.1.476.1.42.3.9.20.1.10.1.2.1.5298.1 Pump Hours
# .1.3.6.1.4.1.476.1.42.3.9.20.1.10.1.2.1.5298.2 Pump Hours
# .1.3.6.1.4.1.476.1.42.3.9.20.1.20.1.2.1.5298.1 3423
# .1.3.6.1.4.1.476.1.42.3.9.20.1.20.1.2.1.5298.2 1
# .1.3.6.1.4.1.476.1.42.3.9.20.1.30.1.2.1.5298.1 hr
# .1.3.6.1.4.1.476.1.42.3.9.20.1.30.1.2.1.5298.2 hr
# .1.3.6.1.4.1.476.1.42.3.9.20.1.10.1.2.1.5299.1 Pump Hours Threshold
# .1.3.6.1.4.1.476.1.42.3.9.20.1.10.1.2.1.5299.2 Pump Hours Threshold
# .1.3.6.1.4.1.476.1.42.3.9.20.1.20.1.2.1.5299.1 32000
# .1.3.6.1.4.1.476.1.42.3.9.20.1.20.1.2.1.5299.2 32000
# .1.3.6.1.4.1.476.1.42.3.9.20.1.30.1.2.1.5299.1 hr
# .1.3.6.1.4.1.476.1.42.3.9.20.1.30.1.2.1.5299.2 hr


def discover_liebert_pump(section):
    yield from ((item, {}) for item in section if "threshold" not in item.lower())


def check_liebert_pump(item, _no_params, parsed):
    data = parsed.get(item)
    if data is None:
        return

    # TODO: this should be done in the parse function, per OID end.
    for key, (value, _unit) in parsed.items():
        if "Threshold" in key and key.replace(" Threshold", "") == item:
            crit = value

    yield check_levels(data[0], None, (crit, crit), unit=data[1])


check_info["liebert_pump"] = LegacyCheckDefinition(
    detect=DETECT_LIEBERT,
    fetch=SNMPTree(
        base=".1.3.6.1.4.1.476.1.42.3.9.20.1",
        oids=[
            "10.1.2.1.5298",
            "20.1.2.1.5298",
            "30.1.2.1.5298",
            "10.1.2.1.5299",
            "20.1.2.1.5299",
            "30.1.2.1.5299",
        ],
    ),
    parse_function=parse_liebert_float,
    service_name="%s",
    discovery_function=discover_liebert_pump,
    check_function=check_liebert_pump,
)
