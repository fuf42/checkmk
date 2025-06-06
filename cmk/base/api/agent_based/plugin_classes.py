#!/usr/bin/env python3
# Copyright (C) 2019 Checkmk GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, NamedTuple, Protocol, Self

from cmk.utils.rulesets import RuleSetName
from cmk.utils.sectionname import SectionName

from cmk.snmplib import SNMPDetectBaseType

from cmk.checkengine.checking import CheckPluginName
from cmk.checkengine.inventory import InventoryPlugin, InventoryPluginName
from cmk.checkengine.sectionparser import ParsedSectionName

from cmk.agent_based.v2 import (
    CheckResult,
    DiscoveryResult,
    HostLabelGenerator,
    StringByteTable,
    StringTable,
)
from cmk.discover_plugins import PluginLocation


@dataclass(frozen=True)
class LegacyPluginLocation:
    file_name: str


CheckFunction = Callable[..., CheckResult]
DiscoveryFunction = Callable[..., DiscoveryResult]

RuleSetTypeName = Literal["merged", "all"]

AgentParseFunction = Callable[[StringTable], Any]

HostLabelFunction = Callable[..., HostLabelGenerator]

SNMPParseFunction = Callable[[list[StringTable]], Any] | Callable[[list[StringByteTable]], Any]

SimpleSNMPParseFunction = Callable[[StringTable], Any] | Callable[[StringByteTable], Any]


class AgentSectionPlugin(NamedTuple):
    name: SectionName
    parsed_section_name: ParsedSectionName
    parse_function: AgentParseFunction
    host_label_function: HostLabelFunction
    host_label_default_parameters: Mapping[str, object] | None
    host_label_ruleset_name: RuleSetName | None
    host_label_ruleset_type: RuleSetTypeName
    supersedes: set[SectionName]
    # We need to allow 'None' for the trivial agent section :-|
    location: PluginLocation | LegacyPluginLocation | None


class _OIDSpecLike(Protocol):
    @property
    def column(self) -> int | str: ...

    @property
    def encoding(self) -> Literal["string", "binary"]: ...

    @property
    def save_to_cache(self) -> bool: ...


class _SNMPTreeLike(Protocol):
    @property
    def base(self) -> str: ...

    @property
    def oids(self) -> Sequence[_OIDSpecLike]: ...


class SNMPSectionPlugin(NamedTuple):
    name: SectionName
    parsed_section_name: ParsedSectionName
    parse_function: SNMPParseFunction
    host_label_function: HostLabelFunction
    host_label_default_parameters: Mapping[str, object] | None
    host_label_ruleset_name: RuleSetName | None
    host_label_ruleset_type: RuleSetTypeName
    detect_spec: SNMPDetectBaseType
    trees: Sequence[_SNMPTreeLike]
    supersedes: set[SectionName]
    location: PluginLocation | LegacyPluginLocation


SectionPlugin = AgentSectionPlugin | SNMPSectionPlugin


class CheckPlugin(NamedTuple):
    name: CheckPluginName
    sections: list[ParsedSectionName]
    service_name: str
    discovery_function: DiscoveryFunction
    discovery_default_parameters: Mapping[str, object] | None
    discovery_ruleset_name: RuleSetName | None
    discovery_ruleset_type: RuleSetTypeName
    check_function: CheckFunction
    check_default_parameters: Mapping[str, object] | None
    check_ruleset_name: RuleSetName | None
    cluster_check_function: CheckFunction | None
    location: PluginLocation | LegacyPluginLocation


@dataclass(frozen=True, kw_only=True)
class AgentBasedPlugins:
    agent_sections: Mapping[SectionName, AgentSectionPlugin]
    snmp_sections: Mapping[SectionName, SNMPSectionPlugin]
    check_plugins: Mapping[CheckPluginName, CheckPlugin]
    inventory_plugins: Mapping[InventoryPluginName, InventoryPlugin]
    errors: Sequence[str]

    @classmethod
    def empty(cls) -> Self:
        return cls(
            agent_sections={},
            snmp_sections={},
            check_plugins={},
            inventory_plugins={},
            errors=(),
        )
