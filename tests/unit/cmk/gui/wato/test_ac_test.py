#!/usr/bin/env python3
# Copyright (C) 2019 Checkmk GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.
from cmk.utils.livestatus_helpers.testing import MockLiveStatusConnection

from cmk.gui.wato._ac_tests import ACTestGenericCheckHelperUsage


def test_local_connection_mocked(mock_livestatus: MockLiveStatusConnection) -> None:
    live = mock_livestatus
    live.set_sites(["local"])
    live.expect_query(
        [
            "GET status",
            "Columns: helper_usage_generic average_latency_generic",
            "ColumnHeaders: off",
        ]
    )
    with live(expect_status_query=False):
        gen = ACTestGenericCheckHelperUsage().execute()
        list(gen)
