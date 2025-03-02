#!/usr/bin/env python3
# Copyright (C) 2019 Checkmk GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.

from collections.abc import Mapping
from typing import Any

from cmk.agent_based.v2alpha import check_levels
from cmk.agent_based.v2alpha.type_defs import CheckResult


def check_fan(rpm: float, params: Mapping[str, Any]) -> CheckResult:
    return check_levels(
        rpm,
        levels_lower=params.get("lower"),
        levels_upper=params.get("upper"),
        metric_name="fan" if params.get("output_metrics") else None,
        render_func=lambda r: f"{int(r)} RPM",
        label="Speed",
    )
