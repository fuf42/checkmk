#!/usr/bin/env python3
# Copyright (C) 2019 Checkmk GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.


from collections.abc import Mapping, Sequence
from typing import Any

from cmk.base.check_api import passwordstore_get_cmdline
from cmk.base.config import special_agent_info


def agent_cisco_prime_arguments(
    params: Mapping[str, Any], hostname: str, ipaddress: str | None
) -> Sequence[str | tuple[str, str, str]]:
    param_host = params.get("host")
    if param_host == "host_name":
        host = hostname
    elif param_host == "ip_address":
        if ipaddress is None:
            raise ValueError(f"IP address for host '{hostname}' is not set")
        host = ipaddress
    elif isinstance(param_host, tuple) and param_host[0] == "custom":
        host = param_host[1]["host"]
    else:
        # behaviour previous to host configuration
        host = ipaddress or hostname

    basic_auth = params.get("basicauth")
    return [
        str(elem)  # non-str get ignored silently - so turn all elements into `str`
        for chunk in (
            ("--hostname", host),
            ("-u", "{}:{}".format(basic_auth[0], passwordstore_get_cmdline("%s", basic_auth[1])))  #
            if basic_auth
            else (),
            ("--port", params["port"]) if "port" in params else (),  #
            ("--no-tls",) if params.get("no-tls") else (),  #
            ("--no-cert-check",) if params.get("no-cert-check") else (),  #
            ("--timeout", params["timeout"]) if "timeout" in params else (),  #
        )
        for elem in chunk
    ]


special_agent_info["cisco_prime"] = agent_cisco_prime_arguments
