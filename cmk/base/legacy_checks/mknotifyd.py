#!/usr/bin/env python3
# Copyright (C) 2019 Checkmk GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.

# Example output from agent:
# <<<mknotifyd:sep(0)>>>
# 1571151364
# [heute]
# Version:         2019.10.14
# Updated:         1571151346 (2019-10-15 16:55:46)
# Started:         1571143926 (2019-10-15 14:52:06, 7420 sec ago)
# Configuration:   1571143926 (2019-10-15 14:52:06, 7420 sec ago)
# Listening FD:    5
#
# Spool:           New
# Count:           0
# Oldest:
# Youngest:
#
# Spool:           Deferred
# Count:           0
# Oldest:
# Youngest:
#
# Spool:           Corrupted
# Count:           0
# Oldest:
# Youngest:
#
# Queue:           mail
# Waiting:         0
# Processing:      0
#
# Queue:           None
# Waiting:         0
# Processing:      0
#
# Connection:               127.0.0.1:49850
# Type:                     incoming
# State:                    established
# Since:                    1571143941 (2019-10-15 14:52:21, 7405 sec ago)
# Notifications Sent:       41
# Notifications Received:   41
# Pending Acknowledgements:
# Socket FD:                6
# HB. Interval:             10 sec
# LastIncomingData:         1571149829 (2019-10-15 16:30:29, 1517 sec ago)
# LastHeartbeat:            1571151344 (2019-10-15 16:55:44, 2 sec ago)
# InputBuffer:              0 Bytes
# OutputBuffer:             0 Bytes

import time
from typing import Any

from cmk.base.check_api import get_age_human_readable, LegacyCheckDefinition
from cmk.base.config import check_info
from cmk.base.plugins.agent_based.agent_based_api.v1 import IgnoreResultsError


def parse_mknotifyd(string_table):  # pylint: disable=too-many-branches
    try:
        timestamp, data = float(string_table[0][0]), string_table[1:]
    except (IndexError, ValueError):
        # versions before 1.5.0p23/1.6.0p4 did not include a timestamp
        timestamp, data = time.time(), string_table

    parsed: dict[str, Any] = {
        "sites": {},
        "timestamp": timestamp,
    }

    for line in data:
        if line[0].startswith("["):
            site = line[0][1:-1]
            site_entry: dict[str, dict] = {
                "spools": {},
                "connections": {},
                "queues": {},
            }
            sub_entry = site_entry
            parsed["sites"][site] = site_entry
        elif ":" in line[0]:
            varname, value = line[0].split(":", 1)
            if varname in ("Site", "Encryption"):
                # We do not use this data yet...
                continue
            value = value.strip()

            if varname == "Spool":
                sub_entry = {}
                site_entry["spools"][value] = sub_entry

            elif varname == "Connection":
                sub_entry = {}
                site_entry["connections"][value] = sub_entry

            elif varname == "Queue":
                sub_entry = {}
                site_entry["queues"][value] = sub_entry

            else:
                if value == "None":
                    value = None
                elif value and varname == "Listening FD":
                    # May be the listening FD number or an error message
                    try:
                        value = int(value.split()[0])
                    except ValueError:
                        pass
                elif (
                    value
                    and value != "-"
                    and varname
                    not in [
                        "Type",
                        "State",
                        "Version",
                        "Status Message",
                        "Pending Acknowledgements",
                        "Connect Time",
                    ]
                ):
                    value = int(value.split()[0])
                elif varname == "Connect Time":
                    value = float(value.split()[0])
                sub_entry[varname] = value

    # Fixup names of the connections. For incoming connections the remote
    # port is irrelevant. It changes randomly. But there might anyway be
    # more than one connection from the same remote host, so we are forced
    # to create artificial numbers if that is the case
    for stats in parsed["sites"].values():
        remote_addresses: dict = {}
        for connection_name, connection in list(stats["connections"].items()):
            if connection["Type"] == "incoming":
                remote_address = connection_name.split(":")[0]
                remote_addresses.setdefault(remote_address, []).append(connection)
                del stats["connections"][connection_name]

        for address, connections in remote_addresses.items():
            if len(connections) == 1:
                stats["connections"][address] = connections[0]
            else:
                for nr, connection in enumerate(connections):
                    stats["connections"][address + "/" + str(nr + 1)] = connection

    return parsed


#   .--Spooler Status------------------------------------------------------.
#   | ____                    _             ____  _        _               |
#   |/ ___| _ __   ___   ___ | | ___ _ __  / ___|| |_ __ _| |_ _   _ ___   |
#   |\___ \| '_ \ / _ \ / _ \| |/ _ \ '__| \___ \| __/ _` | __| | | / __|  |
#   | ___) | |_) | (_) | (_) | |  __/ |     ___) | || (_| | |_| |_| \__ \  |
#   ||____/| .__/ \___/ \___/|_|\___|_|    |____/ \__\__,_|\__|\__,_|___/  |
#   |      |_|                                                             |
#   +----------------------------------------------------------------------+
#   |                                                                      |
#   '----------------------------------------------------------------------'


def inventory_mknotifyd(parsed):
    return [(p, {}) for p in parsed["sites"]]


def check_mknotifyd(item, _no_params, parsed):
    sites = parsed["sites"]
    if item not in sites:
        yield 2, "No status information, Spooler not running"
        return

    # There are dummy-entries created during the parsing. So the
    # dict will never be completely empty. We check for Version
    # because this should be always present in a valid state file.
    stat = sites[item]
    if not stat.get("Version"):
        yield 2, "The state file seems to be empty or corrupted. It is very likely that the spooler is not working properly"
        return

    # Output Version
    yield 0, "Version: " + stat["Version"], []

    # Check age of status file. It's updated every 20 seconds
    status_age = parsed["timestamp"] - stat["Updated"]
    if status_age > 90:
        state = 2
        infotext = (
            "Status last updated %s ago, spooler seems crashed or busy"
            % get_age_human_readable(status_age)
        )
    else:
        state = 0
        infotext = "Spooler running"
    yield state, infotext, [
        ("last_updated", status_age),
        ("new_files", stat["spools"]["New"]["Count"]),
    ]

    # Are there any corrupted files
    corrupted = stat["spools"]["Corrupted"]
    if corrupted["Count"]:
        age = parsed["timestamp"] - corrupted["Youngest"]
        perf_data = [("corrupted_files", corrupted["Count"])]
        yield 1, "%d corrupted files: youngest %s ago" % (
            corrupted["Count"],
            get_age_human_readable(age),
        ), perf_data

    # Are there deferred files that are too old?
    deferred = stat["spools"]["Deferred"]
    if deferred["Count"]:
        age = parsed["timestamp"] - deferred["Oldest"]
        count = deferred["Count"]
        perf_data = [("deferred_age", age), ("deferred_files", deferred["Count"])]
        if age > 5:
            state = 1
        elif age > 600:
            state = 2
        else:
            state = 0
        yield state, "%d deferred files: oldest %s ago" % (
            count,
            get_age_human_readable(age),
        ), perf_data

    return


check_info["mknotifyd"] = LegacyCheckDefinition(
    parse_function=parse_mknotifyd,
    service_name="OMD %s Notification Spooler",
    discovery_function=inventory_mknotifyd,
    check_function=check_mknotifyd,
)

#   .--Connections---------------------------------------------------------.
#   |        ____                            _   _                         |
#   |       / ___|___  _ __  _ __   ___  ___| |_(_) ___  _ __  ___         |
#   |      | |   / _ \| '_ \| '_ \ / _ \/ __| __| |/ _ \| '_ \/ __|        |
#   |      | |__| (_) | | | | | | |  __/ (__| |_| | (_) | | | \__ \        |
#   |       \____\___/|_| |_|_| |_|\___|\___|\__|_|\___/|_| |_|___/        |
#   |                                                                      |
#   '----------------------------------------------------------------------'


def inventory_mknotifyd_connection(parsed):
    for site_name, stats in parsed["sites"].items():
        for connection_name in stats["connections"]:
            yield site_name + "-" + connection_name, {}


def check_mknotifyd_connection(item, _no_params, parsed):
    site_name, connection_name = item.split("-", 1)

    if site_name not in parsed["sites"]:
        raise IgnoreResultsError("No status information about spooler available")

    states = {
        "established": (0, "Alive"),
        "cooldown": (2, "Connection failed or terminated"),
        "initial": (1, "Initialized"),
        "connecting": (2, "Trying to connect"),
    }

    connections = parsed["sites"][site_name]["connections"]
    if connection_name in connections:
        connection = connections[connection_name]

        # First check state
        state, state_name = states[connection["State"]]
        yield state, state_name

        if "Status Message" in connection:
            yield 0, connection["Status Message"]

        # Show uptime
        if connection["State"] == "established":
            age = parsed["timestamp"] - connection["Since"]
            yield 0, "Uptime: %s" % get_age_human_readable(age)

            if "Connect Time" in connection:
                yield 0, "Connect time: %.3f sec" % connection["Connect Time"]

        # Stats
        for what in ("Sent", "Received"):
            num = connection["Notifications " + what]
            if num:
                yield 0, "%d Notifications %s" % (num, what.lower())


check_info["mknotifyd.connection"] = LegacyCheckDefinition(
    service_name="OMD %s Notify Connection",
    sections=["mknotifyd"],
    discovery_function=inventory_mknotifyd_connection,
    check_function=check_mknotifyd_connection,
)
