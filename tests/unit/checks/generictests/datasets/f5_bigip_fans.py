#!/usr/bin/env python3
# Copyright (C) 2019 Checkmk GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.

# fmt: off
# mypy: disable-error-code=var-annotated
f5_bigip_fans_default_levels = (2000, 500)

checkname = "f5_bigip_fans"

info = [
    [
        ["1", "1", "15574"],
        ["2", "1", "16266"],
        ["3", "1", "15913"],
        ["4", "1", "16266"],
        ["5", "0", "0"],
        ["6", "1", "0"],
    ],
    [],
]

discovery = {
    "": [
        ("Chassis 1", f5_bigip_fans_default_levels),
        ("Chassis 2", f5_bigip_fans_default_levels),
        ("Chassis 3", f5_bigip_fans_default_levels),
        ("Chassis 4", f5_bigip_fans_default_levels),
        ("Chassis 5", f5_bigip_fans_default_levels),
        ("Chassis 6", f5_bigip_fans_default_levels),
    ]
}

checks = {
    "": [
        ("Chassis 1", (2000, 500), [(0, "Speed: 15574 RPM", [])]),
        ("Chassis 2", (2000, 500), [(0, "Speed: 16266 RPM", [])]),
        ("Chassis 3", (2000, 500), [(0, "Speed: 15913 RPM", [])]),
        ("Chassis 4", (2000, 500), [(0, "Speed: 16266 RPM", [])]),
        ("Chassis 5", (2000, 500), [(2, "Speed: 0 RPM (warn/crit below 2000 RPM/500 RPM)", [])]),
        ("Chassis 6", (2000, 500), [(0, "Fan Status: OK", [])]),
    ]
}
