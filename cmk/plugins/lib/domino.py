#!/usr/bin/env python3
# Copyright (C) 2019 Checkmk GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.
from cmk.agent_based.v2alpha import any_of, startswith

DETECT = any_of(
    startswith(".1.3.6.1.2.1.1.2.0", ".1.3.6.1.4.1.311.1.1.3.1.2"),
    startswith(".1.3.6.1.2.1.1.2.0", ".1.3.6.1.4.1.8072.3.1.10"),
    startswith(".1.3.6.1.2.1.1.2.0", ".1.3.6.1.4.1.8072.3.2.10"),
)
