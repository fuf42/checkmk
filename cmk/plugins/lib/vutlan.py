#!/usr/bin/env python3
# Copyright (C) 2023 Checkmk GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.

from cmk.agent_based.v2alpha import contains

DETECT_VUTLAN_EMS = contains(".1.3.6.1.2.1.1.1.0", "vutlan ems")
