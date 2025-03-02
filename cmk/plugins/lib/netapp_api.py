#!/usr/bin/env python3
# Copyright (C) 2019 Checkmk GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.

from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal

from typing_extensions import TypedDict

from cmk.agent_based.v2alpha import Result, Service, State, type_defs

CPUSection = TypedDict(
    "CPUSection",
    {
        "clustermode": dict[str, dict[str, str]],
        "7mode": dict[str, str],
    },
    total=False,
)

Instance = dict[str, str]
SectionMultipleInstances = dict[str, list[Instance]]
SectionSingleInstance = Mapping[str, Instance]
CustomKeys = Sequence[str] | None
ItemFunc = Callable[[str, Instance], str] | None


_DEV_KEYS = {
    "fan": ("cooling-element-is-error", "cooling-element-number"),
    "power supply unit": ("power-supply-is-error", "power-supply-element-number"),
}


def parse_netapp_api_multiple_instances(
    string_table: type_defs.StringTable,
    custom_keys: CustomKeys = None,
    item_func: ItemFunc = None,
) -> SectionMultipleInstances:
    """
    >>> from pprint import pprint
    >>> pprint(parse_netapp_api_multiple_instances([
    ... ['interface e0a', 'mediatype auto-1000t-fd-up', 'flowcontrol full', 'mtusize 9000',
    ...  'ipspace-name default-ipspace', 'mac-address 01:b0:89:22:df:01'],
    ... ['interface e0a', 'mediatype auto-1000t-fd-up', 'flowcontrol full', 'mtusize 9000',
    ...  'ipspace-name default-ipspace', 'mac-address 01:b0:89:22:df:01'],
    ... ['interface ifgrp_sto', 'v4-primary-address.ip-address-info.address 11.12.121.33',
    ...  'v4-primary-address.ip-address-info.addr-family af-inet', 'mtusize 9000',
    ...  'v4-primary-address.ip-address-info.netmask-or-prefix 255.255.255.220',
    ...  'v4-primary-address.ip-address-info.broadcast 12.13.142.33', 'ipspace-name default-ipspace',
    ...  'mac-address 01:b0:89:22:df:01', 'v4-primary-address.ip-address-info.creator vfiler:vfiler0',
    ...  'send_mcasts 1360660', 'recv_errors 0', 'instance_name ifgrp_sto', 'send_errors 0',
    ...  'send_data 323931282332034', 'recv_mcasts 1234567', 'v4-primary-address.ip-address-info.address 11.12.121.21',
    ...  'v4-primary-address.ip-address-info.addr-family af-inet', 'v4-primary-address.ip-address-info.netmask-or-prefix 255.255.253.0',
    ...  'v4-primary-address.ip-address-info.broadcast 14.11.123.255', 'ipspace-name default-ipspace',
    ...  'mac-address 01:b0:89:22:df:02', 'v4-primary-address.ip-address-info.creator vfiler:vfiler0',
    ...  'send_mcasts 166092', 'recv_errors 0', 'instance_name ifgrp_srv-600', 'send_errors 0',
    ...  'send_data 12367443455534', 'recv_mcasts 2308439', 'recv_data 412332323639'],
    ... ]))
    {'e0a': [{'flowcontrol': 'full',
              'interface': 'e0a',
              'ipspace-name': 'default-ipspace',
              'mac-address': '01:b0:89:22:df:01',
              'mediatype': 'auto-1000t-fd-up',
              'mtusize': '9000'},
             {'flowcontrol': 'full',
              'interface': 'e0a',
              'ipspace-name': 'default-ipspace',
              'mac-address': '01:b0:89:22:df:01',
              'mediatype': 'auto-1000t-fd-up',
              'mtusize': '9000'}],
     'ifgrp_sto': [{'instance_name': 'ifgrp_srv-600',
                    'interface': 'ifgrp_sto',
                    'ipspace-name': 'default-ipspace',
                    'mac-address': '01:b0:89:22:df:02',
                    'mtusize': '9000',
                    'recv_data': '412332323639',
                    'recv_errors': '0',
                    'recv_mcasts': '2308439',
                    'send_data': '12367443455534',
                    'send_errors': '0',
                    'send_mcasts': '166092',
                    'v4-primary-address.ip-address-info.addr-family': 'af-inet',
                    'v4-primary-address.ip-address-info.address': '11.12.121.21',
                    'v4-primary-address.ip-address-info.broadcast': '14.11.123.255',
                    'v4-primary-address.ip-address-info.creator': 'vfiler:vfiler0',
                    'v4-primary-address.ip-address-info.netmask-or-prefix': '255.255.253.0'}]}
    """
    if custom_keys is None:
        custom_keys = []

    instances: SectionMultipleInstances = {}
    for line in string_table:
        instance = {}
        if len(line) < 2:
            continue
        name = line[0].split(" ", 1)[1]
        for element in line:
            tokens = element.split(" ", 1)
            instance[tokens[0]] = tokens[1]

        if custom_keys:
            custom_name = []
            for key in custom_keys:
                if key in instance:
                    custom_name.append(instance[key])
            name = ".".join(custom_name)

        if item_func:
            name = item_func(name, instance)

        instances.setdefault(name, [])
        instances[name].append(instance)

    return instances


def parse_netapp_api_single_instance(
    string_table: type_defs.StringTable,
    custom_keys: CustomKeys = None,
    item_func: ItemFunc = None,
) -> SectionSingleInstance:
    """
    >>> from pprint import pprint
    >>> pprint(parse_netapp_api_single_instance([
    ... ['interface e0a', 'mediatype auto-1000t-fd-up', 'flowcontrol full', 'mtusize 9000',
    ...  'ipspace-name default-ipspace', 'mac-address 01:b0:89:22:df:01'],
    ... ['interface e0a', 'v4-primary-address.ip-address-info.address 11.12.121.33',
    ...  'v4-primary-address.ip-address-info.addr-family af-inet', 'mtusize 9000',
    ...  'v4-primary-address.ip-address-info.netmask-or-prefix 255.255.255.220',
    ...  'v4-primary-address.ip-address-info.broadcast 12.13.142.33', 'ipspace-name default-ipspace',
    ...  'mac-address 01:b0:89:22:df:01', 'v4-primary-address.ip-address-info.creator vfiler:vfiler0',
    ...  'send_mcasts 1360660', 'recv_errors 0', 'instance_name ifgrp_sto', 'send_errors 0',
    ...  'send_data 323931282332034', 'recv_mcasts 1234567', 'v4-primary-address.ip-address-info.address 11.12.121.21',
    ...  'v4-primary-address.ip-address-info.addr-family af-inet', 'v4-primary-address.ip-address-info.netmask-or-prefix 255.255.253.0',
    ...  'v4-primary-address.ip-address-info.broadcast 14.11.123.255', 'ipspace-name default-ipspace',
    ...  'mac-address 01:b0:89:22:df:02', 'v4-primary-address.ip-address-info.creator vfiler:vfiler0',
    ...  'send_mcasts 166092', 'recv_errors 0', 'instance_name ifgrp_srv-600', 'send_errors 0',
    ...  'send_data 12367443455534', 'recv_mcasts 2308439', 'recv_data 412332323639'],
    ... ]))
    {'e0a': {'flowcontrol': 'full',
             'interface': 'e0a',
             'ipspace-name': 'default-ipspace',
             'mac-address': '01:b0:89:22:df:01',
             'mediatype': 'auto-1000t-fd-up',
             'mtusize': '9000'}}
    """
    return {
        key: instances[0]
        for key, instances in parse_netapp_api_multiple_instances(
            string_table,
            custom_keys=custom_keys,
            item_func=item_func,
        ).items()
    }


def _single_configured(params: Mapping[str, Any]) -> bool:
    return params["mode"] == "single"


def discover_single(
    params: Mapping[str, Any], section: SectionSingleInstance
) -> type_defs.DiscoveryResult:
    if not _single_configured(params):
        return
    yield from (Service(item=item) for item in section)


def discover_summary(
    params: Mapping[str, Any],
    section: SectionSingleInstance,
) -> type_defs.DiscoveryResult:
    if not section or _single_configured(params):
        return
    yield Service(item="Summary")


def get_single_check(
    device_type: Literal["fan", "power supply unit"]
) -> Callable[[str, SectionSingleInstance], type_defs.CheckResult]:
    error_key, number_key = _DEV_KEYS[device_type]

    def check_single(
        item: str,
        section: SectionSingleInstance,
    ) -> type_defs.CheckResult:
        if not (device := section.get(item)):
            return

        if device[error_key] == "true":
            yield Result(
                state=State.CRIT,
                summary=f"Error in {device_type} {device[number_key]}",
            )
        else:
            yield Result(state=State.OK, summary="Operational state OK")

    return check_single


def _pluralize(thing: str, count: int) -> str:
    return thing if count == 1 else f"{thing}s"


def get_summary_check(
    device_type: Literal["fan", "power supply unit"]
) -> Callable[[str, SectionSingleInstance], type_defs.CheckResult]:
    error_key, _number_key = _DEV_KEYS[device_type]

    def check_summary(
        item: str,
        section: SectionSingleInstance,
    ) -> type_defs.CheckResult:
        total = len(section)
        erred = [k for k, v in section.items() if v.get(error_key) == "true"]
        ok_count = total - len(erred)

        yield Result(state=State.OK, summary=f"OK: {ok_count} of {total}")

        if erred:
            yield Result(
                state=State.CRIT,
                summary=f"Failed: {len(erred)} ({', '.join(erred)})",
            )

    return check_summary
