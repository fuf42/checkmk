#!/usr/bin/env python3
# Copyright (C) 2019 Checkmk GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.

# <<<mongodb_replica_status>>>
# <json>


import datetime
import enum
import json
import time
from collections.abc import Iterable, Mapping

from cmk.base.check_api import (
    check_levels,
    get_age_human_readable,
    get_timestamp_human_readable,
    LegacyCheckDefinition,
)
from cmk.base.config import check_info
from cmk.base.plugins.agent_based.agent_based_api.v1 import get_value_store

from cmk.plugins.lib.mongodb import parse_date

# levels_mongdb_replication_lag: (lag threshold, time interval for warning, time interval for critical)

Section = Mapping


def parse_mongodb_replica_set(string_table):
    """
    :param string_table: dictionary with all data for all checks and subchecks
    :return:
    """
    if string_table:
        return json.loads(str(string_table[0][0]))
    return {}


#   .--replication lag-----------------------------------------------------.
#   |                  _ _           _   _               _                 |
#   |   _ __ ___ _ __ | (_) ___ __ _| |_(_) ___  _ __   | | __ _  __ _     |
#   |  | '__/ _ \ '_ \| | |/ __/ _` | __| |/ _ \| '_ \  | |/ _` |/ _` |    |
#   |  | | |  __/ |_) | | | (_| (_| | |_| | (_) | | | | | | (_| | (_| |    |
#   |  |_|  \___| .__/|_|_|\___\__,_|\__|_|\___/|_| |_| |_|\__,_|\__, |    |
#   |           |_|                                              |___/     |
#   +----------------------------------------------------------------------+
# .


class ReplicaState(enum.IntEnum):
    PRIMARY = 1
    ARBITER = 7


def discover_mongodb_replica_set(section: Section) -> Iterable[tuple[None, dict]]:
    if section:
        yield None, {}


def check_mongodb_replica_set_lag(_item, params, status_dict):
    """
    based on MongoDB script 'db.printSlaveReplicationInfo'
    :param _item: <not_used>
    :param _params: mongodb_replica_set_levels parameters
    :param status_dict:
    :return:
    """
    # to calculate replication lag we need at least two members
    number_of_replica_set_members = len(status_dict.get("members", []))
    if number_of_replica_set_members <= 1:
        yield 1, "Number of members is %d" % number_of_replica_set_members
        return

    # get primary and other members (besides arbiters)
    primary, secondaries = _get_primary(status_dict.get("members"))

    # get timestamp of the last entry in the oplog from primary (if available)
    start_operation_timestamp, name = _get_start_timestamp(primary, secondaries)

    long_output = []
    # loop through members and calculate replication lag
    for member in secondaries:
        member_name = member.get("name", "unknown")

        if member.get("optime", {}).get("ts", {}).get("$timestamp", {}).get("t", None):
            # calculate replication lag
            member_optime_date = parse_date(member.get("optimeDate", {}).get("$date", 0))
            replication_lag_sec = _calculate_replication_lag(
                start_operation_timestamp, member_optime_date
            )

            # check it
            yield _check_lag_over_time(
                time.time(),
                member_name,
                name,
                replication_lag_sec,
                params.get("levels_mongdb_replication_lag"),
            )

            # add to long output
            long_output.append(
                _get_long_output(member_name, member_optime_date, replication_lag_sec, name)
            )
        else:
            # no info available yet
            yield 0, "%s: no replication info yet, State: %d" % (
                member_name,
                member.get("state", 0),
            )

    yield 0, "\n" + "\n".join(long_output)


def _check_lag_over_time(new_timestamp, member_name, name, lag_in_sec, levels):
    member_state_name = "mongodb.replica.set.lag.%s" % member_name
    value_store = get_value_store()
    if lag_in_sec > levels[0]:
        # I don't think getting zero by default is the right thing to do here.
        last_timestamp = value_store.get(member_state_name, 0.0)
        lag_duration = new_timestamp - last_timestamp
        state, infotext, _ = check_levels(
            lag_duration,
            None,
            levels[1:],
            human_readable_func=get_age_human_readable,
            infoname=f"{member_name} is behind {name} for",
        )

        if last_timestamp == 0:
            value_store[member_state_name] = new_timestamp
        elif state:
            return state, infotext

        return 0, "", []

    # zero? see above!
    value_store[member_state_name] = 0.0

    return 0, "", []


def _get_long_output(member_name, member_optime_date, replication_lag_sec, name):
    log = []
    log.append("source: %s" % member_name)
    log.append(
        "syncedTo: %s (UTC)"
        % (
            datetime.datetime.fromtimestamp(member_optime_date / 1000.0).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
        )
    )
    log.append(
        "member (%s) is %ds (%dh) behind %s"
        % (member_name, round(replication_lag_sec), round((replication_lag_sec / 36) / 100.0), name)
    )
    log.append("")
    return "\n".join(log)


def _get_start_timestamp(primary, secondaries):
    """
    Get timestamp of the last entry in the oplog from primary.
    If there is no primary, get the newest timestamp from the other members.
    :param primary: primary of replica set if available
    :param secondaries: rest of replica set
    :return: start of operation and name of member
    """
    start_operation_timestamp = 0.0
    name = "unknown"
    if primary:
        start_operation_timestamp = parse_date(primary.get("optimeDate", {}).get("$date", 0))
        name = "primary (%s)" % primary.get("name")
    else:
        index_to_delete = -1
        for index, member in enumerate(secondaries):
            if parse_date(member.get("optimeDate", {}).get("$date", 0)) > start_operation_timestamp:
                start_operation_timestamp = parse_date(member.get("optimeDate", {}).get("$date", 0))
                name = "freshest member (%s, no primary available at the moment)" % member.get(
                    "name"
                )
                index_to_delete = index

        # remove member from the list to avoid comparing to itself later
        if index_to_delete != -1:
            secondaries.pop(index_to_delete)

    return start_operation_timestamp, name


def _calculate_replication_lag(start_operation_time, secondary_operation_time):
    """
    calculate time difference when the last oplog entry was written to the secondary
    :param primary:
    :param start_operation_time:
    :param secondary_operation_time:
    :return: replication lag in seconds
    """
    return (start_operation_time - secondary_operation_time) / 1000.0


check_info["mongodb_replica_set"] = LegacyCheckDefinition(
    parse_function=parse_mongodb_replica_set,
    service_name="MongoDB Replication Lag",
    discovery_function=discover_mongodb_replica_set,
    check_function=check_mongodb_replica_set_lag,
    check_ruleset_name="mongodb_replica_set",
    check_default_parameters={"levels_mongdb_replication_lag": (10, 60, 3600)},
)

#   .--primary election----------------------------------------------------.
#   |                          _                                           |
#   |               _ __  _ __(_)_ __ ___   __ _ _ __ _   _                |
#   |              | '_ \| '__| | '_ ` _ \ / _` | '__| | | |               |
#   |              | |_) | |  | | | | | | | (_| | |  | |_| |               |
#   |              | .__/|_|  |_|_| |_| |_|\__,_|_|   \__, |               |
#   |              |_|                                |___/                |
#   |                      _           _   _                               |
#   |                  ___| | ___  ___| |_(_) ___  _ __                    |
#   |                 / _ \ |/ _ \/ __| __| |/ _ \| '_ \                   |
#   |                |  __/ |  __/ (__| |_| | (_) | | | |                  |
#   |                 \___|_|\___|\___|\__|_|\___/|_| |_|                  |
#   |                                                                      |
#   +----------------------------------------------------------------------+
# .


def check_mongodb_primary_election(_item, _params, status_dict):
    """
    checks if primary has changed between last check
    :param _item: <not_used>
    :param _params: <not_used>
    :param status_dict:
    :return:
    """
    # consistency check
    if not status_dict.get("members"):
        yield 1, "Replica set has no members"
        return

    # get primary member
    primary_dict = _get_primary(status_dict.get("members"))[0]
    # get primary name
    primary_name = primary_dict.get("name", None)
    # get primary election timestamp
    primary_election_time = _get_primary_election_time(primary_dict)

    # consistency check
    if not primary_name or not primary_election_time:
        yield 1, "Can not retrieve primary name and election date"
        return

    value_store = get_value_store()
    # get primary information from last check
    last_primary_dict = value_store.get("mongodb_primary_election", {})

    # check if primary or election date has changed between checks
    primary_name_changed = bool(
        last_primary_dict and last_primary_dict.get("name", primary_name) != primary_name
    )
    election_date_changed = bool(
        last_primary_dict
        and last_primary_dict.get("election_time", primary_election_time) != primary_election_time
    )

    # warning if primary has changed
    if last_primary_dict and (primary_name_changed or election_date_changed):
        yield 1, "New primary '{}' elected {} {}".format(
            primary_name,
            get_timestamp_human_readable(primary_election_time),
            "(%s)" % ("node changed" if primary_name_changed else "election date changed"),
        )
    else:
        yield 0, "Primary '{}' elected {}".format(
            primary_name,
            get_timestamp_human_readable(primary_election_time),
        )

    # update primary information
    value_store["mongodb_primary_election"] = {
        "name": primary_name,
        "election_time": primary_election_time,
    }


def _get_primary_election_time(primary):
    """
    Get election date for primary
    :param primary: name of primary
    :return: election date as datetime
    """
    if not primary:
        return None
    return primary.get("electionTime", {}).get("$timestamp", {}).get("t", None)


check_info["mongodb_replica_set.election"] = LegacyCheckDefinition(
    service_name="MongoDB Replica Set Primary Election",
    sections=["mongodb_replica_set"],
    discovery_function=discover_mongodb_replica_set,
    check_function=check_mongodb_primary_election,
    check_ruleset_name="mongodb_replica_set",
)


def _get_primary(member_list):
    """
    Get primary from list of members, put the rest in secondary list.
    :param member_list:
    :return:
    """
    primary = {}
    secondaries = []
    for member in member_list:
        if member.get("state", -1) == ReplicaState.PRIMARY:
            primary = member
            continue
        if member.get("state", -1) == ReplicaState.ARBITER:
            # ignore arbiters(7)
            continue

        secondaries.append(member)
    return primary, secondaries
