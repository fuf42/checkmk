#!/usr/bin/env python3
# Copyright (C) 2019 Checkmk GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.

import pytest

from cmk.utils.exceptions import MKAgentError, MKGeneralException, MKTimeout
from cmk.utils.hostaddress import HostName

from cmk.snmplib import SNMPBackendEnum

from cmk.checkengine.checkresults import ActiveCheckResult
from cmk.checkengine.exitspec import ExitSpec

from cmk.base.errorhandling import CheckResultErrorHandler


def _handler() -> CheckResultErrorHandler:
    return CheckResultErrorHandler(
        ExitSpec(),
        host_name=HostName("hello"),
        service_name="service_name",
        plugin_name="plugin_name",
        is_cluster=False,
        snmp_backend=SNMPBackendEnum.CLASSIC,
        keepalive=False,
    )


def test_no_error_keeps_status_from_callee() -> None:
    check_result = None

    with _handler() as handler:
        check_result = ActiveCheckResult(
            0,
            "summary",
            ("details", "lots of"),
            ("metrics", "x"),
        )

    assert check_result is not None
    assert check_result.state == 0
    assert check_result.as_text() == "summary | metrics x\ndetails\nlots of"
    assert handler.result is None


def test_MKTimeout_exception_returns_2() -> None:
    with _handler() as handler:
        raise MKTimeout("oops!")

    assert handler.result is not None
    assert handler.result.state == 2
    assert handler.result.as_text() == "Timed out"


def test_MKAgentError_exception_returns_2() -> None:
    with _handler() as handler:
        raise MKAgentError("oops!")

    assert handler.result is not None
    assert handler.result.state == 2
    assert handler.result.as_text() == "oops!"


def test_MKGeneralException_returns_3() -> None:
    with _handler() as handler:
        raise MKGeneralException("kaputt!")

    assert handler.result is not None
    assert handler.result.state == 3
    assert handler.result.as_text() == "kaputt!"


@pytest.mark.usefixtures("disable_debug")
def test_unhandled_exception_returns_3() -> None:
    with _handler() as handler:
        raise ValueError("unexpected :/")

    assert handler.result is not None
    assert handler.result.state == 3
    assert handler.result.as_text().startswith("check failed - please submit a crash report!")
