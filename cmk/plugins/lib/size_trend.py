#!/usr/bin/env python3
# Copyright (C) 2019 Checkmk GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.

import time
from collections.abc import Mapping, MutableMapping
from typing import Any

from cmk.agent_based.v2alpha import check_levels, get_average, get_rate, Metric, render
from cmk.agent_based.v2alpha.type_defs import CheckResult

Levels = tuple[float, float]

MB = 1024 * 1024
SEC_PER_H = 60 * 60
SEC_PER_D = SEC_PER_H * 24


def _level_bytes_to_mb(levels: Levels | None) -> Levels | None:
    """convert levels given as bytes to levels as MB
    >>> _level_bytes_to_mb(None)
    >>> _level_bytes_to_mb((1048576, 2097152))
    (1.0, 2.0)
    """
    if levels is None:
        return None
    return levels[0] / MB, levels[1] / MB


def _reverse_level_signs(levels: Levels | None) -> Levels | None:
    """reverse the sign of all values
    >>> _reverse_level_signs(None)
    >>> _reverse_level_signs((-1, 2))
    (1, -2)
    """
    if levels is None:
        return None
    return -levels[0], -levels[1]


def size_trend(
    *,
    value_store: MutableMapping[str, Any],
    value_store_key: str,
    resource: str,
    levels: Mapping[str, Any],
    used_mb: float,
    size_mb: float,
    timestamp: float | None,
) -> CheckResult:
    """Trend computation for size related checks of disks, ram, etc.
    Trends are computed in two steps. In the first step the delta to
    the last check is computed, using a normal check_mk counter.
    In the second step an average over that counter is computed to
    make a long-term prediction.

    Note:
      This function is experimental and may change in future releases.
      Use at your own risk!

    Args:
      value_store: Retrived value_store by calling check function
      value_store_key (str): The key (prefix) to use in the value_store
      resource (str): The resource in question, e.g. "disk", "ram", "swap".
      levels (dict): Level parameters for the trend computation. Items:
          "trend_range"          : 24,       # interval for the trend in hours
          "trend_perfdata"       : True      # generate perfomance data for trends
          "trend_bytes"          : (10, 20), # change during trend_range
          "trend_shrinking_bytes": (16, 32), # Bytes of shrinking during trend_range
          "trend_perc"           : (1, 2),   # percent change during trend_range
          "trend_shrinking_perc" : (1, 2),   # percent decreasing change during trend_range
          "trend_timeleft"       : (72, 48)  # time left in hours until full
          "trend_showtimeleft    : True      # display time left in infotext
        The item "trend_range" is required. All other items are optional.
      timestamp (float, optional): Time in secs used to calculate the rate
        and average. Defaults to "None".
      used_mb (float): Used space in MB.
      size_mb (float): Max. available space in MB.

    Yields:
      Result- and Metric- instances for the trend computation.
    >>> from cmk.agent_based.v2alpha import GetRateError, Result
    >>> vs = {}
    >>> t0 = time.time()
    >>> for i in range(2):
    ...     try:
    ...         for result in size_trend(
    ...                 value_store=vs,
    ...                 value_store_key="vskey",
    ...                 resource="resource_name",
    ...                 levels={
    ...                     "trend_range": 24,
    ...                     "trend_perfdata": True,
    ...                     "trend_bytes":   (10 * 1024**2, 20 * 1024**2),
    ...                     "trend_perc": (50, 70),
    ...                     "trend_timeleft"   : (72, 48),
    ...                     "trend_showtimeleft": True,
    ...                 },
    ...                 used_mb=100 + 50 * i,      # 50MB/h
    ...                 size_mb=2000,
    ...                 timestamp=t0 + i * 3600):
    ...             print(result)
    ...     except GetRateError:
    ...         pass
    Metric('growth', 1200.0)
    Result(state=<State.CRIT: 2>, summary='trend per 1 day 0 hours: +1.17 GiB (warn/crit at +10.0 MiB/+20.0 MiB)')
    Result(state=<State.WARN: 1>, summary='trend per 1 day 0 hours: +60.00% (warn/crit at +50.00%/+70.00%)')
    Metric('trend', 1200.0, levels=(10.0, 20.0))
    Result(state=<State.CRIT: 2>, summary='Time left until resource_name full: 1 day 13 hours (warn/crit below 3 days 0 hours/2 days 0 hours)')
    Metric('trend_hoursleft', 37.0)
    """

    range_sec = levels["trend_range"] * SEC_PER_H
    timestamp = timestamp or time.time()

    mb_per_sec = get_rate(value_store, "%s.delta" % value_store_key, timestamp, used_mb)

    avg_mb_per_sec = get_average(
        value_store=value_store,
        key="%s.trend" % value_store_key,
        time=timestamp,
        value=mb_per_sec,
        backlog_minutes=range_sec // 60,
    )

    mb_in_range = avg_mb_per_sec * range_sec

    if levels.get("trend_perfdata"):
        yield Metric("growth", mb_per_sec * SEC_PER_D)  # MB / day

    # apply levels for absolute growth in MB / interval
    yield from check_levels(
        mb_in_range * MB,
        levels_upper=levels.get("trend_bytes"),
        levels_lower=_reverse_level_signs(levels.get("trend_shrinking_bytes")),
        render_func=lambda x: ("+" if x >= 0 else "") + render.bytes(x),
        label="trend per %s" % render.timespan(range_sec),
    )

    # apply levels for percentual growth in % / interval
    yield from check_levels(
        mb_in_range * 100 / size_mb,
        levels_upper=levels.get("trend_perc"),
        levels_lower=_reverse_level_signs(levels.get("trend_shrinking_perc")),
        render_func=lambda x: ("+" if x >= 0 else "") + render.percent(x),
        label="trend per %s" % render.timespan(range_sec),
    )

    def to_abs(levels: Levels | None) -> Levels | None:
        if levels is None:
            return None
        return levels[0] / 100 * size_mb, levels[1] / 100 * size_mb

    def mins(levels1: Levels | None, levels2: Levels | None) -> Levels | None:
        return (
            (min(levels1[0], levels2[0]), min(levels1[1], levels2[1]))
            if levels1 and levels2
            else levels1 or levels2 or None  #
        )

    if levels.get("trend_perfdata"):
        yield Metric(
            "trend",
            avg_mb_per_sec * SEC_PER_D,
            levels=mins(
                _level_bytes_to_mb(levels.get("trend_bytes")),
                to_abs(levels.get("trend_perc")),
            ),
        )

    if mb_in_range > 0:
        yield from check_levels(
            # CMK-13217: size_mb - used_mb < 0: the device reported nonsense, resulting in a crash:
            # ValueError("Cannot render negative timespan")
            max(
                (size_mb - used_mb) / mb_in_range * range_sec / SEC_PER_H,
                0,
            ),
            levels_lower=levels.get("trend_timeleft"),
            metric_name="trend_hoursleft" if "trend_showtimeleft" in levels else None,
            render_func=lambda x: render.timespan(x * SEC_PER_H),
            label="Time left until %s full" % resource,
        )
