#!/usr/bin/env python3
# Copyright (C) 2019 Checkmk GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.


from collections.abc import Mapping, Sequence
from typing import Any

from cmk.base.check_api import passwordstore_get_cmdline
from cmk.base.config import special_agent_info


def _get_tag_options(tag_values, prefix):
    options = []
    for key, values in tag_values:
        options.append("--%s-tag-key" % prefix)
        options.append(key)
        options.append("--%s-tag-values" % prefix)
        options += values
    return options


def _get_services_config(services):
    # '--services': {
    #   's3': {'selection': ('tags', [('KEY', ['VAL1', 'VAL2'])])},
    #   'ec2': {'selection': 'all'},
    #   'ebs': {'selection': ('names', ['ebs1', 'ebs2'])},
    # }
    service_args = []
    for service_name, service_config in services.items():
        if service_config is None:
            continue

        if service_config.get("limits"):
            service_args += ["--%s-limits" % service_name]

        selection = service_config.get("selection")
        if not isinstance(selection, tuple):
            # Here: value of selection is 'all' which means there's no
            # restriction (names or tags) to the instances of a specific
            # AWS service. The commandline option already includes this
            # service '--services SERVICE1 SERVICE2 ...' (see below).
            continue

        selection_type, selection_values = selection
        if not selection_values:
            continue

        if selection_type == "names":
            service_args.append("--%s-names" % service_name)
            service_args += selection_values

        elif selection_type == "tags":
            service_args += _get_tag_options(selection_values, service_name)
    return service_args


def _proxy_args(details: Mapping[str, Any]) -> Sequence[Any]:
    proxy_args = ["--proxy-host", details["proxy_host"]]

    if proxy_port := details.get("proxy_port"):
        proxy_args += ["--proxy-port", str(proxy_port)]

    if (proxy_user := details.get("proxy_user")) and (proxy_pwd := details.get("proxy_password")):
        proxy_args += [
            "--proxy-user",
            proxy_user,
            "--proxy-password",
            passwordstore_get_cmdline("%s", proxy_pwd),
        ]
    return proxy_args


def agent_aws_arguments(  # pylint: disable=too-many-branches
    params: Mapping[str, Any], hostname: str, ipaddress: str | None
) -> Sequence[Any]:
    args = [
        "--access-key-id",
        params["access_key_id"],
        "--secret-access-key",
        passwordstore_get_cmdline("%s", params["secret_access_key"]),
        *(_proxy_args(params["proxy_details"]) if "proxy_details" in params else []),
    ]

    global_service_region = params.get("access", {}).get("global_service_region")
    if global_service_region is not None:
        args += ["--global-service-region", global_service_region]

    role_arn_id = params.get("access", {}).get("role_arn_id")
    if role_arn_id:
        args += ["--assume-role"]
        if role_arn_id[0]:
            args += ["--role-arn", role_arn_id[0]]
        if role_arn_id[1]:
            args += ["--external-id", role_arn_id[1]]

    regions = params.get("regions")
    if regions:
        args.append("--regions")
        args += regions

    global_services = params.get("global_services", {})
    if global_services:
        args.append("--global-services")
        # We need to sort the inner services-as-a-dict-params
        # in order to create reliable tests
        args += sorted(global_services)
        args += _get_services_config(global_services)

    services = params.get("services", {})

    # for backwards compatibility
    if "cloudwatch" in services:
        services["cloudwatch_alarms"] = services["cloudwatch"]
        del services["cloudwatch"]

    if services:
        args.append("--services")
        # We need to sort the inner services-as-a-dict-params
        # in order to create reliable tests
        args += sorted(services)
        args += _get_services_config(services)

    if "requests" in services.get("s3", {}):
        args += ["--s3-requests"]

    alarms = services.get("cloudwatch_alarms", {}).get("alarms")
    if alarms:
        # {'alarms': 'all'} is handled by no additionally specified names
        args += ["--cloudwatch-alarms"]
        if isinstance(alarms, tuple):
            args += alarms[1]

    if "cloudfront" in services.get("wafv2", {}):
        args += ["--wafv2-cloudfront"]

    if "cloudfront" in global_services:
        cloudfront_host_assignment = global_services["cloudfront"]["host_assignment"]
        args += ["--cloudfront-host-assignment", cloudfront_host_assignment]

    # '--overall-tags': [('KEY_1', ['VAL_1', 'VAL_2']), ...)],
    args += _get_tag_options(params.get("overall_tags", []), "overall")
    args += [
        "--hostname",
        hostname,
    ]
    args.extend(("--piggyback-naming-convention", params["piggyback_naming_convention"]))
    return args


special_agent_info["aws"] = agent_aws_arguments
