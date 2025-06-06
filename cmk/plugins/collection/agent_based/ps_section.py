#!/usr/bin/env python3
# Copyright (C) 2019 Checkmk GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.
import time
from collections.abc import Iterable, Mapping, Sequence

from cmk.agent_based.v2 import AgentSection, RuleSetType, StringTable
from cmk.plugins.lib import ps

# First generation of agents output only the process command line:
# /usr/sbin/xinetd -pidfile /var/run/xinetd.pid -stayalive -inetd_compat -inetd_ipv6

# Second generation of agents output the user in brackets in the first columns:
# (root) /usr/sbin/xinetd -pidfile /var/run/xinetd.pid -stayalive -inetd_compat -inetd_ipv6

# Third generation (from 1.1.5) output also virtual memory, resident memory and %CPU:
# (class,122376,88128,0.0) /usr/jre1.6.0_13/bin/java -Dn=Cart_16TH13 -Dmcs.node=zbgh1ca -Dmcs.mdt.redundan

# Forth generation (>=1.2.5), additional columns in bracket:
# (user, virtual_size, resident_size, %cpu, processID, pagefile_usage, usermodetime, kernelmodetime, openHandles, threadCount) name
# (\\KLAPPRECHNER\ab,29284,2948,0,3124,904,400576,901296,35,1)    NOTEPAD.EXE

# Sixth generation (>=1.2.7) adds an optional etime, joined by "/" with the CPU time

# The plug-in "psperf.bat" is deprecated. As of version 1.2.5 all of this information
# is reported by the windows agent itself. However, we still support sections from psperf.bat
# if the agent version is lower than 1.2.5.
# Windows agent now ships a plug-in "psperf.bat" that adds a section from wmic
# to the output:
# <<<ps:sep(44)>>>
# [wmic process]
# ^M
# Node,KernelModeTime,Name,PageFileUsage,ThreadCount,UserModeTime,VirtualSize,WorkingSetSize^M
# WINDOWSXP,43478281250,System Idle Process,0,2,0,0,28672^M
# WINDOWSXP,155781250,System,0,59,0,1957888,253952^M
# WINDOWSXP,468750,smss.exe,176128,3,156250,3928064,442368^M
# WINDOWSXP,56406250,csrss.exe,1863680,12,11406250,25780224,3956736^M
# WINDOWSXP,18593750,winlogon.exe,6832128,19,4843750,59314176,2686976^M
# WINDOWSXP,167500000,services.exe,1765376,16,13750000,22601728,4444160^M
# WINDOWSXP,16875000,lsass.exe,3964928,21,3906250,43462656,6647808^M
# WINDOWSXP,8750000,VBoxService.exe,1056768,8,468750,26652672,3342336^M

Section = tuple[int, list]  # don't ask what kind of list.

HOST_LABEL_DEFAULT_PARAMETERS = {
    "descr": "Example service - unused",
    "default_params": {
        "cpu_rescale_max": True,
    },
}


# This function is only concerned with deprecated output from psperf.bat,
# in case of all other output it just returns info unmodified. But if it is
# a windows output it will extract the number of cpu cores
def _merge_wmic_info(info: StringTable) -> tuple[int, StringTable]:
    # Agent output version cmk>1.2.5
    # Assumes line = [CLUSTER, PS_INFO, COMMAND]
    has_wmic = False
    for line in info:
        if len(line) > 1 and line[1].lower() == "system idle process":
            cpu_cores = int(line[0][1:-1].split(",")[9])
            return cpu_cores, info
        if "wmic process" in line[-1]:
            has_wmic = True
            break
    # Data from other systems than windows
    if not has_wmic:
        return 1, info

    # Data from windows with wmic info, cmk<1.2.5
    return _extract_wmic_info(info)


def _extract_wmic_info(info: StringTable) -> tuple[int, StringTable]:
    ps_result = []
    lines = iter(info)
    wmic_info: dict[str, list] = {}
    is_wmic = False

    while True:
        try:
            line = next(lines)
            if line[-1] == "[wmic process]":
                is_wmic = True
                wmic_headers = next(lines)
                continue
            if line[-1] == "[wmic process end]":
                is_wmic = False
                continue
        except StopIteration:
            break  # Finished with all lines

        if is_wmic:
            row = dict(zip(wmic_headers, line))
            # Row might be damaged. I've seen this agent output:
            # Node - TILE-BUILDER02
            # ERROR:
            # Description = Quota violation
            #
            # Node,
            if "Name" in row and "ProcessId" in row:
                wmic_info.setdefault(row["Name"], []).append(row)
        else:
            ps_result.append(line)  # plain list of process names

    return _merge_wmic(ps_result, wmic_info, wmic_headers)


def _merge_wmic(
    ps_result: StringTable, wmic_info: Mapping[str, list], wmic_headers: Sequence[str]
) -> tuple[int, StringTable]:
    info = []
    seen_pids = set()  # Remove duplicate entries
    cpu_cores = 1
    for line in ps_result:
        psinfos = wmic_info.get(line[0], [])
        if psinfos:
            psinfo = psinfos.pop()  # each info is used only once!
            # Get number of CPU cores from system idle process
            if "ThreadCount" in wmic_headers and psinfo["Name"].lower() == "system idle process":
                cpu_cores = int(psinfo["ThreadCount"])
            pid = int(psinfo["ProcessId"])
            if pid not in seen_pids:
                seen_pids.add(pid)
                virt = int(psinfo["VirtualSize"]) >> 10  # Bytes -> KB
                resi = int(psinfo["WorkingSetSize"]) >> 10  # Bytes -> KB
                pagefile = int(psinfo["PageFileUsage"]) >> 10  # Bytes -> KB
                userc = int(psinfo["UserModeTime"])  # do not resolve counter here!
                kernelc = int(psinfo["KernelModeTime"])  # do not resolve counter here!
                handlec = int(psinfo.get("HandleCount", 0))  # Only in newer psperf.bat versions
                threadc = int(psinfo["ThreadCount"])  # do not resolve counter here!
                line[0:0] = [
                    "(unknown,%d,%d,0,%d,%d,%d,%d,%d,%d,)"
                    % (virt, resi, pid, pagefile, userc, kernelc, handlec, threadc)
                ]
        info.append(line)

    return cpu_cores, info


# This mainly formats the line[1] element which contains the process info (user,...)
def parse_process_entries(pre_parsed: StringTable) -> list[tuple[ps.PsInfo, list[str]]]:
    parsed = []
    # line[0] = process_info OR (if no process info available) = process name
    for line in pre_parsed:
        try:
            process_info = ps.PsInfo.from_raw(line[0])
        except ValueError:
            process_info = ps.PsInfo()
            cmd_line = line
        else:
            cmd_line = line[1:]

        # Filter out any lines where no process command line is available, e.g.
        # [None, u'(<defunct>,,,)']
        # [None, u'(<defunct>,,,)', u'']
        if cmd_line and cmd_line[0]:
            parsed.append((process_info, cmd_line))

    return parsed


def _consolidate_lines(string_table: StringTable) -> StringTable:
    """
    >>> _consolidate_lines([['(mywinproc)', 'somescript.exe'], [' -Port 39999'], ['-UpdatePeriodMs 1000'], ['(mynextwinproc)', '']])
    [['(mywinproc)', 'somescript.exe  -Port 39999 -UpdatePeriodMs 1000'], ['(mynextwinproc)', '']]

    """

    iter_string_table = iter(string_table)
    consolidated_lines: list[list[str]] = []

    for line in iter_string_table:
        if line[0].replace("'", "").replace('"', "").strip().startswith("-"):
            # For some reason, some of the Windows process descriptions can contain a newline.
            # This leads to an extra line in string_table which is in fact a continuation
            # of the previus line. This seems to be the case when executables are called with
            # parameters.
            consolidated_lines[-1][1] = f"{consolidated_lines[-1][1]} {' '.join(line)}"
            continue
        consolidated_lines.append(line)

    return consolidated_lines


def parse_ps(string_table: StringTable) -> ps.Section:
    now = int(time.time())
    return _parse_ps(now, string_table)


def _parse_ps(now: int, string_table: StringTable) -> ps.Section:
    ps_time, ps_string_table = _separate_sub_string_table(now, string_table)
    # Produces a list of Tuples where each sub list is built as follows:
    # [
    #     [(u'root', u'35156', u'4372', u'00:00:05/2-14:14:49', u'1'), u'/sbin/init'],
    # ]
    # First element: The process info tuple (see ps.include: check_ps_common() for details on the elements)
    # second element:  The process command line
    cpu_cores, info = _merge_wmic_info(_consolidate_lines(ps_string_table))
    parsed = parse_process_entries(info)
    return cpu_cores, parsed, ps_time


agent_section_ps = AgentSection[ps.Section](
    name="ps",
    parse_function=parse_ps,
    host_label_function=ps.host_labels_ps,
    host_label_ruleset_name="inventory_processes_rules",
    host_label_default_parameters=HOST_LABEL_DEFAULT_PARAMETERS,
    host_label_ruleset_type=RuleSetType.ALL,
)


def _handle_deleted_cgroup(attrs: Iterable[str], line: Sequence[str]) -> Sequence[str]:
    """
    >>> _handle_deleted_cgroup(
    ...     ('cgroup', 'user', 'vsz', 'rss', 'time', 'elapsed', 'pid'),
    ...     ['some_cgroup', '(deleted)', 'root', '0', '0', '00:00:00', '01:54', '654939', '[node]', '<defunct>']
    ... )
    ['some_cgroup (deleted)', 'root', '0', '0', '00:00:00', '01:54', '654939', '[node]', '<defunct>']
    >>> _handle_deleted_cgroup(
    ...     ('cgroup', 'user', 'vsz', 'rss', 'time', 'elapsed', 'pid'),
    ...     ['some_cgroup', 'root', '0', '0', '00:00:00', '01:54', '654939', '[node]', '<defunct>']
    ... )
    ['some_cgroup', 'root', '0', '0', '00:00:00', '01:54', '654939', '[node]', '<defunct>']
    """
    for idx, attr in enumerate(attrs):
        if attr == "cgroup" and len(line) > (next_idx := idx + 1) and line[next_idx] == "(deleted)":
            return [
                *line[:idx],
                line[idx] + " (deleted)",
                *line[next_idx + 1 :],
            ]
    return line


def parse_ps_lnx(string_table: StringTable) -> ps.Section | None:
    now = int(time.time())
    return _parse_ps_lnx(now, string_table)


def _separate_sub_string_table(now: int, string_table: StringTable) -> tuple[int, StringTable]:
    if string_table and string_table[0][0].startswith("[time]") and string_table[2][0].startswith("[processes]"):
        return int(string_table[1][0]), string_table[3:]
    return now, string_table


def _parse_ps_lnx(now: int, string_table: StringTable) -> ps.Section | None:
    ps_time, ps_string_table = _separate_sub_string_table(now, string_table)
    data = {}
    # info[0]: $Node [header] user ... pid command
    # we rely on the command being the last one!

    attrs = tuple(word.lower() for word in ps_string_table[0][1:-1])
    # busybox' ps seems to not provide the columns we need so we abort
    if not all(att in attrs for att in ("user", "vsz", "rss", "time", "elapsed", "pid")):
        return None

    cmd_idx = len(attrs)

    for line in (_handle_deleted_cgroup(attrs, l) for l in ps_string_table[1:]):
        if line[0].startswith("[") or line[0].isdigit():
            # This can happen, if the agent output contains multiple sections. It is safe to skip
            # those lines:
            # - Any subsection header (e.g. [time])
            # - Only numeric values (e.g. the timestamp after [time]
            # Hence, this loop only parses lines generated by the `ps` command.
            continue

        # read all but 'command' into dict
        ps_raw = dict(zip(attrs, line))

        # If the string_table contains data from concatenated sections, we allow more recent data
        # to take precedence. This assumes that section data is generated and concatenated in
        # temporal order.
        data[ps_raw["pid"]] = (
            ps.PsInfo(
                user=ps_raw["user"],
                virtual=int(ps_raw["vsz"]),
                physical=int(ps_raw["rss"]),
                cputime="{}/{}".format(ps_raw["time"], ps_raw["elapsed"]),
                process_id=ps_raw["pid"],
                cgroup=ps_raw.get("cgroup"),
            ),
            line[cmd_idx:],
        )

    # cpu_cores for compatibility!
    return 1, list(data.values()), ps_time


agent_section_ps_lnx = AgentSection(
    name="ps_lnx",
    parsed_section_name="ps",
    parse_function=parse_ps_lnx,
    host_label_function=ps.host_labels_ps,
    host_label_ruleset_name="inventory_processes_rules",
    host_label_default_parameters=HOST_LABEL_DEFAULT_PARAMETERS,
    host_label_ruleset_type=RuleSetType.ALL,
    supersedes=["ps"],
)
