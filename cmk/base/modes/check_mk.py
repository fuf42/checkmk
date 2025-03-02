#!/usr/bin/env python3
# Copyright (C) 2019 Checkmk GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.

import itertools
import logging
import os
import subprocess
import sys
import time
from collections.abc import Callable, Container, Iterable, Mapping, Sequence
from contextlib import suppress
from functools import partial
from pathlib import Path
from typing import Final, Literal, NamedTuple, overload, Protocol, TypeVar

from typing_extensions import TypedDict

import livestatus

import cmk.utils.cleanup
import cmk.utils.debug
import cmk.utils.log as log
import cmk.utils.paths
import cmk.utils.piggyback as piggyback
import cmk.utils.store as store
import cmk.utils.tty as tty
import cmk.utils.version as cmk_version
from cmk.utils.agentdatatype import AgentRawData
from cmk.utils.auto_queue import AutoQueue
from cmk.utils.check_utils import maincheckify
from cmk.utils.cpu_tracking import CPUTracker, Snapshot
from cmk.utils.diagnostics import (
    DiagnosticsModesParameters,
    OPT_CHECKMK_CONFIG_FILES,
    OPT_CHECKMK_LOG_FILES,
    OPT_CHECKMK_OVERVIEW,
    OPT_LOCAL_FILES,
    OPT_OMD_CONFIG,
    OPT_PERFORMANCE_GRAPHS,
)
from cmk.utils.everythingtype import EVERYTHING
from cmk.utils.exceptions import MKBailOut, MKGeneralException, MKTimeout, OnError
from cmk.utils.hostaddress import HostAddress, HostName, Hosts
from cmk.utils.log import console, section
from cmk.utils.resulttype import Result
from cmk.utils.rulesets.ruleset_matcher import RulesetMatcher
from cmk.utils.sectionname import SectionMap, SectionName
from cmk.utils.structured_data import (
    ImmutableTree,
    load_tree,
    MutableTree,
    RawIntervalFromConfig,
    TreeOrArchiveStore,
    UpdateResult,
)
from cmk.utils.tags import TagID
from cmk.utils.timeout import Timeout

from cmk.snmplib import (
    get_single_oid,
    OID,
    oids_to_walk,
    SNMPBackend,
    SNMPBackendEnum,
    SNMPRawData,
    walk_for_export,
)

import cmk.fetchers.snmp as snmp_factory
from cmk.fetchers import FetcherType, get_raw_data
from cmk.fetchers import Mode as FetchMode
from cmk.fetchers.filecache import FileCacheOptions, MaxAge

import cmk.checkengine.inventory as inventory
from cmk.checkengine.checking import CheckPluginName, execute_checkmk_checks, make_timing_results
from cmk.checkengine.checkresults import ActiveCheckResult
from cmk.checkengine.discovery import (
    commandline_discovery,
    execute_check_discovery,
    remove_autochecks_of_host,
)
from cmk.checkengine.fetcher import FetcherFunction, SourceInfo, SourceType
from cmk.checkengine.inventory import HWSWInventoryParameters, InventoryPlugin, InventoryPluginName
from cmk.checkengine.parser import (
    NO_SELECTION,
    parse_raw_data,
    ParserFunction,
    SectionNameCollection,
)
from cmk.checkengine.sectionparser import SectionPlugin
from cmk.checkengine.submitters import get_submitter, ServiceState, Submitter
from cmk.checkengine.summarize import summarize, SummarizerFunction

import cmk.base.api.agent_based.register as agent_based_register
import cmk.base.config as config
import cmk.base.core
import cmk.base.core_nagios
import cmk.base.diagnostics
import cmk.base.dump_host
import cmk.base.ip_lookup as ip_lookup
import cmk.base.obsolete_output as out
import cmk.base.parent_scan
import cmk.base.profiling as profiling
import cmk.base.sources as sources
from cmk.base import plugin_contexts
from cmk.base.api.agent_based.plugin_classes import SNMPSectionPlugin
from cmk.base.api.agent_based.value_store import ValueStoreManager
from cmk.base.checkers import (
    CheckPluginMapper,
    CMKFetcher,
    CMKParser,
    CMKSummarizer,
    DiscoveryPluginMapper,
    HostLabelPluginMapper,
    InventoryPluginMapper,
    SectionPluginMapper,
)
from cmk.base.config import ConfigCache
from cmk.base.core_factory import create_core, get_licensing_handler_type
from cmk.base.errorhandling import CheckResultErrorHandler, create_section_crash_dump
from cmk.base.modes import keepalive_option, Mode, modes, Option
from cmk.base.plugins.server_side_calls import load_active_checks
from cmk.base.sources import make_parser

from cmk.agent_based.v1.value_store import set_value_store_manager

from ._localize import do_localize

# TODO: Investigate all modes and try to find out whether or not we can
# set needs_checks=False for them. This would save a lot of IO/time for
# these modes.

# .
#   .--General options-----------------------------------------------------.
#   |       ____                           _               _               |
#   |      / ___| ___ _ __   ___ _ __ __ _| |   ___  _ __ | |_ ___         |
#   |     | |  _ / _ \ '_ \ / _ \ '__/ _` | |  / _ \| '_ \| __/ __|        |
#   |     | |_| |  __/ | | |  __/ | | (_| | | | (_) | |_) | |_\__ \_       |
#   |      \____|\___|_| |_|\___|_|  \__,_|_|  \___/| .__/ \__|___(_)      |
#   |                                               |_|                    |
#   +----------------------------------------------------------------------+
#   | The general options that are available for all Checkmk modes. Only   |
#   | add new general options in case they are really affecting basic      |
#   | things and used by the most of the modes.                            |
#   '----------------------------------------------------------------------'

_verbosity = 0


def parse_snmp_backend(backend: object) -> SNMPBackendEnum | None:
    match backend:
        case None:
            return None
        case "inline":
            return SNMPBackendEnum.INLINE
        case "classic":
            return SNMPBackendEnum.CLASSIC
        case "stored-walk":
            return SNMPBackendEnum.STORED_WALK
        case _:
            raise ValueError(backend)


def option_verbosity() -> None:
    global _verbosity
    _verbosity += 1
    log.logger.setLevel(log.verbosity_to_log_level(_verbosity))


modes.register_general_option(
    Option(
        long_option="verbose",
        short_option="v",
        short_help="Enable verbose output (Use twice for more)",
        handler_function=option_verbosity,
    )
)


def option_debug() -> None:
    cmk.utils.debug.enable()


modes.register_general_option(
    Option(
        long_option="debug",
        short_help="Let most Python exceptions raise through",
        handler_function=option_debug,
    )
)


def option_profile() -> None:
    profiling.enable()


modes.register_general_option(
    Option(
        long_option="profile",
        short_help="Enable profiling mode",
        handler_function=option_profile,
    )
)


def option_fake_dns(a: HostAddress) -> None:
    ip_lookup.enforce_fake_dns(a)


modes.register_general_option(
    Option(
        long_option="fake-dns",
        short_help="Fake IP addresses of all hosts to be IP. This " "prevents DNS lookups.",
        handler_function=option_fake_dns,
        argument=True,
        argument_descr="IP",
    )
)


# .
#   .--Fetcher options-----------------------------------------------------.
#   |                  _____    _       _                                  |
#   |                 |  ___|__| |_ ___| |__   ___ _ __                    |
#   |                 | |_ / _ \ __/ __| '_ \ / _ \ '__|                   |
#   |                 |  _|  __/ || (__| | | |  __/ |                      |
#   |                 |_|  \___|\__\___|_| |_|\___|_|                      |
#   |                                                                      |
#   |                              _   _                                   |
#   |                   ___  _ __ | |_(_) ___  _ __  ___                   |
#   |                  / _ \| '_ \| __| |/ _ \| '_ \/ __|                  |
#   |                 | (_) | |_) | |_| | (_) | | | \__ \                  |
#   |                  \___/| .__/ \__|_|\___/|_| |_|___/                  |
#   |                       |_|                                            |
#   +----------------------------------------------------------------------+
#   | These options are shared by all modes that use fetchers.             |
#   | These used to be general options, that's why we currently have these |
#   | handler *like*  functions, that only have side-effects.              |
#   | It's not meant to stay this way.                                     |
#   '----------------------------------------------------------------------'
# .


def _handle_fetcher_options(
    options: Mapping[str, object], *, defaults: FileCacheOptions | None = None
) -> FileCacheOptions:
    file_cache_options = defaults or FileCacheOptions()

    if options.get("cache", False):
        file_cache_options = file_cache_options._replace(disabled=False, use_outdated=True)

    if options.get("no-cache", False):
        file_cache_options = file_cache_options._replace(disabled=True, use_outdated=False)

    if options.get("no-tcp", False):
        file_cache_options = file_cache_options._replace(tcp_use_only_cache=True)

    if options.get("usewalk", False):
        snmp_factory.force_stored_walks()
        ip_lookup.enforce_localhost()

    return file_cache_options


_FETCHER_OPTIONS: Final = [
    Option(
        long_option="cache",
        short_help="Read info from data source cache files when existent, even when it "
        "is outdated. Only contact the data sources when the cache file "
        "is absent",
    ),
    Option(
        long_option="no-cache",
        short_help="Never use cached information",
    ),
    Option(
        long_option="no-tcp",
        short_help="Only use cache files. Skip hosts without cache files.",
    ),
    Option(
        long_option="usewalk",
        short_help="Use snmpwalk stored with --snmpwalk",
    ),
]

_SNMP_BACKEND_OPTION: Final = Option(
    long_option="snmp-backend",
    short_help="Override default SNMP backend",
    argument=True,
    argument_descr="inline|classic|stored-walk",
)

# .
#   .--list-hosts----------------------------------------------------------.
#   |              _ _     _        _               _                      |
#   |             | (_)___| |_     | |__   ___  ___| |_ ___                |
#   |             | | / __| __|____| '_ \ / _ \/ __| __/ __|               |
#   |             | | \__ \ ||_____| | | | (_) \__ \ |_\__ \               |
#   |             |_|_|___/\__|    |_| |_|\___/|___/\__|___/               |
#   |                                                                      |
#   '----------------------------------------------------------------------'


def mode_list_hosts(options: dict, args: list[str]) -> None:
    config_cache = config.get_config_cache()
    hosts = _list_all_hosts(
        config_cache,
        config_cache.ruleset_matcher,
        args,
        options,
    )
    out.output("\n".join(hosts))
    if hosts:
        out.output("\n")


# TODO: Does not care about internal group "check_mk"
def _list_all_hosts(
    config_cache: ConfigCache, ruleset_matcher: RulesetMatcher, hostgroups: list[str], options: dict
) -> list[HostName]:
    hosts_config = config_cache.hosts_config
    hostnames: Iterable[HostName]

    all_sites = options.get("all-sites")
    offline = "include-offline" in options

    if all_sites:
        hostnames = filter(
            lambda hn: offline or config_cache.is_online(hn),
            itertools.chain(hosts_config.hosts, hosts_config.clusters, hosts_config.shadow_hosts),
        )
    else:
        hostnames = filter(
            lambda hn: config_cache.is_active(hn) and (offline or config_cache.is_online(hn)),
            itertools.chain(hosts_config.hosts, hosts_config.clusters),
        )

    hostnames = sorted(set(hostnames))
    if not hostgroups:
        return hostnames

    hostlist = []
    for hn in hostnames:
        for hg in config_cache.hostgroups(hn):
            if hg in hostgroups:
                hostlist.append(hn)
                break

    return hostlist


modes.register(
    Mode(
        long_option="list-hosts",
        short_option="l",
        handler_function=mode_list_hosts,
        argument=True,
        argument_descr="G1 G2...",
        argument_optional=True,
        short_help="Print list of all hosts or members of host groups",
        long_help=[
            "Called without argument lists all hosts. You may "
            "specify one or more host groups to restrict the output to hosts "
            "that are in at least one of those groups.",
        ],
        sub_options=[
            Option(
                long_option="all-sites",
                short_help="Include hosts of foreign sites",
            ),
            Option(
                long_option="include-offline",
                short_help="Include offline hosts",
            ),
        ],
    )
)

# .
#   .--list-tag------------------------------------------------------------.
#   |                   _ _     _        _                                 |
#   |                  | (_)___| |_     | |_ __ _  __ _                    |
#   |                  | | / __| __|____| __/ _` |/ _` |                   |
#   |                  | | \__ \ ||_____| || (_| | (_| |                   |
#   |                  |_|_|___/\__|     \__\__,_|\__, |                   |
#   |                                             |___/                    |
#   '----------------------------------------------------------------------'


def mode_list_tag(args: list[str]) -> None:
    hosts = _list_all_hosts_with_tags(tuple(TagID(_) for _ in args))
    out.output("\n".join(sorted(hosts)))
    if hosts:
        out.output("\n")


def _list_all_hosts_with_tags(tags: Sequence[TagID]) -> Sequence[HostName]:
    config_cache = config.get_config_cache()
    hosts_config = config_cache.hosts_config

    if "offline" in tags:
        hostnames = filter(
            lambda hn: config_cache.is_active(hn) and config_cache.is_offline(hn),
            itertools.chain(hosts_config.hosts, hosts_config.clusters),
        )
    else:
        hostnames = filter(
            lambda hn: config_cache.is_active(hn) and config_cache.is_online(hn),
            itertools.chain(hosts_config.hosts, hosts_config.clusters),
        )

    hosts = []
    for h in set(hostnames):
        if config.hosttags_match_taglist(config_cache.tag_list(h), tags):
            hosts.append(h)
    return hosts


modes.register(
    Mode(
        long_option="list-tag",
        handler_function=mode_list_tag,
        argument=True,
        argument_descr="TAG1 TAG2...",
        argument_optional=True,
        short_help="List hosts having certain tags",
        long_help=["Prints all hosts that have all of the specified tags at once."],
    )
)

# .
#   .--list-checks---------------------------------------------------------.
#   |           _ _     _             _               _                    |
#   |          | (_)___| |_       ___| |__   ___  ___| | _____             |
#   |          | | / __| __|____ / __| '_ \ / _ \/ __| |/ / __|            |
#   |          | | \__ \ ||_____| (__| | | |  __/ (__|   <\__ \            |
#   |          |_|_|___/\__|     \___|_| |_|\___|\___|_|\_\___/            |
#   |                                                                      |
#   '----------------------------------------------------------------------'


def mode_list_checks() -> None:
    import cmk.utils.man_pages as man_pages  # pylint: disable=import-outside-toplevel

    all_check_manuals = {maincheckify(n): k for n, k in man_pages.all_man_pages().items()}

    all_checks: list[CheckPluginName | str] = [
        p.name for p in agent_based_register.iter_all_check_plugins()
    ]  #

    # active checks using both new and old API have to be collected
    all_checks += [
        "check_" + name
        for name in itertools.chain(config.active_check_info, load_active_checks()[1])
    ]

    for plugin_name in sorted(all_checks, key=str):
        ds_protocol = _get_ds_protocol(plugin_name)
        title = _get_check_plugin_title(str(plugin_name), all_check_manuals)

        out.output(f"{tty.bold}{plugin_name!s:44}{ds_protocol} {tty.normal}{title}\n")


def _get_ds_protocol(check_name: CheckPluginName | str) -> str:
    if isinstance(check_name, str):  # active check
        return f"{tty.blue}{'active':10}"

    raw_section_is_snmp = {
        isinstance(s, SNMPSectionPlugin)
        for s in agent_based_register.get_relevant_raw_sections(
            check_plugin_names=(check_name,),
            inventory_plugin_names=(),
        ).values()
    }

    if not any(raw_section_is_snmp):
        return f"{tty.yellow}{'agent':10}"

    if all(raw_section_is_snmp):
        return f"{tty.magenta}{'snmp':10}"

    return f"{tty.yellow}agent{tty.white}/{tty.magenta}snmp"


def _get_check_plugin_title(
    check_plugin_name: str,
    all_man_pages: dict[str, str],
) -> str:
    man_filename = all_man_pages.get(check_plugin_name)
    if man_filename is None:
        return "(no man page present)"

    try:
        return cmk.utils.man_pages.get_title_from_man_page(Path(man_filename))
    except MKGeneralException:
        return "(failed to read man page)"


modes.register(
    Mode(
        long_option="list-checks",
        short_option="L",
        handler_function=mode_list_checks,
        needs_config=False,
        short_help="List all available Check_MK checks",
    )
)

# .
#   .--dump-agent----------------------------------------------------------.
#   |        _                                                    _        |
#   |     __| |_   _ _ __ ___  _ __         __ _  __ _  ___ _ __ | |_      |
#   |    / _` | | | | '_ ` _ \| '_ \ _____ / _` |/ _` |/ _ \ '_ \| __|     |
#   |   | (_| | |_| | | | | | | |_) |_____| (_| | (_| |  __/ | | | |_      |
#   |    \__,_|\__,_|_| |_| |_| .__/       \__,_|\__, |\___|_| |_|\__|     |
#   |                         |_|                |___/                     |
#   '----------------------------------------------------------------------'


def mode_dump_agent(options: Mapping[str, object], hostname: HostName) -> None:
    file_cache_options = _handle_fetcher_options(options)

    try:
        snmp_backend_override = parse_snmp_backend(options.get("snmp-backend"))
    except ValueError as exc:
        raise MKBailOut("Unknown SNMP backend") from exc

    try:
        config_cache = config.get_config_cache()
        config_cache.ruleset_matcher.ruleset_optimizer.set_all_processed_hosts({hostname})

        hosts_config = config.make_hosts_config()
        if hostname in hosts_config.clusters:
            raise MKBailOut("Can not be used with cluster hosts")

        ipaddress = config.lookup_ip_address(config_cache, hostname)
        check_interval = config_cache.check_mk_check_interval(hostname)

        output = []
        # Show errors of problematic data sources
        has_errors = False
        for source in sources.make_sources(
            hostname,
            ipaddress,
            ConfigCache.address_family(hostname),
            config_cache=config_cache,
            is_cluster=False,
            simulation_mode=config.simulation_mode,
            file_cache_options=file_cache_options,
            file_cache_max_age=MaxAge(
                checking=config.check_max_cachefile_age,
                discovery=1.5 * check_interval,
                inventory=1.5 * check_interval,
            ),
            snmp_backend_override=snmp_backend_override,
        ):
            source_info = source.source_info()
            if source_info.fetcher_type is FetcherType.SNMP:
                continue

            raw_data = get_raw_data(
                source.file_cache(
                    simulation=config.simulation_mode,
                    file_cache_options=file_cache_options,
                ),
                source.fetcher(),
                FetchMode.CHECKING,
            )
            host_sections = parse_raw_data(
                make_parser(
                    config_cache,
                    source_info,
                    checking_sections=config_cache.make_checking_sections(
                        hostname, selected_sections=NO_SELECTION
                    ),
                    keep_outdated=file_cache_options.keep_outdated,
                    logger=log.logger,
                ),
                raw_data,
                selection=NO_SELECTION,
            )
            source_results = summarize(
                hostname,
                ipaddress,
                host_sections,
                exit_spec=config_cache.exit_code_spec(hostname, source_info.ident),
                time_settings=config.get_config_cache().get_piggybacked_hosts_time_settings(
                    piggybacked_hostname=hostname,
                ),
                is_piggyback=config_cache.is_piggyback_host(hostname),
                fetcher_type=source_info.fetcher_type,
            )
            if any(r.state != 0 for r in source_results):
                console.error(
                    "ERROR [%s]: %s\n",
                    source_info.ident,
                    ", ".join(r.summary for r in source_results),
                )
                has_errors = True
            if raw_data.is_ok():
                assert raw_data.ok is not None
                output.append(raw_data.ok)

        out.output(b"".join(output).decode(errors="surrogateescape"))
        if has_errors:
            sys.exit(1)
    except Exception as e:
        if cmk.utils.debug.enabled():
            raise
        raise MKBailOut("Unhandled exception: %s" % e)


modes.register(
    Mode(
        long_option="dump-agent",
        short_option="d",
        handler_function=mode_dump_agent,
        argument=True,
        argument_descr="HOSTNAME|ADDRESS",
        short_help="Show raw information from agent",
        long_help=[
            "Shows the raw information received from the given host. For regular "
            "hosts it shows the agent output plus possible piggyback information. "
            "Does not work on clusters but only on real hosts. "
        ],
        sub_options=[*_FETCHER_OPTIONS[:3], _SNMP_BACKEND_OPTION],
    )
)

# .
#   .--dump----------------------------------------------------------------.
#   |                         _                                            |
#   |                      __| |_   _ _ __ ___  _ __                       |
#   |                     / _` | | | | '_ ` _ \| '_ \                      |
#   |                    | (_| | |_| | | | | | | |_) |                     |
#   |                     \__,_|\__,_|_| |_| |_| .__/                      |
#   |                                          |_|                         |
#   '----------------------------------------------------------------------'


def mode_dump_hosts(hostlist: Iterable[HostName]) -> None:
    config_cache = config.get_config_cache()
    hosts_config = config_cache.hosts_config
    all_hosts = {
        hn
        for hn in itertools.chain(hosts_config.hosts, hosts_config.clusters)
        if config_cache.is_active(hn) and config_cache.is_online(hn)
    }
    hosts = set(hostlist)
    if not hosts:
        hosts = all_hosts

    config_cache.ruleset_matcher.ruleset_optimizer.set_all_processed_hosts(hosts)
    for hostname in sorted(hosts - all_hosts):
        sys.stderr.write(f"unknown host: {hostname}\n")
    for hostname in sorted(hosts & all_hosts):
        cmk.base.dump_host.dump_host(config_cache, hostname)


modes.register(
    Mode(
        long_option="dump",
        short_option="D",
        handler_function=mode_dump_hosts,
        argument=True,
        argument_descr="H1 H2...",
        argument_optional=True,
        short_help="Dump info about all or some hosts",
        long_help=[
            "Dumps out the complete configuration and information "
            "about one, several or all hosts. It shows all services, hostgroups, "
            "contacts and other information about that host.",
        ],
    )
)

# .
#   .--paths---------------------------------------------------------------.
#   |                                  _   _                               |
#   |                      _ __   __ _| |_| |__  ___                       |
#   |                     | '_ \ / _` | __| '_ \/ __|                      |
#   |                     | |_) | (_| | |_| | | \__ \                      |
#   |                     | .__/ \__,_|\__|_| |_|___/                      |
#   |                     |_|                                              |
#   '----------------------------------------------------------------------'


def mode_paths() -> None:
    inst = 1
    conf = 2
    data = 3
    pipe = 4
    local = 5
    directory = 1
    fil = 2

    paths = [
        (cmk.utils.paths.modules_dir, directory, inst, "Main components of check_mk"),
        (cmk.utils.paths.checks_dir, directory, inst, "Checks"),
        (str(cmk.utils.paths.notifications_dir), directory, inst, "Notification scripts"),
        (cmk.utils.paths.inventory_dir, directory, inst, "Inventory plugins"),
        (cmk.utils.paths.agents_dir, directory, inst, "Agents for operating systems"),
        (str(cmk.utils.paths.doc_dir), directory, inst, "Documentation files"),
        (cmk.utils.paths.web_dir, directory, inst, "Check_MK's web pages"),
        (cmk.utils.paths.check_manpages_dir, directory, inst, "Check manpages (for check_mk -M)"),
        (cmk.utils.paths.lib_dir, directory, inst, "Binary plugins (architecture specific)"),
        (str(cmk.utils.paths.pnp_templates_dir), directory, inst, "Templates for PNP4Nagios"),
    ]
    if config.monitoring_core == "nagios":
        paths += [
            (cmk.utils.paths.nagios_startscript, fil, inst, "Startscript for Nagios daemon"),
            (cmk.utils.paths.nagios_binary, fil, inst, "Path to Nagios executable"),
            (cmk.utils.paths.nagios_config_file, fil, conf, "Main configuration file of Nagios"),
            (
                cmk.utils.paths.nagios_conf_dir,
                directory,
                conf,
                "Directory where Nagios reads all *.cfg files",
            ),
            (
                cmk.utils.paths.nagios_objects_file,
                fil,
                data,
                "File into which Nagios configuration is written",
            ),
            (cmk.utils.paths.nagios_status_file, fil, data, "Path to Nagios status.dat"),
            (cmk.utils.paths.nagios_command_pipe_path, fil, pipe, "Nagios' command pipe"),
            (cmk.utils.paths.check_result_path, fil, pipe, "Nagios' check results directory"),
        ]

    paths += [
        (cmk.utils.paths.default_config_dir, directory, conf, "Directory that contains main.mk"),
        (
            cmk.utils.paths.check_mk_config_dir,
            directory,
            conf,
            "Directory containing further *.mk files",
        ),
        (
            cmk.utils.paths.apache_config_dir,
            directory,
            conf,
            "Directory where Apache reads all config files",
        ),
        (cmk.utils.paths.htpasswd_file, fil, conf, "Users/Passwords for HTTP basic authentication"),
        (cmk.utils.paths.var_dir, directory, data, "Base working directory for variable data"),
        (cmk.utils.paths.autochecks_dir, directory, data, "Checks found by inventory"),
        (cmk.utils.paths.precompiled_hostchecks_dir, directory, data, "Precompiled host checks"),
        (cmk.utils.paths.snmpwalks_dir, directory, data, "Stored snmpwalks (output of --snmpwalk)"),
        (cmk.utils.paths.counters_dir, directory, data, "Current state of performance counters"),
        (cmk.utils.paths.tcp_cache_dir, directory, data, "Cached output from agents"),
        (
            cmk.utils.paths.logwatch_dir,
            directory,
            data,
            "Unacknowledged logfiles of logwatch extension",
        ),
        (
            cmk.utils.paths.livestatus_unix_socket,
            fil,
            pipe,
            "Socket of Check_MK's livestatus module",
        ),
        (str(cmk.utils.paths.local_checks_dir), directory, local, "Locally installed checks"),
        (
            str(cmk.utils.paths.local_notifications_dir),
            directory,
            local,
            "Locally installed notification scripts",
        ),
        (
            str(cmk.utils.paths.local_inventory_dir),
            directory,
            local,
            "Locally installed inventory plugins",
        ),
        (
            str(cmk.utils.paths.local_check_manpages_dir),
            directory,
            local,
            "Locally installed check man pages",
        ),
        (
            str(cmk.utils.paths.local_agents_dir),
            directory,
            local,
            "Locally installed agents and plugins",
        ),
        (
            str(cmk.utils.paths.local_web_dir),
            directory,
            local,
            "Locally installed Multisite addons",
        ),
        (
            str(cmk.utils.paths.local_pnp_templates_dir),
            directory,
            local,
            "Locally installed PNP templates",
        ),
        (str(cmk.utils.paths.local_doc_dir), directory, local, "Locally installed documentation"),
        (
            str(cmk.utils.paths.local_locale_dir),
            directory,
            local,
            "Locally installed localizations",
        ),
    ]

    def show_paths(title: str, t: int) -> None:
        if t != inst:
            out.output("\n")
        out.output(tty.bold + title + tty.normal + "\n")
        for path, filedir, typp, descr in paths:
            if typp == t:
                if filedir == directory:
                    path += "/"
                out.output("  %-47s: %s%s%s\n" % (descr, tty.bold + tty.blue, path, tty.normal))

    for title, t in [
        ("Files copied or created during installation", inst),
        ("Configuration files edited by you", conf),
        ("Data created by Nagios/Check_MK at runtime", data),
        ("Sockets and pipes", pipe),
        ("Locally installed addons", local),
    ]:
        show_paths(title, t)


modes.register(
    Mode(
        long_option="paths",
        handler_function=mode_paths,
        needs_config=False,
        short_help="List all pathnames and directories",
    )
)

# .
#   .--package-------------------------------------------------------------.
#   |                                 _                                    |
#   |                _ __   __ _  ___| | ____ _  __ _  ___                 |
#   |               | '_ \ / _` |/ __| |/ / _` |/ _` |/ _ \                |
#   |               | |_) | (_| | (__|   < (_| | (_| |  __/                |
#   |               | .__/ \__,_|\___|_|\_\__,_|\__, |\___|                |
#   |               |_|                         |___/                      |
#   '----------------------------------------------------------------------'


_DEPRECATION_MSG = "This command is no longer supported. Please use `mkp%s` instead."


def _fail_with_deprecation_msg(argv: list[str]) -> Literal[1]:
    sys.stdout.write(_DEPRECATION_MSG % " ".join(("", *argv)) + "\n")
    return 1


modes.register(
    Mode(
        long_option="package",
        short_option="P",
        handler_function=_fail_with_deprecation_msg,
        argument=True,
        argument_descr="COMMAND",
        argument_optional=True,
        short_help="DEPRECATED: Do package operations",
        long_help=[_DEPRECATION_MSG % ""],
        needs_config=False,
        needs_checks=False,
    )
)

# .
#   .--localize------------------------------------------------------------.
#   |                    _                 _ _                             |
#   |                   | | ___   ___ __ _| (_)_______                     |
#   |                   | |/ _ \ / __/ _` | | |_  / _ \                    |
#   |                   | | (_) | (_| (_| | | |/ /  __/                    |
#   |                   |_|\___/ \___\__,_|_|_/___\___|                    |
#   |                                                                      |
#   '----------------------------------------------------------------------'


def mode_localize(args: list[str]) -> None:
    do_localize(args)


modes.register(
    Mode(
        long_option="localize",
        handler_function=mode_localize,
        needs_config=False,
        needs_checks=False,
        argument=True,
        argument_descr="COMMAND",
        argument_optional=True,
        short_help="Do localization operations",
        long_help=[
            "Brings you into localization mode. You can create "
            "and/or improve the localization of Check_MKs GUI. "
            "Call without arguments for a help on localization."
        ],
    )
)

# .
#   .--update-dns-cache----------------------------------------------------.
#   |                        _            _                                |
#   |        _   _ _ __   __| |        __| |_ __  ___        ___           |
#   |       | | | | '_ \ / _` | _____ / _` | '_ \/ __|_____ / __|          |
#   |       | |_| | |_) | (_| ||_____| (_| | | | \__ \_____| (__ _         |
#   |        \__,_| .__/ \__,_(_)     \__,_|_| |_|___/      \___(_)        |
#   |             |_|                                                      |
#   '----------------------------------------------------------------------'


def mode_update_dns_cache() -> None:
    config_cache = config.get_config_cache()
    hosts_config = config_cache.hosts_config
    ip_lookup.update_dns_cache(
        ip_lookup_configs=(
            config_cache.ip_lookup_config(hn)
            for hn in set(hosts_config.hosts).union(hosts_config.clusters)
            if config_cache.is_active(hn) and config_cache.is_online(hn)
        ),
        configured_ipv6_addresses=config.ipaddresses,
        configured_ipv4_addresses=config.ipv6addresses,
        simulation_mode=config.simulation_mode,
        override_dns=HostAddress(config.fake_dns) if config.fake_dns is not None else None,
    )


modes.register(
    Mode(
        long_option="update-dns-cache",
        handler_function=mode_update_dns_cache,
        short_help="Update IP address lookup cache",
    )
)

# .
#   .--clean.-piggyb.------------------------------------------------------.
#   |        _                               _                   _         |
#   |    ___| | ___  __ _ _ __         _ __ (_) __ _  __ _ _   _| |__      |
#   |   / __| |/ _ \/ _` | '_ \  _____| '_ \| |/ _` |/ _` | | | | '_ \     |
#   |  | (__| |  __/ (_| | | | ||_____| |_) | | (_| | (_| | |_| | |_) |    |
#   |   \___|_|\___|\__,_|_| |_(_)    | .__/|_|\__, |\__, |\__, |_.__(_)   |
#   |                                 |_|      |___/ |___/ |___/           |
#   '----------------------------------------------------------------------'


def mode_cleanup_piggyback() -> None:
    time_settings = config.get_config_cache().get_piggybacked_hosts_time_settings()
    piggyback.cleanup_piggyback_files(time_settings)


modes.register(
    Mode(
        long_option="cleanup-piggyback",
        handler_function=mode_cleanup_piggyback,
        short_help="Cleanup outdated piggyback files",
    )
)

# .
#   .--scan-parents--------------------------------------------------------.
#   |                                                         _            |
#   |    ___  ___ __ _ _ __        _ __   __ _ _ __ ___ _ __ | |_ ___      |
#   |   / __|/ __/ _` | '_ \ _____| '_ \ / _` | '__/ _ \ '_ \| __/ __|     |
#   |   \__ \ (_| (_| | | | |_____| |_) | (_| | | |  __/ | | | |_\__ \     |
#   |   |___/\___\__,_|_| |_|     | .__/ \__,_|_|  \___|_| |_|\__|___/     |
#   |                             |_|                                      |
#   '----------------------------------------------------------------------'


def mode_scan_parents(options: dict, args: list[str]) -> None:
    config.load(exclude_parents_mk=True)
    config_cache = config.get_config_cache()
    hosts_config = config.make_hosts_config()

    if "procs" in options:
        config.max_num_processes = options["procs"]

    cmk.base.parent_scan.do_scan_parents(
        config_cache,
        hosts_config,
        HostName(config.monitoring_host) if config.monitoring_host is not None else None,
        [HostName(hn) for hn in args],
    )


modes.register(
    Mode(
        long_option="scan-parents",
        handler_function=mode_scan_parents,
        needs_config=False,
        needs_checks=False,
        argument=True,
        argument_descr="HOST1 HOST2...",
        argument_optional=True,
        short_help="Autoscan parents, create conf.d/parents.mk",
        long_help=[
            "Uses traceroute in order to automatically detect hosts's parents. "
            "It creates the file conf.d/parents.mk which "
            "defines gateway hosts and parent declarations.",
        ],
        sub_options=[
            Option(
                long_option="procs",
                argument=True,
                argument_descr="N",
                argument_conv=int,
                short_help="Start up to N processes in parallel. Defaults to 50.",
            ),
        ],
    )
)

# .
#   .--snmptranslate-------------------------------------------------------.
#   |                            _                       _       _         |
#   |  ___ _ __  _ __ ___  _ __ | |_ _ __ __ _ _ __  ___| | __ _| |_ ___   |
#   | / __| '_ \| '_ ` _ \| '_ \| __| '__/ _` | '_ \/ __| |/ _` | __/ _ \  |
#   | \__ \ | | | | | | | | |_) | |_| | | (_| | | | \__ \ | (_| | ||  __/  |
#   | |___/_| |_|_| |_| |_| .__/ \__|_|  \__,_|_| |_|___/_|\__,_|\__\___|  |
#   |                     |_|                                              |
#   '----------------------------------------------------------------------'


def mode_snmptranslate(walk_filename: str) -> None:
    if not walk_filename:
        raise MKGeneralException("Please provide the name of a SNMP walk file")

    walk_path = Path(cmk.utils.paths.snmpwalks_dir) / walk_filename
    if not walk_path.exists():
        raise MKGeneralException("The walk '%s' does not exist" % walk_path)

    command: list[str] = [
        "snmptranslate",
        "-m",
        "ALL",
        "-M+%s" % cmk.utils.paths.local_mib_dir,
        "-",
    ]
    with walk_path.open("rb") as walk_file:
        walk = walk_file.read().split(b"\n")
    while walk[-1] == b"":
        del walk[-1]

    # to be compatible to previous version of this script, we do not feed
    # to original walk to snmptranslate (which would be possible) but a
    # version without values. The output should look like:
    # "[full oid] [value] --> [translated oid]"
    walk_without_values = b"\n".join(line.split(b" ", 1)[0] for line in walk)

    completed_process = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        check=False,
        input=walk_without_values,
    )

    data_translated = completed_process.stdout.split(b"\n")
    # remove last empty line (some tools add a '\n' at the end of the file, others not)
    if data_translated[-1] == b"":
        del data_translated[-1]

    if len(walk) != len(data_translated):
        raise MKGeneralException("call to snmptranslate returned a ambiguous result")

    for element_input, element_translated in zip(walk, data_translated):
        sys.stdout.buffer.write(element_input.strip())
        sys.stdout.buffer.write(b" --> ")
        sys.stdout.buffer.write(element_translated.strip())
        sys.stdout.buffer.write(b"\n")


modes.register(
    Mode(
        long_option="snmptranslate",
        handler_function=mode_snmptranslate,
        needs_config=False,
        needs_checks=False,
        argument=True,
        argument_descr="HOST",
        short_help="Do snmptranslate on walk",
        long_help=[
            "Does not contact the host again, but reuses the hosts walk from the "
            "directory %s. You can add further MIBs to the directory %s."
            % (cmk.utils.paths.snmpwalks_dir, cmk.utils.paths.local_mib_dir)
        ],
    )
)

# .
#   .--snmpwalk------------------------------------------------------------.
#   |                                                   _ _                |
#   |            ___ _ __  _ __ ___  _ ____      ____ _| | | __            |
#   |           / __| '_ \| '_ ` _ \| '_ \ \ /\ / / _` | | |/ /            |
#   |           \__ \ | | | | | | | | |_) \ V  V / (_| | |   <             |
#   |           |___/_| |_|_| |_| |_| .__/ \_/\_/ \__,_|_|_|\_\            |
#   |                               |_|                                    |
#   '----------------------------------------------------------------------'

_oids: list[str] = []
_extra_oids: list[str] = []
_SNMPWalkOptions = dict[str, list[OID]]


def _do_snmpwalk(options: _SNMPWalkOptions, *, backend: SNMPBackend) -> None:
    if not os.path.exists(cmk.utils.paths.snmpwalks_dir):
        os.makedirs(cmk.utils.paths.snmpwalks_dir)

    # TODO: What about SNMP management boards?
    try:
        _do_snmpwalk_on(
            options, cmk.utils.paths.snmpwalks_dir + "/" + backend.hostname, backend=backend
        )
    except Exception as e:
        console.error(f"Error walking {backend.hostname}: {e}\n")
        if cmk.utils.debug.enabled():
            raise
    cmk.utils.cleanup.cleanup_globals()


def _do_snmpwalk_on(options: _SNMPWalkOptions, filename: str, *, backend: SNMPBackend) -> None:
    console.verbose("%s:\n" % backend.hostname)

    oids = oids_to_walk(options)

    with Path(filename).open("w", encoding="utf-8") as file:
        for rows in _execute_walks_for_dump(oids, backend=backend):
            for oid, value in rows:
                file.write(f"{oid} {value}\n")
            console.verbose("%d variables.\n" % len(rows))

    console.verbose(f"Wrote fetched data to {tty.bold}{filename}{tty.normal}.\n")


def _execute_walks_for_dump(
    oids: list[OID], *, backend: SNMPBackend
) -> Iterable[list[tuple[OID, str]]]:
    for oid in oids:
        try:
            console.verbose('Walk on "%s"...\n' % oid)
            yield walk_for_export(backend.walk(oid, context=""))
        except Exception as e:
            console.error("Error: %s\n" % e)
            if cmk.utils.debug.enabled():
                raise


def mode_snmpwalk(options: dict, hostnames: list[str]) -> None:
    if _oids:
        options["oids"] = _oids
    if _extra_oids:
        options["extraoids"] = _extra_oids
    if "oids" in options and "extraoids" in options:
        raise MKGeneralException("You cannot specify --oid and --extraoid at the same time.")

    try:
        snmp_backend_override = parse_snmp_backend(options.get("snmp-backend"))
    except ValueError as exc:
        raise MKBailOut("Unknown SNMP backend") from exc

    if not hostnames:
        raise MKBailOut("Please specify host names to walk on.")

    config_cache = config.get_config_cache()

    for hostname in (HostName(hn) for hn in hostnames):
        ipaddress = config.lookup_ip_address(config_cache, hostname)
        if not ipaddress:
            raise MKGeneralException("Failed to gather IP address of %s" % hostname)

        snmp_config = config_cache.make_snmp_config(hostname, ipaddress, SourceType.HOST)
        if snmp_backend_override is not None:
            snmp_config = snmp_config._replace(snmp_backend=snmp_backend_override)
        _do_snmpwalk(options, backend=snmp_factory.make_backend(snmp_config, log.logger))


modes.register(
    Mode(
        long_option="snmpwalk",
        handler_function=mode_snmpwalk,
        argument=True,
        argument_descr="HOST1 HOST2...",
        argument_optional=True,
        short_help="Do snmpwalk on one or more hosts",
        long_help=[
            "Does a complete snmpwalk for the specified hosts both "
            "on the standard MIB and the enterprises MIB and stores the "
            "result in the directory '%s'. Use the option --oid one or several "
            "times in order to specify alternative OIDs to walk. You need to "
            "specify numeric OIDs. If you want to keep the two standard OIDS "
            ".1.3.6.1.2.1 and .1.3.6.1.4.1 then use --extraoid for just adding "
            "additional OIDs to walk." % cmk.utils.paths.snmpwalks_dir,
        ],
        sub_options=[
            _SNMP_BACKEND_OPTION,
            Option(
                long_option="extraoid",
                argument=True,
                argument_descr="A",
                argument_conv=_extra_oids.append,
                short_help="Walk also on this OID, in addition to mib-2 and "
                "enterprises. You can specify this option multiple "
                "times.",
            ),
            Option(
                long_option="oid",
                argument=True,
                argument_descr="A",
                argument_conv=_oids.append,
                short_help="Walk on this OID instead of mib-2 and enterprises. "
                "You can specify this option multiple times.",
            ),
        ],
    )
)

# .
#   .--snmpget-------------------------------------------------------------.
#   |                                                   _                  |
#   |              ___ _ __  _ __ ___  _ __   __ _  ___| |_                |
#   |             / __| '_ \| '_ ` _ \| '_ \ / _` |/ _ \ __|               |
#   |             \__ \ | | | | | | | | |_) | (_| |  __/ |_                |
#   |             |___/_| |_|_| |_| |_| .__/ \__, |\___|\__|               |
#   |                                 |_|    |___/                         |
#   '----------------------------------------------------------------------'


def mode_snmpget(options: Mapping[str, object], args: Sequence[str]) -> None:
    if not args:
        raise MKBailOut("You need to specify an OID.")
    try:
        snmp_backend_override = parse_snmp_backend(options.get("snmp-backend"))
    except ValueError as exc:
        raise MKBailOut("Unknown SNMP backend") from exc

    config_cache = config.get_config_cache()
    oid, *hostnames = args

    if not hostnames:
        hosts_config = config_cache.hosts_config
        hostnames.extend(
            host
            for host in frozenset(hosts_config.hosts)
            if config_cache.is_active(host)
            and config_cache.is_online(host)
            and config_cache.is_snmp_host(host)
        )

    assert hostnames
    for hostname in (HostName(hn) for hn in hostnames):
        ipaddress = config.lookup_ip_address(config_cache, hostname)
        if not ipaddress:
            raise MKGeneralException("Failed to gather IP address of %s" % hostname)

        snmp_config = config_cache.make_snmp_config(hostname, ipaddress, SourceType.HOST)
        if snmp_backend_override is not None:
            snmp_config = snmp_config._replace(snmp_backend=snmp_backend_override)

        backend = snmp_factory.make_backend(snmp_config, log.logger)
        value = get_single_oid(oid, single_oid_cache={}, backend=backend)
        sys.stdout.write(f"{backend.hostname} ({backend.address}): {value!r}\n")


modes.register(
    Mode(
        long_option="snmpget",
        handler_function=mode_snmpget,
        argument=True,
        argument_descr="OID [HOST1 HOST2...]",
        argument_optional=True,
        short_help="Fetch single OID from one or multiple hosts",
        long_help=[
            "Does a snmpget on the given OID on one or multiple hosts. In case "
            "no host is given, all known SNMP hosts are queried."
        ],
        sub_options=[_SNMP_BACKEND_OPTION],
    )
)

# .
#   .--flush---------------------------------------------------------------.
#   |                         __ _           _                             |
#   |                        / _| |_   _ ___| |__                          |
#   |                       | |_| | | | / __| '_ \                         |
#   |                       |  _| | |_| \__ \ | | |                        |
#   |                       |_| |_|\__,_|___/_| |_|                        |
#   |                                                                      |
#   '----------------------------------------------------------------------'


def mode_flush(hosts: list[HostName]) -> None:  # pylint: disable=too-many-branches
    config_cache = config.get_config_cache()
    hosts_config = config_cache.hosts_config
    ruleset_matcher = config_cache.ruleset_matcher

    if not hosts:
        hosts = sorted(
            {
                hn
                for hn in itertools.chain(hosts_config.hosts, hosts_config.clusters)
                if config_cache.is_active(hn) and config_cache.is_online(hn)
            }
        )

    for host in hosts:
        out.output("%-20s: " % host)
        flushed = False

        # counters
        try:
            os.remove(cmk.utils.paths.counters_dir + "/" + host)
            out.output(tty.bold + tty.blue + " counters")
            flushed = True
        except OSError:
            pass

        # cache files
        d = 0
        cache_dir = cmk.utils.paths.tcp_cache_dir
        if os.path.exists(cache_dir):
            for f in os.listdir(cache_dir):
                if f == host or f.startswith(host + "."):
                    try:
                        os.remove(cache_dir + "/" + f)
                        d += 1
                        flushed = True
                    except OSError:
                        pass
            if d == 1:
                out.output(tty.bold + tty.green + " cache")
            elif d > 1:
                out.output(tty.bold + tty.green + " cache(%d)" % d)

        # piggy files from this as source host
        d = piggyback.remove_source_status_file(host)
        if d:
            out.output(tty.bold + tty.magenta + " piggyback(1)")

        # logfiles
        log_dir = cmk.utils.paths.logwatch_dir + "/" + host
        if os.path.exists(log_dir):
            d = 0
            for f in os.listdir(log_dir):
                if f not in [".", ".."]:
                    try:
                        os.remove(log_dir + "/" + f)
                        d += 1
                        flushed = True
                    except OSError:
                        pass
            if d > 0:
                out.output(tty.bold + tty.magenta + " logfiles(%d)" % d)

        # autochecks
        count = sum(
            remove_autochecks_of_host(
                node,
                host,
                config_cache.effective_host,
                partial(config.service_description, ruleset_matcher),
            )
            for node in config_cache.nodes_of(host) or [host]
        )
        # config_cache.remove_autochecks(host)
        if count:
            flushed = True
            out.output(tty.bold + tty.cyan + " autochecks(%d)" % count)

        # inventory
        path = cmk.utils.paths.var_dir + "/inventory/" + host
        if os.path.exists(path):
            os.remove(path)
            out.output(tty.bold + tty.yellow + " inventory")

        if not flushed:
            out.output("(nothing)")

        out.output(tty.normal + "\n")


modes.register(
    Mode(
        long_option="flush",
        handler_function=mode_flush,
        argument=True,
        argument_descr="HOST1 HOST2...",
        argument_optional=True,
        needs_config=True,
        short_help="Flush all data of some or all hosts",
        long_help=[
            "Deletes all runtime data belonging to a host. This includes "
            "the inventorized checks, the state of performance counters, "
            "cached agent output, and logfiles. Precompiled host checks "
            "are not deleted.",
        ],
    )
)

# .
#   .--nagios-config-------------------------------------------------------.
#   |                     _                                  __ _          |
#   |   _ __   __ _  __ _(_) ___  ___        ___ ___  _ __  / _(_) __ _    |
#   |  | '_ \ / _` |/ _` | |/ _ \/ __|_____ / __/ _ \| '_ \| |_| |/ _` |   |
#   |  | | | | (_| | (_| | | (_) \__ \_____| (_| (_) | | | |  _| | (_| |   |
#   |  |_| |_|\__,_|\__, |_|\___/|___/      \___\___/|_| |_|_| |_|\__, |   |
#   |               |___/                                         |___/    |
#   '----------------------------------------------------------------------'


def mode_dump_nagios_config(args: list[HostName]) -> None:
    from cmk.utils.config_path import VersionedConfigPath

    from cmk.base.core_nagios import create_config  # pylint: disable=import-outside-toplevel

    create_config(
        sys.stdout,
        next(VersionedConfigPath.current()),
        args if len(args) else None,
        get_licensing_handler_type().make(),
    )


modes.register(
    Mode(
        long_option="nagios-config",
        short_option="N",
        handler_function=mode_dump_nagios_config,
        argument=True,
        argument_descr="HOST1 HOST2...",
        argument_optional=True,
        short_help="Output Nagios configuration",
        long_help=[
            "Outputs the Nagios configuration. You may optionally add a list "
            "of hosts. In that case the configuration is generated only for "
            "that hosts (useful for debugging).",
        ],
    )
)

# .
#   .--update--------------------------------------------------------------.
#   |                                   _       _                          |
#   |                   _   _ _ __   __| | __ _| |_ ___                    |
#   |                  | | | | '_ \ / _` |/ _` | __/ _ \                   |
#   |                  | |_| | |_) | (_| | (_| | ||  __/                   |
#   |                   \__,_| .__/ \__,_|\__,_|\__\___|                   |
#   |                        |_|                                           |
#   '----------------------------------------------------------------------'


def mode_update() -> None:
    from cmk.base.core_config import do_create_config  # pylint: disable=import-outside-toplevel

    config_cache = config.get_config_cache()
    hosts_config = config_cache.hosts_config
    try:
        with cmk.base.core.activation_lock(mode=config.restart_locking):
            do_create_config(
                core=create_core(config.monitoring_core),
                config_cache=config_cache,
                all_hosts=hosts_config.hosts,
                duplicates=sorted(
                    hosts_config.duplicates(
                        lambda hn: config_cache.is_active(hn) and config_cache.is_online(hn)
                    )
                ),
            )
    except Exception as e:
        console.error("Configuration Error: %s\n" % e)
        if cmk.utils.debug.enabled():
            raise
        sys.exit(1)


modes.register(
    Mode(
        long_option="update",
        short_option="U",
        handler_function=mode_update,
        short_help="Create core config",
        long_help=[
            "Updates the core configuration based on the current Checkmk "
            "configuration. When using the Nagios core, the precompiled host "
            "checks are created and the nagios configuration is updated. "
            "When using the CheckMK Microcore, the core configuration is created "
            "and the configuration for the Core helper processes is being created.",
            "The agent bakery is updating the agents.",
        ],
    )
)

# .
#   .--restart-------------------------------------------------------------.
#   |                                 _             _                      |
#   |                   _ __ ___  ___| |_ __ _ _ __| |_                    |
#   |                  | '__/ _ \/ __| __/ _` | '__| __|                   |
#   |                  | | |  __/\__ \ || (_| | |  | |_                    |
#   |                  |_|  \___||___/\__\__,_|_|   \__|                   |
#   |                                                                      |
#   '----------------------------------------------------------------------'


def mode_restart(args: Sequence[HostName]) -> None:
    config_cache = config.get_config_cache()
    hosts_config = config_cache.hosts_config
    cmk.base.core.do_restart(
        config_cache,
        create_core(config.monitoring_core),
        hosts_to_update=set(args) if args else None,
        locking_mode=config.restart_locking,
        all_hosts=hosts_config.hosts,
        duplicates=sorted(
            hosts_config.duplicates(
                lambda hn: config_cache.is_active(hn) and config_cache.is_online(hn)
            )
        ),
    )


modes.register(
    Mode(
        long_option="restart",
        short_option="R",
        argument=True,
        argument_optional=True,
        argument_descr="[HostA, HostB]",
        long_help=[
            "You may add hostnames as additional arguments. This enables the incremental "
            "activate mechanism, only compiling these hostnames and using cached data for all "
            "other hosts. Only supported with Checkmk Microcore."
        ],
        handler_function=mode_restart,
        short_help="Create core config + core restart",
    )
)

# .
#   .--reload--------------------------------------------------------------.
#   |                             _                 _                      |
#   |                    _ __ ___| | ___   __ _  __| |                     |
#   |                   | '__/ _ \ |/ _ \ / _` |/ _` |                     |
#   |                   | | |  __/ | (_) | (_| | (_| |                     |
#   |                   |_|  \___|_|\___/ \__,_|\__,_|                     |
#   |                                                                      |
#   '----------------------------------------------------------------------'


def mode_reload(args: Sequence[HostName]) -> None:
    config_cache = config.get_config_cache()
    hosts_config = config_cache.hosts_config
    cmk.base.core.do_reload(
        config_cache,
        create_core(config.monitoring_core),
        hosts_to_update=set(args) if args else None,
        locking_mode=config.restart_locking,
        all_hosts=hosts_config.hosts,
        duplicates=sorted(
            hosts_config.duplicates(
                lambda hn: config_cache.is_active(hn) and config_cache.is_online(hn)
            ),
        ),
    )


modes.register(
    Mode(
        long_option="reload",
        short_option="O",
        argument=True,
        argument_optional=True,
        argument_descr="[HostA, HostB]",
        long_help=[
            "You may add hostnames as additional arguments. This enables the incremental "
            "activate mechanism, only compiling these hostnames and using cached data for all "
            "other hosts. Only supported with Checkmk Microcore."
        ],
        handler_function=mode_reload,
        short_help="Create core config + core reload",
    )
)

# .
#   .--man-----------------------------------------------------------------.
#   |                                                                      |
#   |                        _ __ ___   __ _ _ __                          |
#   |                       | '_ ` _ \ / _` | '_ \                         |
#   |                       | | | | | | (_| | | | |                        |
#   |                       |_| |_| |_|\__,_|_| |_|                        |
#   |                                                                      |
#   '----------------------------------------------------------------------'


def mode_man(args: list[str]) -> None:
    import cmk.utils.man_pages as man_pages  # pylint: disable=import-outside-toplevel

    if args:
        man_pages.ConsoleManPageRenderer(args[0]).paint()
    else:
        man_pages.print_man_page_table()


modes.register(
    Mode(
        long_option="man",
        short_option="M",
        handler_function=mode_man,
        argument=True,
        argument_descr="CHECKTYPE",
        argument_optional=True,
        needs_config=False,
        needs_checks=False,
        short_help="Show manpage for check CHECKTYPE",
        long_help=[
            "Shows documentation about a check type. If /usr/bin/less is "
            "available it is used as pager. Exit by pressing Q. "
            "Use -M without an argument to show a list of all manual pages."
        ],
    )
)

# .
#   .--browse-man----------------------------------------------------------.
#   |    _                                                                 |
#   |   | |__  _ __ _____      _____  ___       _ __ ___   __ _ _ __       |
#   |   | '_ \| '__/ _ \ \ /\ / / __|/ _ \_____| '_ ` _ \ / _` | '_ \      |
#   |   | |_) | | | (_) \ V  V /\__ \  __/_____| | | | | | (_| | | | |     |
#   |   |_.__/|_|  \___/ \_/\_/ |___/\___|     |_| |_| |_|\__,_|_| |_|     |
#   |                                                                      |
#   '----------------------------------------------------------------------'


def mode_browse_man() -> None:
    import cmk.utils.man_pages as man_pages  # pylint: disable=import-outside-toplevel

    man_pages.print_man_page_browser()


modes.register(
    Mode(
        long_option="browse-man",
        short_option="m",
        handler_function=mode_browse_man,
        needs_config=False,
        needs_checks=False,
        short_help="Open interactive manpage browser",
    )
)

# .
#   .--automation----------------------------------------------------------.
#   |                   _                        _   _                     |
#   |        __ _ _   _| |_ ___  _ __ ___   __ _| |_(_) ___  _ __          |
#   |       / _` | | | | __/ _ \| '_ ` _ \ / _` | __| |/ _ \| '_ \         |
#   |      | (_| | |_| | || (_) | | | | | | (_| | |_| | (_) | | | |        |
#   |       \__,_|\__,_|\__\___/|_| |_| |_|\__,_|\__|_|\___/|_| |_|        |
#   |                                                                      |
#   '----------------------------------------------------------------------'


def mode_automation(args: list[str]) -> None:
    import cmk.base.automations as automations  # pylint: disable=import-outside-toplevel

    if not args:
        raise automations.MKAutomationError("You need to provide arguments")

    # At least for the automation calls that buffer and handle the stdout/stderr on their own
    # we can now enable this. In the future we should remove this call for all automations calls and
    # handle the output in a common way.
    if args[0] not in [
        "restart",
        "reload",
        "start",
        "create-diagnostics-dump",
        "try-inventory",
        "service-discovery-preview",
    ]:
        log.clear_console_logging()

    sys.exit(automations.automations.execute(args[0], args[1:]))


modes.register(
    Mode(
        long_option="automation",
        handler_function=mode_automation,
        needs_config=False,
        needs_checks=False,
        argument=True,
        argument_descr="COMMAND...",
        argument_optional=True,
        short_help="Internal helper to invoke Check_MK actions",
    )
)

# .
#   .--notify--------------------------------------------------------------.
#   |                                 _   _  __                            |
#   |                     _ __   ___ | |_(_)/ _|_   _                      |
#   |                    | '_ \ / _ \| __| | |_| | | |                     |
#   |                    | | | | (_) | |_| |  _| |_| |                     |
#   |                    |_| |_|\___/ \__|_|_|  \__, |                     |
#   |                                           |___/                      |
#   '----------------------------------------------------------------------'


def mode_notify(options: dict, args: list[str]) -> int | None:
    import cmk.base.notify as notify  # pylint: disable=import-outside-toplevel

    with store.lock_checkmk_configuration():
        config.load(with_conf_d=True, validate_hosts=False)
    return notify.do_notify(options, args)


modes.register(
    Mode(
        long_option="notify",
        handler_function=mode_notify,
        needs_config=False,
        needs_checks=False,
        argument=True,
        argument_descr="MODE",
        argument_optional=True,
        short_help="Used to send notifications from core",
        # TODO: Write long help
        sub_options=[
            Option(
                long_option="log-to-stdout",
                short_help="Also write log messages to console",
            ),
            keepalive_option,
        ],
    )
)


# .
#   .--check-discovery-----------------------------------------------------.
#   |       _     _               _ _                                      |
#   |   ___| |__ | | __        __| (_)___  ___ _____   _____ _ __ _   _    |
#   |  / __| '_ \| |/ / _____ / _` | / __|/ __/ _ \ \ / / _ \ '__| | | |   |
#   | | (__| | | |   < |_____| (_| | \__ \ (_| (_) \ V /  __/ |  | |_| |   |
#   |  \___|_| |_|_|\_(_)     \__,_|_|___/\___\___/ \_/ \___|_|   \__, |   |
#   |                                                             |___/    |
#   '----------------------------------------------------------------------'


def mode_check_discovery(
    options: Mapping[str, object],
    hostname: HostName,
    *,
    active_check_handler: Callable[[HostName, str], object],
    keepalive: bool,
) -> int:
    file_cache_options = _handle_fetcher_options(options)
    try:
        snmp_backend_override = parse_snmp_backend(options.get("snmp-backend"))
    except ValueError as exc:
        raise MKBailOut("Unknown SNMP backend") from exc

    config_cache = config.get_config_cache()
    ruleset_matcher = config_cache.ruleset_matcher
    ruleset_matcher.ruleset_optimizer.set_all_processed_hosts({hostname})
    check_interval = config_cache.check_mk_check_interval(hostname)
    discovery_file_cache_max_age = 1.5 * check_interval if file_cache_options.use_outdated else 0
    fetcher = CMKFetcher(
        config_cache,
        file_cache_options=file_cache_options,
        force_snmp_cache_refresh=False,
        mode=FetchMode.DISCOVERY,
        on_error=OnError.RAISE,
        selected_sections=NO_SELECTION,
        simulation_mode=config.simulation_mode,
        max_cachefile_age=MaxAge(
            checking=config.check_max_cachefile_age,
            discovery=discovery_file_cache_max_age,
            inventory=1.5 * check_interval,
        ),
        snmp_backend_override=snmp_backend_override,
    )
    parser = CMKParser(
        config_cache,
        selected_sections=NO_SELECTION,
        keep_outdated=file_cache_options.keep_outdated,
        logger=logging.getLogger("cmk.base.discovery"),
    )
    summarizer = CMKSummarizer(
        config_cache,
        hostname,
        override_non_ok_state=None,
    )
    error_handler = CheckResultErrorHandler(
        exit_spec=config_cache.exit_code_spec(hostname),
        host_name=hostname,
        service_name="Check_MK Discovery",
        plugin_name="discover",
        is_cluster=hostname in config_cache.hosts_config.clusters,
        snmp_backend=config_cache.get_snmp_backend(hostname),
        keepalive=keepalive,
    )
    check_result = ActiveCheckResult(3, "unknown error")
    with error_handler:
        fetched = fetcher(hostname, ip_address=None)
        with plugin_contexts.current_host(hostname):
            check_result = execute_check_discovery(
                hostname,
                is_cluster=hostname in config_cache.hosts_config.clusters,
                cluster_nodes=config_cache.nodes_of(hostname) or (),
                params=config_cache.discovery_check_parameters(hostname),
                fetched=((f[0], f[1]) for f in fetched),
                parser=parser,
                summarizer=summarizer,
                section_plugins=SectionPluginMapper(),
                section_error_handling=lambda section_name, raw_data: create_section_crash_dump(
                    operation="parsing",
                    section_name=section_name,
                    section_content=raw_data,
                    host_name=hostname,
                    rtc_package=None,
                ),
                host_label_plugins=HostLabelPluginMapper(ruleset_matcher=ruleset_matcher),
                plugins=DiscoveryPluginMapper(ruleset_matcher=ruleset_matcher),
                ignore_service=config_cache.service_ignored,
                ignore_plugin=config_cache.check_plugin_ignored,
                get_effective_host=config_cache.effective_host,
                find_service_description=partial(config.service_description, ruleset_matcher),
                enforced_services=config_cache.enforced_services_table(hostname),
            )

    if error_handler.result is not None:
        check_result = error_handler.result

    active_check_handler(hostname, check_result.as_text())
    if keepalive:
        console.verbose(check_result.as_text())
    else:
        with suppress(IOError):
            sys.stdout.write(check_result.as_text() + "\n")
            sys.stdout.flush()
    return check_result.state


def register_mode_check_discovery(
    *, active_check_handler: Callable[[HostName, str], object], keepalive: bool
) -> None:
    modes.register(
        Mode(
            long_option="check-discovery",
            handler_function=partial(
                mode_check_discovery, active_check_handler=active_check_handler, keepalive=keepalive
            ),
            argument=True,
            argument_descr="HOSTNAME",
            short_help="Check for not yet monitored services",
            long_help=[
                "Make Check_MK behave as monitoring plugins that checks if an "
                "inventory would find new or vanished services for the host. "
                "If configured to do so, this will queue those hosts for automatic "
                "autodiscovery"
            ],
            sub_options=[*_FETCHER_OPTIONS, _SNMP_BACKEND_OPTION],
        )
    )


if cmk_version.edition() is cmk_version.Edition.CRE:
    register_mode_check_discovery(active_check_handler=lambda *args: None, keepalive=False)

# .
#   .--discover------------------------------------------------------------.
#   |                     _ _                                              |
#   |                  __| (_)___  ___ _____   _____ _ __                  |
#   |                 / _` | / __|/ __/ _ \ \ / / _ \ '__|                 |
#   |                | (_| | \__ \ (_| (_) \ V /  __/ |                    |
#   |                 \__,_|_|___/\___\___/ \_/ \___|_|                    |
#   |                                                                      |
#   '----------------------------------------------------------------------'

_TName = TypeVar("_TName", str, CheckPluginName, InventoryPluginName, SectionName)


def _convert_sections_argument(arg: str) -> set[SectionName]:
    try:
        # kindly forgive empty strings
        return {SectionName(n) for n in arg.split(",") if n}
    except ValueError as exc:
        raise MKBailOut("Error in --detect-sections argument: %s" % exc)


_option_sections = Option(
    long_option="detect-sections",
    short_help=(
        "Comma separated list of sections. The provided sections (but no more) will be"
        " available (skipping SNMP detection)"
    ),
    argument=True,
    argument_descr="S",
    argument_conv=_convert_sections_argument,
)


def _get_plugins_option(type_: type[_TName]) -> Option:
    def _convert_plugins_argument(arg: str) -> set[_TName]:
        try:
            # kindly forgive empty strings
            return {type_(n) for n in arg.split(",") if n}
        except ValueError as exc:
            raise MKBailOut("Error in --plugins argument: %s" % exc) from exc

    return Option(
        long_option="plugins",
        short_help="Restrict discovery, checking or inventory to these plugins",
        argument=True,
        argument_descr="P",
        argument_conv=_convert_plugins_argument,
    )


def _convert_detect_plugins_argument(arg: str) -> set[str]:
    try:
        # kindly forgive empty strings
        # also maincheckify, as we may be dealing with old "--checks" input including dots.
        return {maincheckify(n) for n in arg.split(",") if n}
    except ValueError as exc:
        raise MKBailOut("Error in --detect-plugins argument: %s" % exc) from exc


_option_detect_plugins = Option(
    long_option="detect-plugins",
    deprecated_long_options={"checks"},
    short_help="Same as '--plugins', but implies a best efford guess for --detect-sections",
    argument=True,
    argument_descr="P",
    argument_conv=_convert_detect_plugins_argument,
)


@overload
def _extract_plugin_selection(
    options: "_CheckingOptions | _DiscoveryOptions",
    type_: type[CheckPluginName],
) -> tuple[SectionNameCollection, Container[CheckPluginName]]:
    pass


@overload
def _extract_plugin_selection(
    options: "_InventoryOptions",
    type_: type[InventoryPluginName],
) -> tuple[SectionNameCollection, Container[InventoryPluginName]]:
    pass


def _extract_plugin_selection(
    options: "_CheckingOptions | _DiscoveryOptions | _InventoryOptions",
    type_: type,
) -> tuple[SectionNameCollection, Container]:
    detect_plugins = options.get("detect-plugins")
    if detect_plugins is None:
        return (
            options.get("detect-sections", NO_SELECTION),
            options.get("plugins", EVERYTHING),
        )

    conflicting_options = {"detect-sections", "plugins"}
    if conflicting_options.intersection(options):
        raise MKBailOut(
            "Option '--detect-plugins' must not be combined with %s"
            % "/".join(f"--{o}" for o in conflicting_options)
        )

    if detect_plugins == {"@all"}:
        # this is the same as ommitting the option entirely.
        # (mo) ... which is weird, because specifiying *all* plugins would do
        # something different. Keeping this for compatibility with old --checks
        return NO_SELECTION, EVERYTHING

    if type_ is CheckPluginName:
        check_plugin_names = {CheckPluginName(p) for p in detect_plugins}
        return (
            frozenset(
                agent_based_register.get_relevant_raw_sections(
                    check_plugin_names=check_plugin_names,
                    inventory_plugin_names=(),
                )
            ),
            check_plugin_names,
        )

    if type_ is InventoryPluginName:
        inventory_plugin_names = {InventoryPluginName(p) for p in detect_plugins}
        return (
            frozenset(
                agent_based_register.get_relevant_raw_sections(
                    check_plugin_names=(),
                    inventory_plugin_names=inventory_plugin_names,
                )
            ),
            inventory_plugin_names,
        )

    raise NotImplementedError(f"unknown plugin name {type_}")


_DiscoveryOptions = TypedDict(
    "_DiscoveryOptions",
    {
        "cache": Literal[True],
        "no-cache": Literal[True],
        "no-tcp": Literal[True],
        "usewalk": Literal[True],
        "detect-sections": frozenset[SectionName],
        "plugins": frozenset[CheckPluginName],
        "detect-plugins": frozenset[str],
        "discover": int,
        "only-host-labels": bool,
    },
    total=False,
)


def _preprocess_hostnames(
    arg_host_names: frozenset[HostName],
    is_cluster: Callable[[HostName], bool],
    resolve_nodes: Callable[[HostName], Iterable[HostName]],
    config_cache: ConfigCache,
    only_host_labels: bool,
) -> set[HostName]:
    """Default to all hosts and expand cluster names to their nodes"""
    if not arg_host_names:
        console.verbose(
            "Discovering %shost labels on all hosts\n"
            % ("services and " if not only_host_labels else "")
        )
        hosts_config = config_cache.hosts_config
        return {
            hn
            for hn in hosts_config.hosts
            if config_cache.is_active(hn) and config_cache.is_online(hn)
        }

    node_names = {
        node_name
        for host_name in arg_host_names
        for node_name in (resolve_nodes(host_name) if is_cluster(host_name) else (host_name,))
    }

    console.verbose(
        "Discovering {}host labels on: {}\n".format(
            "services and " if not only_host_labels else "", ", ".join(sorted(node_names))
        )
    )

    return node_names


def mode_discover(options: _DiscoveryOptions, args: list[str]) -> None:
    config_cache = config.get_config_cache()
    hosts_config = config.make_hosts_config()
    hostnames = modes.parse_hostname_list(config_cache, hosts_config, args)
    if hostnames:
        # In case of discovery with host restriction, do not use the cache
        # file by default as -I and -II are used for debugging.
        file_cache_options = FileCacheOptions(disabled=True, use_outdated=False)
    else:
        # In case of discovery without host restriction, use the cache file
        # by default. Otherwise Checkmk would have to connect to ALL hosts.
        file_cache_options = FileCacheOptions(disabled=False, use_outdated=True)

    file_cache_options = _handle_fetcher_options(options, defaults=file_cache_options)
    try:
        snmp_backend_override = parse_snmp_backend(options.get("snmp-backend"))
    except ValueError as exc:
        raise MKBailOut("Unknown SNMP backend") from exc

    hostnames = modes.parse_hostname_list(config_cache, hosts_config, args)
    config_cache = config.get_config_cache()
    if not hostnames:
        # In case of discovery without host restriction, use the cache file
        # by default. Otherwise Checkmk would have to connect to ALL hosts.
        file_cache_options = file_cache_options._replace(use_outdated=True)
    else:
        config_cache.ruleset_matcher.ruleset_optimizer.set_all_processed_hosts(set(hostnames))

    on_error = OnError.RAISE if cmk.utils.debug.enabled() else OnError.WARN
    selected_sections, run_plugin_names = _extract_plugin_selection(options, CheckPluginName)
    config_cache = config.get_config_cache()
    parser = CMKParser(
        config_cache,
        selected_sections=selected_sections,
        keep_outdated=file_cache_options.keep_outdated,
        logger=logging.getLogger("cmk.base.discovery"),
    )
    fetcher = CMKFetcher(
        config_cache,
        file_cache_options=file_cache_options,
        force_snmp_cache_refresh=False,
        mode=FetchMode.DISCOVERY if selected_sections is NO_SELECTION else FetchMode.FORCE_SECTIONS,
        on_error=on_error,
        selected_sections=selected_sections,
        simulation_mode=config.simulation_mode,
        snmp_backend_override=snmp_backend_override,
    )
    for hostname in sorted(
        _preprocess_hostnames(
            frozenset(hostnames),
            is_cluster=lambda hn: hn in config_cache.hosts_config.clusters,
            resolve_nodes=lambda hn: config_cache.nodes_of(hn) or (),
            config_cache=config_cache,
            only_host_labels="only-host-labels" in options,
        )
    ):

        def section_error_handling(
            section_name: SectionName, raw_data: Sequence[object], host_name: HostName = hostname
        ) -> str:
            return create_section_crash_dump(
                operation="parsing",
                section_name=section_name,
                section_content=raw_data,
                host_name=host_name,
                rtc_package=None,
            )

        with plugin_contexts.current_host(hostname):
            commandline_discovery(
                hostname,
                ruleset_matcher=config_cache.ruleset_matcher,
                parser=parser,
                fetcher=fetcher,
                section_plugins=SectionPluginMapper(),
                section_error_handling=section_error_handling,
                host_label_plugins=HostLabelPluginMapper(
                    ruleset_matcher=config_cache.ruleset_matcher
                ),
                plugins=DiscoveryPluginMapper(ruleset_matcher=config_cache.ruleset_matcher),
                run_plugin_names=run_plugin_names,
                ignore_plugin=config_cache.check_plugin_ignored,
                arg_only_new=options["discover"] == 1,
                only_host_labels="only-host-labels" in options,
                on_error=on_error,
            )


modes.register(
    Mode(
        long_option="discover",
        short_option="I",
        handler_function=mode_discover,
        argument=True,
        argument_descr="[-I] HOST1 HOST2...",
        argument_optional=True,
        short_help="Find new services",
        long_help=[
            "Make Check_MK behave as monitoring plugins that checks if an "
            "inventory would find new or vanished services for the host. "
            "If configured to do so, this will queue those hosts for automatic "
            "autodiscovery",
            "Can be restricted to certain check types. Write '--checks df -I' if "
            "you just want to look for new filesystems. Use 'cmk -L' for a "
            "list of all check types.",
            "Can also be restricted to only discovering new host labels. "
            "Use: '--only-host-labels' or '-L' ",
            "-II does the same as -I but deletes all existing checks of the "
            "specified types and hosts.",
        ],
        sub_options=[
            *_FETCHER_OPTIONS,
            _SNMP_BACKEND_OPTION,
            Option(
                long_option="discover",
                short_option="I",
                short_help="Delete existing services before starting discovery",
                count=True,
            ),
            _option_sections,
            _get_plugins_option(CheckPluginName),
            _option_detect_plugins,
            Option(
                long_option="only-host-labels",
                short_option="L",
                short_help="Restrict discovery to host labels only",
            ),
        ],
    )
)

# .
#   .--check---------------------------------------------------------------.
#   |                           _               _                          |
#   |                       ___| |__   ___  ___| | __                      |
#   |                      / __| '_ \ / _ \/ __| |/ /                      |
#   |                     | (__| | | |  __/ (__|   <                       |
#   |                      \___|_| |_|\___|\___|_|\_\                      |
#   |                                                                      |
#   '----------------------------------------------------------------------'

_CheckingOptions = TypedDict(
    "_CheckingOptions",
    {
        "cache": Literal[True],
        "no-cache": Literal[True],
        "no-tcp": Literal[True],
        "usewalk": Literal[True],
        "no-submit": bool,
        "perfdata": bool,
        "detect-sections": frozenset[SectionName],
        "plugins": frozenset[CheckPluginName],
        "detect-plugins": frozenset[str],
    },
    total=False,
)


class GetSubmitter(Protocol):
    def __call__(
        self,
        check_submission: Literal["pipe", "file"],
        monitoring_core: Literal["nagios", "cmc"],
        host_name: HostName,
        *,
        dry_run: bool,
        perfdata_format: Literal["pnp", "standard"],
        show_perfdata: bool,
    ) -> Submitter:
        ...


def mode_check(
    get_submitter_: GetSubmitter,
    options: _CheckingOptions,
    args: list[str],
    *,
    active_check_handler: Callable[[HostName, str], object],
    keepalive: bool,
) -> ServiceState:
    file_cache_options = _handle_fetcher_options(options)
    try:
        snmp_backend_override = parse_snmp_backend(options.get("snmp-backend"))
    except ValueError as exc:
        raise MKBailOut("Unknown SNMP backend") from exc

    # handle adhoc-check
    hostname = HostName(args[0])
    ipaddress: HostAddress | None = None
    if len(args) == 2:
        ipaddress = HostAddress(args[1])

    config_cache = config.get_config_cache()
    hosts_config = config.make_hosts_config()
    config_cache.ruleset_matcher.ruleset_optimizer.set_all_processed_hosts({hostname})
    selected_sections, run_plugin_names = _extract_plugin_selection(options, CheckPluginName)
    fetcher = CMKFetcher(
        config_cache,
        file_cache_options=file_cache_options,
        force_snmp_cache_refresh=False,
        mode=FetchMode.CHECKING if selected_sections is NO_SELECTION else FetchMode.FORCE_SECTIONS,
        on_error=OnError.RAISE,
        selected_sections=selected_sections,
        simulation_mode=config.simulation_mode,
        snmp_backend_override=snmp_backend_override,
    )
    parser = CMKParser(
        config_cache,
        selected_sections=selected_sections,
        keep_outdated=file_cache_options.keep_outdated,
        logger=logging.getLogger("cmk.base.checking"),
    )
    summarizer = CMKSummarizer(
        config_cache,
        hostname,
        override_non_ok_state=None,
    )
    dry_run = options.get("no-submit", False)
    error_handler = CheckResultErrorHandler(
        config_cache.exit_code_spec(hostname),
        host_name=hostname,
        service_name="Check_MK",
        plugin_name="mk",
        is_cluster=hostname in hosts_config.clusters,
        snmp_backend=config_cache.get_snmp_backend(hostname),
        keepalive=keepalive,
    )
    check_result = ActiveCheckResult(3, "unknown error")
    fetched: Sequence[
        tuple[
            SourceInfo,
            Result[AgentRawData | SNMPRawData, Exception],
            Snapshot,
        ]
    ] = ()
    with error_handler, plugin_contexts.current_host(hostname), set_value_store_manager(
        ValueStoreManager(hostname), store_changes=not dry_run
    ) as value_store_manager:
        console.vverbose("Checkmk version %s\n", cmk_version.__version__)
        fetched = fetcher(hostname, ip_address=ipaddress)
        check_plugins = CheckPluginMapper(
            config_cache,
            value_store_manager,
            clusters=hosts_config.clusters,
            rtc_package=None,
        )
        with CPUTracker() as tracker:
            check_result = execute_checkmk_checks(
                hostname=hostname,
                fetched=((f[0], f[1]) for f in fetched),
                parser=parser,
                summarizer=summarizer,
                section_plugins=SectionPluginMapper(),
                section_error_handling=lambda section_name, raw_data: create_section_crash_dump(
                    operation="parsing",
                    section_name=section_name,
                    section_content=raw_data,
                    host_name=hostname,
                    rtc_package=None,
                ),
                check_plugins=check_plugins,
                inventory_plugins=InventoryPluginMapper(),
                inventory_parameters=config_cache.inventory_parameters,
                params=config_cache.hwsw_inventory_parameters(hostname),
                services=config_cache.configured_services(hostname),
                run_plugin_names=run_plugin_names,
                get_check_period=partial(config_cache.check_period_of_service, hostname),
                submitter=get_submitter_(
                    check_submission=config.check_submission,
                    monitoring_core=config.monitoring_core,
                    dry_run=dry_run,
                    host_name=hostname,
                    perfdata_format="pnp" if config.perfdata_format == "pnp" else "standard",
                    show_perfdata=options.get("perfdata", False),
                ),
                exit_spec=config_cache.exit_code_spec(hostname),
            )

        check_result = ActiveCheckResult.from_subresults(
            check_result,
            make_timing_results(
                tracker.duration,
                tuple((f[0], f[2]) for f in fetched),
                perfdata_with_times=config.check_mk_perfdata_with_times,
            ),
        )

    if error_handler.result is not None:
        check_result = error_handler.result

    active_check_handler(hostname, check_result.as_text())
    if keepalive:
        console.verbose(check_result.as_text())
    else:
        with suppress(IOError):
            sys.stdout.write(check_result.as_text() + "\n")
            sys.stdout.flush()
    return check_result.state


def register_mode_check(
    get_submitter_: GetSubmitter,
    *,
    active_check_handler: Callable[[HostName, str], object],
    keepalive: bool,
) -> None:
    modes.register(
        Mode(
            long_option="check",
            handler_function=partial(
                mode_check,
                get_submitter_,
                active_check_handler=active_check_handler,
                keepalive=keepalive,
            ),
            argument=True,
            argument_descr="HOST [IPADDRESS]",
            argument_optional=True,
            short_help="Check all services on the given HOST",
            long_help=[
                "Execute all checks on the given HOST. Optionally you can specify "
                "a second argument, the IPADDRESS. If you don't set this, the "
                "configured IP address of the HOST is used.",
                "By default the check results are sent to the core. If you provide "
                "the option '-n', the results will not be sent to the core and the "
                "counters of the check will not be stored.",
                "You can use '-v' to see the results of the checks. Add '-p' to "
                "also see the performance data of the checks. "
                "Can be restricted to certain check types. Write '--checks df -I' if "
                "you just want to look for new filesystems. Use 'check_mk -L' for a "
                "list of all check types. Use 'tcp' for all TCP based checks and "
                "'snmp' for all SNMP based checks.",
            ],
            sub_options=[
                *_FETCHER_OPTIONS,
                _SNMP_BACKEND_OPTION,
                Option(
                    long_option="no-submit",
                    short_option="n",
                    short_help="Do not submit results to core, do not save counters",
                ),
                Option(
                    long_option="perfdata",
                    short_option="p",
                    short_help="Also show performance data (use with -v)",
                ),
                _option_sections,
                _get_plugins_option(CheckPluginName),
                _option_detect_plugins,
            ],
        )
    )


if cmk_version.edition() is cmk_version.Edition.CRE:
    register_mode_check(get_submitter, active_check_handler=lambda *args: None, keepalive=False)

# .
#   .--inventory-----------------------------------------------------------.
#   |             _                      _                                 |
#   |            (_)_ ____   _____ _ __ | |_ ___  _ __ _   _               |
#   |            | | '_ \ \ / / _ \ '_ \| __/ _ \| '__| | | |              |
#   |            | | | | \ V /  __/ | | | || (_) | |  | |_| |              |
#   |            |_|_| |_|\_/ \___|_| |_|\__\___/|_|   \__, |              |
#   |                                                  |___/               |
#   '----------------------------------------------------------------------'

_InventoryOptions = TypedDict(
    "_InventoryOptions",
    {
        "cache": Literal[True],
        "no-cache": Literal[True],
        "no-tcp": Literal[True],
        "usewalk": Literal[True],
        "force": bool,
        "detect-sections": frozenset[SectionName],
        "plugins": frozenset[InventoryPluginName],
        "detect-plugins": frozenset[str],
    },
    total=False,
)


def mode_inventory(options: _InventoryOptions, args: list[str]) -> None:
    file_cache_options = _handle_fetcher_options(options)
    try:
        snmp_backend_override = parse_snmp_backend(options.get("snmp-backend"))
    except ValueError as exc:
        raise MKBailOut("Unknown SNMP backend") from exc

    config_cache = config.get_config_cache()
    hosts_config = config.make_hosts_config()

    if args:
        hostnames = modes.parse_hostname_list(config_cache, hosts_config, args, with_clusters=True)
        config_cache.ruleset_matcher.ruleset_optimizer.set_all_processed_hosts(set(hostnames))
        console.verbose("Doing HW/SW inventory on: %s\n" % ", ".join(hostnames))
    else:
        # No hosts specified: do all hosts and force caching
        hostnames = sorted(
            {
                hn
                for hn in itertools.chain(hosts_config.hosts, hosts_config.clusters)
                if config_cache.is_active(hn) and config_cache.is_online(hn)
            }
        )
        console.verbose("Doing HW/SW inventory on all hosts\n")

    if "force" in options:
        file_cache_options = file_cache_options._replace(keep_outdated=True)

    selected_sections, run_plugin_names = _extract_plugin_selection(options, InventoryPluginName)
    fetcher = CMKFetcher(
        config_cache,
        file_cache_options=file_cache_options,
        force_snmp_cache_refresh=False,
        mode=FetchMode.INVENTORY if selected_sections is NO_SELECTION else FetchMode.FORCE_SECTIONS,
        on_error=OnError.RAISE,
        selected_sections=selected_sections,
        simulation_mode=config.simulation_mode,
        snmp_backend_override=snmp_backend_override,
    )
    parser = CMKParser(
        config_cache,
        selected_sections=selected_sections,
        keep_outdated=file_cache_options.keep_outdated,
        logger=logging.getLogger("cmk.base.inventory"),
    )

    store.makedirs(cmk.utils.paths.inventory_output_dir)
    store.makedirs(cmk.utils.paths.inventory_archive_dir)

    section_plugins = SectionPluginMapper()
    inventory_plugins = InventoryPluginMapper()

    for hostname in hostnames:

        def section_error_handling(
            section_name: SectionName,
            raw_data: Sequence[object],
            host_name: HostName = hostname,
        ) -> str:
            return create_section_crash_dump(
                operation="parsing",
                section_name=section_name,
                section_content=raw_data,
                host_name=host_name,
                rtc_package=None,
            )

        parameters = config_cache.hwsw_inventory_parameters(hostname)
        raw_intervals_from_config = config_cache.inv_retention_intervals(hostname)
        summarizer = CMKSummarizer(
            config_cache,
            hostname,
            override_non_ok_state=parameters.fail_status,
        )

        section.section_begin(hostname)
        section.section_step("Inventorizing")
        try:
            previous_tree = load_tree(Path(cmk.utils.paths.inventory_output_dir, hostname))
            if hostname in hosts_config.clusters:
                check_result = inventory.inventorize_cluster(
                    config_cache.nodes_of(hostname) or (),
                    parameters=parameters,
                    previous_tree=previous_tree,
                ).check_result
            else:
                check_result = inventory.inventorize_host(
                    hostname,
                    fetcher=fetcher,
                    parser=parser,
                    summarizer=summarizer,
                    inventory_parameters=config_cache.inventory_parameters,
                    section_plugins=section_plugins,
                    section_error_handling=section_error_handling,
                    inventory_plugins=inventory_plugins,
                    run_plugin_names=run_plugin_names,
                    parameters=parameters,
                    raw_intervals_from_config=raw_intervals_from_config,
                    previous_tree=previous_tree,
                ).check_result
            if check_result.state:
                section.section_error(check_result.summary)
            else:
                section.section_success(check_result.summary)

        except Exception as e:
            if cmk.utils.debug.enabled():
                raise
            section.section_error("%s" % e)
        finally:
            cmk.utils.cleanup.cleanup_globals()


modes.register(
    Mode(
        long_option="inventory",
        short_option="i",
        handler_function=mode_inventory,
        argument=True,
        argument_descr="HOST1 HOST2...",
        argument_optional=True,
        short_help="Do a HW/SW-Inventory on some or all hosts",
        long_help=[
            "Does a HW/SW-Inventory for all, one or several "
            "hosts. If you add the option -f, --force then persisted sections "
            "will be used even if they are outdated."
        ],
        sub_options=[
            *_FETCHER_OPTIONS,
            _SNMP_BACKEND_OPTION,
            Option(
                long_option="force",
                short_option="f",
                short_help="Use cached agent data even if it's outdated.",
            ),
            _option_sections,
            _get_plugins_option(InventoryPluginName),
            _option_detect_plugins,
        ],
    )
)

# .
#   .--inventory-as-check--------------------------------------------------.
#   | _                      _                              _     _        |
#   |(_)_ ____   _____ _ __ | |_ ___  _ __ _   _        ___| |__ | | __    |
#   || | '_ \ \ / / _ \ '_ \| __/ _ \| '__| | | |_____ / __| '_ \| |/ /    |
#   || | | | \ V /  __/ | | | || (_) | |  | |_| |_____| (__| | | |   < _   |
#   ||_|_| |_|\_/ \___|_| |_|\__\___/|_|   \__, |      \___|_| |_|_|\_(_)  |
#   |                                      |___/                           |
#   '----------------------------------------------------------------------'


def _execute_active_check_inventory(
    host_name: HostName,
    *,
    config_cache: ConfigCache,
    hosts_config: Hosts,
    fetcher: FetcherFunction,
    parser: ParserFunction,
    summarizer: SummarizerFunction,
    section_plugins: SectionMap[SectionPlugin],
    inventory_plugins: Mapping[InventoryPluginName, InventoryPlugin],
    inventory_parameters: Callable[[HostName, InventoryPlugin], Mapping[str, object]],
    parameters: HWSWInventoryParameters,
    raw_intervals_from_config: Sequence[RawIntervalFromConfig],
) -> ActiveCheckResult:
    tree_or_archive_store = TreeOrArchiveStore(
        cmk.utils.paths.inventory_output_dir,
        cmk.utils.paths.inventory_archive_dir,
    )
    previous_tree = tree_or_archive_store.load_previous(host_name=host_name)

    if host_name in hosts_config.clusters:
        result = inventory.inventorize_cluster(
            config_cache.nodes_of(host_name) or (),
            parameters=parameters,
            previous_tree=previous_tree,
        )
    else:
        result = inventory.inventorize_host(
            host_name,
            fetcher=fetcher,
            parser=parser,
            summarizer=summarizer,
            inventory_parameters=inventory_parameters,
            section_plugins=section_plugins,
            section_error_handling=lambda section_name, raw_data: create_section_crash_dump(
                operation="parsing",
                section_name=section_name,
                section_content=raw_data,
                host_name=host_name,
                rtc_package=None,
            ),
            inventory_plugins=inventory_plugins,
            run_plugin_names=EVERYTHING,
            parameters=parameters,
            raw_intervals_from_config=raw_intervals_from_config,
            previous_tree=previous_tree,
        )

    if result.no_data_or_files:
        AutoQueue(cmk.utils.paths.autoinventory_dir).add(host_name)

    if not (result.processing_failed or result.no_data_or_files):
        save_tree_actions = _get_save_tree_actions(
            previous_tree=previous_tree,
            inventory_tree=result.inventory_tree,
            update_result=result.update_result,
        )
        # The order of archive or save is important:
        if save_tree_actions.do_archive:
            tree_or_archive_store.archive(host_name=host_name)
        if save_tree_actions.do_save:
            tree_or_archive_store.save(host_name=host_name, tree=result.inventory_tree)

    return result.check_result


class _SaveTreeActions(NamedTuple):
    do_archive: bool
    do_save: bool


def _get_save_tree_actions(
    *,
    previous_tree: ImmutableTree,
    inventory_tree: MutableTree,
    update_result: UpdateResult,
) -> _SaveTreeActions:
    if not inventory_tree:
        # Archive current inventory tree file if it exists. Important for host inventory icon
        console.verbose("No inventory tree.\n")
        return _SaveTreeActions(do_archive=True, do_save=False)

    if not previous_tree:
        console.verbose("New inventory tree.\n")
        return _SaveTreeActions(do_archive=False, do_save=True)

    if has_changed := previous_tree != inventory_tree:
        console.verbose("Inventory tree has changed. Add history entry.\n")

    if update_result.save_tree:
        console.verbose(str(update_result))

    return _SaveTreeActions(
        do_archive=has_changed,
        do_save=(has_changed or update_result.save_tree),
    )


def mode_inventory_as_check(
    options: Mapping[str, object],
    hostname: HostName,
    *,
    active_check_handler: Callable[[HostName, str], object],
    keepalive: bool,
) -> ServiceState:
    config_cache = config.get_config_cache()
    config_cache.ruleset_matcher.ruleset_optimizer.set_all_processed_hosts({hostname})
    hosts_config = config.make_hosts_config()
    file_cache_options = _handle_fetcher_options(options)
    try:
        snmp_backend_override = parse_snmp_backend(options.get("snmp-backend"))
    except ValueError as exc:
        raise MKBailOut("Unknown SNMP backend") from exc

    parameters = HWSWInventoryParameters.from_raw(options)

    fetcher = CMKFetcher(
        config_cache,
        file_cache_options=file_cache_options,
        force_snmp_cache_refresh=False,
        mode=FetchMode.INVENTORY,
        on_error=OnError.RAISE,
        selected_sections=NO_SELECTION,
        simulation_mode=config.simulation_mode,
        snmp_backend_override=snmp_backend_override,
    )
    parser = CMKParser(
        config_cache,
        selected_sections=NO_SELECTION,
        keep_outdated=file_cache_options.keep_outdated,
        logger=logging.getLogger("cmk.base.inventory"),
    )
    summarizer = CMKSummarizer(
        config_cache,
        hostname,
        override_non_ok_state=parameters.fail_status,
    )
    error_handler = CheckResultErrorHandler(
        exit_spec=config_cache.exit_code_spec(hostname),
        host_name=hostname,
        service_name="Check_MK HW/SW Inventory",
        plugin_name="check_mk_active-cmk_inv",
        is_cluster=hostname in hosts_config.clusters,
        snmp_backend=config_cache.get_snmp_backend(hostname),
        keepalive=keepalive,
    )
    check_result = ActiveCheckResult(3, "unknown error")
    with error_handler:
        check_result = _execute_active_check_inventory(
            hostname,
            config_cache=config_cache,
            hosts_config=hosts_config,
            fetcher=fetcher,
            parser=parser,
            summarizer=summarizer,
            section_plugins=SectionPluginMapper(),
            inventory_plugins=InventoryPluginMapper(),
            inventory_parameters=config_cache.inventory_parameters,
            parameters=parameters,
            raw_intervals_from_config=config_cache.inv_retention_intervals(hostname),
        )

    if error_handler.result is not None:
        check_result = error_handler.result

    active_check_handler(hostname, check_result.as_text())
    if keepalive:
        console.verbose(check_result.as_text())
    else:
        with suppress(IOError):
            sys.stdout.write(check_result.as_text() + "\n")
            sys.stdout.flush()
    return check_result.state


def register_mode_inventory_as_check(
    *,
    active_check_handler: Callable[[HostName, str], object],
    keepalive: bool,
) -> None:
    modes.register(
        Mode(
            long_option="inventory-as-check",
            handler_function=partial(
                mode_inventory_as_check,
                active_check_handler=active_check_handler,
                keepalive=keepalive,
            ),
            argument=True,
            argument_descr="HOST",
            short_help="Do HW/SW-Inventory, behave like check plugin",
            sub_options=[
                *_FETCHER_OPTIONS,
                _SNMP_BACKEND_OPTION,
                Option(
                    long_option="hw-changes",
                    argument=True,
                    argument_descr="S",
                    argument_conv=int,
                    short_help="Use monitoring state S for HW changes",
                ),
                Option(
                    long_option="sw-changes",
                    argument=True,
                    argument_descr="S",
                    argument_conv=int,
                    short_help="Use monitoring state S for SW changes",
                ),
                Option(
                    long_option="sw-missing",
                    argument=True,
                    argument_descr="S",
                    argument_conv=int,
                    short_help="Use monitoring state S for missing SW packages info",
                ),
                Option(
                    long_option="inv-fail-status",
                    argument=True,
                    argument_descr="S",
                    argument_conv=int,
                    short_help="Use monitoring state S in case of error",
                ),
            ],
        )
    )


if cmk_version.edition() is cmk_version.Edition.CRE:
    register_mode_inventory_as_check(
        active_check_handler=lambda *args: None,
        keepalive=False,
    )

# .
#   .--inventorize-marked-hosts--------------------------------------------.
#   |           _                      _             _                     |
#   |          (_)_ ____   _____ _ __ | |_ ___  _ __(_)_______             |
#   |          | | '_ \ \ / / _ \ '_ \| __/ _ \| '__| |_  / _ \            |
#   |          | | | | \ V /  __/ | | | || (_) | |  | |/ /  __/            |
#   |          |_|_| |_|\_/ \___|_| |_|\__\___/|_|  |_/___\___|            |
#   |                                                                      |
#   |                         _            _   _               _           |
#   |    _ __ ___   __ _ _ __| | _____  __| | | |__   ___  ___| |_ ___     |
#   |   | '_ ` _ \ / _` | '__| |/ / _ \/ _` | | '_ \ / _ \/ __| __/ __|    |
#   |   | | | | | | (_| | |  |   <  __/ (_| | | | | | (_) \__ \ |_\__ \    |
#   |   |_| |_| |_|\__,_|_|  |_|\_\___|\__,_| |_| |_|\___/|___/\__|___/    |
#   |                                                                      |
#   '----------------------------------------------------------------------'


def mode_inventorize_marked_hosts(options: Mapping[str, object]) -> None:
    file_cache_options = _handle_fetcher_options(options)
    try:
        snmp_backend_override = parse_snmp_backend(options.get("snmp-backend"))
    except ValueError as exc:
        raise MKBailOut("Unknown SNMP backend") from exc

    if not (queue := AutoQueue(cmk.utils.paths.autoinventory_dir)):
        console.verbose("Autoinventory: No hosts marked by inventory check\n")
        return

    config.load()
    config_cache = config.get_config_cache()
    parser = CMKParser(
        config_cache,
        selected_sections=NO_SELECTION,
        keep_outdated=file_cache_options.keep_outdated,
        logger=logging.getLogger("cmk.base.inventory"),
    )
    fetcher = CMKFetcher(
        config_cache,
        file_cache_options=file_cache_options,
        force_snmp_cache_refresh=False,
        mode=FetchMode.INVENTORY,
        on_error=OnError.RAISE,
        selected_sections=NO_SELECTION,
        simulation_mode=config.simulation_mode,
        snmp_backend_override=snmp_backend_override,
    )

    def summarizer(host_name: HostName) -> CMKSummarizer:
        return CMKSummarizer(
            config_cache,
            host_name,
            override_non_ok_state=config_cache.hwsw_inventory_parameters(host_name).fail_status,
        )

    hosts_config = config_cache.hosts_config
    all_hosts = frozenset(
        itertools.chain(hosts_config.hosts, hosts_config.clusters, hosts_config.shadow_hosts)
    )
    for host_name in queue:
        if host_name not in all_hosts:
            console.verbose(f"  Removing mark '{host_name}' (host not configured\n")
            (queue.path / str(host_name)).unlink(missing_ok=True)

    if queue.oldest() is None:
        console.verbose("Autoinventory: No hosts marked by inventory check\n")
        return

    console.verbose("Autoinventory: Inventorize all hosts marked by inventory check:\n")
    try:
        response = livestatus.LocalConnection().query("GET hosts\nColumns: name state")
        process_hosts: Container[HostName] = {
            HostName(name) for name, state in response if state == 0
        }
    except (livestatus.MKLivestatusNotFoundError, livestatus.MKLivestatusSocketError):
        process_hosts = EVERYTHING

    section_plugins = SectionPluginMapper()
    inventory_plugins = InventoryPluginMapper()

    start = time.monotonic()
    limit = 120
    message = f"  Timeout of {limit} seconds reached. Let's do the remaining hosts next time."

    try:
        with Timeout(limit + 10, message=message):
            for host_name in queue:
                if time.monotonic() > start + limit:
                    raise TimeoutError(message)

                if host_name not in process_hosts:
                    continue

                _execute_active_check_inventory(
                    host_name,
                    config_cache=config_cache,
                    hosts_config=hosts_config,
                    parser=parser,
                    fetcher=fetcher,
                    summarizer=summarizer(host_name),
                    section_plugins=section_plugins,
                    inventory_plugins=inventory_plugins,
                    inventory_parameters=config_cache.inventory_parameters,
                    parameters=config_cache.hwsw_inventory_parameters(host_name),
                    raw_intervals_from_config=config_cache.inv_retention_intervals(host_name),
                )
    except (MKTimeout, TimeoutError) as exc:
        console.verbose(str(exc))


modes.register(
    Mode(
        long_option="inventorize-marked-hosts",
        handler_function=mode_inventorize_marked_hosts,
        short_help="Run inventory for hosts which previously had no tree data",
        long_help=[
            "Run actual service HW/SW Inventory on all hosts that had no tree data",
            "in the previous run",
        ],
        sub_options=[*_FETCHER_OPTIONS, _SNMP_BACKEND_OPTION],
        needs_config=False,
    )
)

# .
#   .--version-------------------------------------------------------------.
#   |                                     _                                |
#   |                 __   _____ _ __ ___(_) ___  _ __                     |
#   |                 \ \ / / _ \ '__/ __| |/ _ \| '_ \                    |
#   |                  \ V /  __/ |  \__ \ | (_) | | | |                   |
#   |                   \_/ \___|_|  |___/_|\___/|_| |_|                   |
#   |                                                                      |
#   '----------------------------------------------------------------------'


def mode_version() -> None:
    out.output(
        """This is Check_MK version %s %s
Copyright (C) 2009 Mathias Kettner

    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation; either version 2 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.

    You should have received a copy of the GNU General Public License
    along with this program; see the file COPYING.  If not, write to
    the Free Software Foundation, Inc., 59 Temple Place - Suite 330,
    Boston, MA 02111-1307, USA.

""",
        cmk_version.__version__,
        cmk_version.edition().short.upper(),
    )


modes.register(
    Mode(
        long_option="version",
        short_option="V",
        handler_function=mode_version,
        short_help="Print the version of Check_MK",
        needs_config=False,
        needs_checks=False,
    )
)

# .
#   .--help----------------------------------------------------------------.
#   |                         _          _                                 |
#   |                        | |__   ___| |_ __                            |
#   |                        | '_ \ / _ \ | '_ \                           |
#   |                        | | | |  __/ | |_) |                          |
#   |                        |_| |_|\___|_| .__/                           |
#   |                                     |_|                              |
#   '----------------------------------------------------------------------'


def mode_help() -> None:
    out.output(
        """WAYS TO CALL:
%s

OPTIONS:
%s

NOTES:
%s

"""
        % (
            modes.short_help(),
            modes.general_option_help(),
            modes.long_help(),
        )
    )


modes.register(
    Mode(
        long_option="help",
        short_option="h",
        handler_function=mode_help,
        short_help="Print this help",
        needs_config=False,
        needs_checks=False,
    )
)

# .
#   .--diagnostics---------------------------------------------------------.
#   |             _ _                             _   _                    |
#   |          __| (_) __ _  __ _ _ __   ___  ___| |_(_) ___ ___           |
#   |         / _` | |/ _` |/ _` | '_ \ / _ \/ __| __| |/ __/ __|          |
#   |        | (_| | | (_| | (_| | | | | (_) \__ \ |_| | (__\__ \          |
#   |         \__,_|_|\__,_|\__, |_| |_|\___/|___/\__|_|\___|___/          |
#   |                       |___/                                          |
#   '----------------------------------------------------------------------'


def mode_create_diagnostics_dump(options: DiagnosticsModesParameters) -> None:
    cmk.base.diagnostics.create_diagnostics_dump(
        cmk.utils.diagnostics.deserialize_modes_parameters(options)
    )


def _get_diagnostics_dump_sub_options() -> list[Option]:
    sub_options = [
        Option(
            long_option=OPT_LOCAL_FILES,
            short_help=(
                "Pack a list of installed, unpacked, optional files below $OMD_ROOT/local. "
                "This also includes information about installed MKPs."
            ),
        ),
        Option(
            long_option=OPT_OMD_CONFIG,
            short_help="Pack content of 'etc/omd/site.conf'",
        ),
        Option(
            long_option=OPT_CHECKMK_OVERVIEW,
            short_help="Pack HW/SW inventory node 'Software > Applications > Checkmk'",
        ),
        Option(
            long_option=OPT_CHECKMK_CONFIG_FILES,
            short_help="Pack configuration files ('*.mk' or '*.conf') from etc/checkmk",
            argument=True,
            argument_descr="FILE,FILE...",
        ),
        Option(
            long_option=OPT_CHECKMK_LOG_FILES,
            short_help="Pack log files ('*.log' or '*.state') from var/log",
            argument=True,
            argument_descr="FILE,FILE...",
        ),
    ]

    if cmk_version.edition() is not cmk_version.Edition.CRE:
        sub_options.append(
            Option(
                long_option=OPT_PERFORMANCE_GRAPHS,
                short_help=(
                    "Pack performance graphs like CPU load and utilization of Checkmk Server"
                ),
            )
        )
    return sub_options


modes.register(
    Mode(
        long_option="create-diagnostics-dump",
        handler_function=mode_create_diagnostics_dump,
        short_help="Create diagnostics dump",
        long_help=[
            "Create a dump containing information for diagnostic analysis "
            "in the folder var/check_mk/diagnostics."
        ],
        needs_config=False,
        needs_checks=False,
        sub_options=_get_diagnostics_dump_sub_options(),
    )
)
