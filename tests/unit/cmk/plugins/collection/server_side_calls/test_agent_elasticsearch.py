#!/usr/bin/env python3
# Copyright (C) 2019 Checkmk GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.


from cmk.plugins.collection.agent_based.agent_elasticsearch import special_agent_elasticsearch
from cmk.server_side_calls.v1 import HostConfig, IPAddressFamily, PlainTextSecret

TEST_HOST_CONFIG = HostConfig("my_host", "1.2.3.4", "my_alias", IPAddressFamily.IPV4)


def test_agent_elasticsearch_arguments_cert_check() -> None:
    params: dict[str, object] = {
        "hosts": ["testhost"],
        "protocol": "https",
        "infos": ["cluster_health", "nodestats", "stats"],
    }
    (cmd,) = special_agent_elasticsearch.commands_function(params, TEST_HOST_CONFIG, {})
    assert "--no-cert-check" not in cmd.command_arguments

    params["no-cert-check"] = True
    (cmd,) = special_agent_elasticsearch.commands_function(params, TEST_HOST_CONFIG, {})
    assert "--no-cert-check" in cmd.command_arguments


def test_agent_elasticsearch_arguments_password_store() -> None:
    params: dict[str, object] = {
        "hosts": ["testhost"],
        "protocol": "https",
        "infos": ["cluster_health", "nodestats", "stats"],
        "user": "user",
        "password": ("password", "pass"),
    }
    (cmd,) = special_agent_elasticsearch.commands_function(params, TEST_HOST_CONFIG, {})
    assert cmd.command_arguments == [
        "-P",
        "https",
        "-m",
        "cluster_health",
        "nodestats",
        "stats",
        "-u",
        "user",
        "-s",
        PlainTextSecret("pass"),
        "testhost",
    ]
