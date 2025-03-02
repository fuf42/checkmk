#!/usr/bin/env python3
# Copyright (C) 2019 Checkmk GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.

# fmt: off
# mypy: disable-error-code=var-annotated


checkname = "ibm_svc_disks"


info = [
    [
        "0",
        "online",
        "",
        "member",
        "sas_hdd",
        "558.4GB",
        "7",
        "V7BRZ_mdisk08",
        "4",
        "1",
        "24",
        "",
        "",
    ],
    [
        "1",
        "online",
        "",
        "member",
        "sas_hdd",
        "558.4GB",
        "7",
        "V7BRZ_mdisk08",
        "3",
        "1",
        "23",
        "",
        "",
    ],
    [
        "2",
        "online",
        "",
        "member",
        "sas_hdd",
        "558.4GB",
        "7",
        "V7BRZ_mdisk08",
        "2",
        "1",
        "22",
        "",
        "",
    ],
    [
        "3",
        "online",
        "",
        "member",
        "sas_hdd",
        "558.4GB",
        "7",
        "V7BRZ_mdisk08",
        "1",
        "1",
        "21",
        "",
        "",
    ],
    [
        "4",
        "online",
        "",
        "member",
        "sas_hdd",
        "558.4GB",
        "7",
        "V7BRZ_mdisk08",
        "0",
        "1",
        "20",
        "",
        "",
    ],
    [
        "5",
        "online",
        "",
        "member",
        "sas_hdd",
        "558.4GB",
        "8",
        "V7BRZ_mdisk09",
        "4",
        "5",
        "4",
        "",
        "",
    ],
    [
        "6",
        "online",
        "",
        "member",
        "sas_hdd",
        "558.4GB",
        "1",
        "V7BRZ_mdisk02",
        "6",
        "1",
        "18",
        "",
        "",
    ],
    [
        "7",
        "online",
        "",
        "member",
        "sas_hdd",
        "558.4GB",
        "1",
        "V7BRZ_mdisk02",
        "5",
        "1",
        "17",
        "",
        "",
    ],
    [
        "8",
        "online",
        "",
        "member",
        "sas_hdd",
        "558.4GB",
        "1",
        "V7BRZ_mdisk02",
        "4",
        "1",
        "16",
        "",
        "",
    ],
    [
        "9",
        "online",
        "",
        "member",
        "sas_hdd",
        "558.4GB",
        "1",
        "V7BRZ_mdisk02",
        "3",
        "1",
        "15",
        "",
        "",
    ],
    [
        "10",
        "online",
        "",
        "member",
        "sas_hdd",
        "558.4GB",
        "1",
        "V7BRZ_mdisk02",
        "2",
        "1",
        "14",
        "",
        "",
    ],
    [
        "11",
        "online",
        "",
        "member",
        "sas_hdd",
        "558.4GB",
        "1",
        "V7BRZ_mdisk02",
        "1",
        "1",
        "13",
        "",
        "",
    ],
    [
        "12",
        "online",
        "",
        "member",
        "sas_hdd",
        "558.4GB",
        "1",
        "V7BRZ_mdisk02",
        "0",
        "1",
        "12",
        "",
        "",
    ],
    [
        "13",
        "online",
        "",
        "member",
        "sas_hdd",
        "558.4GB",
        "16",
        "V7BRZ_mdisk19",
        "6",
        "1",
        "10",
        "",
        "",
    ],
    [
        "14",
        "online",
        "",
        "member",
        "sas_hdd",
        "558.4GB",
        "16",
        "V7BRZ_mdisk19",
        "7",
        "1",
        "11",
        "",
        "",
    ],
    [
        "15",
        "online",
        "",
        "member",
        "sas_hdd",
        "558.4GB",
        "16",
        "V7BRZ_mdisk19",
        "5",
        "1",
        "9",
        "",
        "",
    ],
    [
        "16",
        "online",
        "",
        "member",
        "sas_hdd",
        "558.4GB",
        "16",
        "V7BRZ_mdisk19",
        "3",
        "1",
        "7",
        "",
        "",
    ],
    [
        "17",
        "online",
        "",
        "member",
        "sas_hdd",
        "558.4GB",
        "16",
        "V7BRZ_mdisk19",
        "4",
        "1",
        "8",
        "",
        "",
    ],
    [
        "18",
        "online",
        "",
        "member",
        "sas_hdd",
        "558.4GB",
        "16",
        "V7BRZ_mdisk19",
        "2",
        "1",
        "6",
        "",
        "",
    ],
    [
        "19",
        "online",
        "",
        "member",
        "sas_hdd",
        "558.4GB",
        "16",
        "V7BRZ_mdisk19",
        "1",
        "1",
        "5",
        "",
        "",
    ],
    [
        "20",
        "online",
        "",
        "member",
        "sas_hdd",
        "558.4GB",
        "7",
        "V7RZ_mdisk8",
        "4",
        "1",
        "24",
        "",
        "",
        "inactive",
    ],
    [
        "21",
        "online",
        "",
        "member",
        "sas_hdd",
        "558.4GB",
        "7",
        "V7RZ_mdisk8",
        "3",
        "1",
        "23",
        "",
        "",
        "inactive",
    ],
]


discovery = {"": [(None, {})]}


checks = {
    "": [
        (
            None,
            {"failed_spare_ratio": (1.0, 50.0), "offline_spare_ratio": (1.0, 50.0)},
            [
                (
                    0,
                    "Total raw capacity: 12.0 TiB",
                    [("total_disk_capacity", 13190703559475.195, None, None, None, None)],
                ),
                (0, "Total disks: 22", [("total_disks", 22, None, None, None, None)]),
                (0, "Spare disks: 0", [("spare_disks", 0, None, None, None, None)]),
                (0, "Failed disks: 0", [("failed_disks", 0, None, None, None, None)]),
            ],
        )
    ]
}
