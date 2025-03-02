#!/usr/bin/env python3
# Copyright (C) 2019 Checkmk GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.

# <<<mongodb_chunks>>>
# <json>


# mypy: disable-error-code="var-annotated,arg-type"

import json
from collections.abc import Iterable, Mapping

from cmk.base.check_api import get_bytes_human_readable, LegacyCheckDefinition
from cmk.base.config import check_info

Section = Mapping


def parse_mongodb_cluster(string_table):
    """
    :param string_table: dictionary with all data for all checks and subchecks
    :return:
    """
    if string_table:
        return json.loads(str(string_table[0][0]))
    return {}


#   .--database------------------------------------------------------------.
#   |                  _       _        _                                  |
#   |               __| | __ _| |_ __ _| |__   __ _ ___  ___               |
#   |              / _` |/ _` | __/ _` | '_ \ / _` / __|/ _ \              |
#   |             | (_| | (_| | || (_| | |_) | (_| \__ \  __/              |
#   |              \__,_|\__,_|\__\__,_|_.__/ \__,_|___/\___|              |
#   |                                                                      |
#   +----------------------------------------------------------------------+-'
# .


def inventory_mongodb_cluster_databases(databases_dict):
    """
    one service per database
    :param databases_dict:
    :return:
    """
    return [(str(name), {}) for name in databases_dict.get("databases", {})]


def check_mongodb_cluster_databases(item, _params, databases_dict):
    """
    checks:
    if database is partitioned (only output)
    if number of collections is 0
    primary shard for database (only output)
    :param item: database name
    :param _params: not used
    :param databases_dict: dictionary with all data
    :return:
    """
    database = databases_dict.get("databases", {}).get(item, {})

    # is partitioned
    yield 0, "Partitioned: %s" % ("true" if database.get("partitioned", False) else "false")

    # number of collections
    number_of_collections = len(database.get("collections", []))
    collection_info = "Collections: %d" % number_of_collections
    if number_of_collections > 0:
        yield 0, collection_info
    else:
        yield 1, collection_info

    # get primary
    yield 0, "Primary: %s" % database.get("primary")


check_info["mongodb_cluster"] = LegacyCheckDefinition(
    parse_function=parse_mongodb_cluster,
    service_name="MongoDB Database: %s",
    discovery_function=inventory_mongodb_cluster_databases,
    check_function=check_mongodb_cluster_databases,
    check_ruleset_name="mongodb_cluster",
)

#   .--shards--------------------------------------------------------------.
#   |                        _                   _                         |
#   |                    ___| |__   __ _ _ __ __| |___                     |
#   |                   / __| '_ \ / _` | '__/ _` / __|                    |
#   |                   \__ \ | | | (_| | | | (_| \__ \                    |
#   |                   |___/_| |_|\__,_|_|  \__,_|___/                    |
#   |                                                                      |
#   +----------------------------------------------------------------------+
# .

# description: [([interval for total number of chunks[, number of chunks threshold),...]
BALANCE_THRESHOLDS = [((0, 20), 2), ((21, 79), 4), ((80, 2**31 - 1), 8)]


def inventory_mongodb_cluster_shards(databases_dict):
    """
    one service per collection
    :param databases_dict:
    :return:
    """
    db_coll_list = []
    for db_name in databases_dict.get("databases", {}):
        db_coll_list += [
            (f"{db_name}.{coll_name}", {})
            for coll_name in databases_dict.get("databases").get(db_name).get("collections", [])
        ]
    return db_coll_list


def check_mongodb_cluster_shards(item, params, databases_dict):
    """
    checks:
    if collection is sharded (only output)
    if collection is balanced
    if collection's balancer is disabled
    if shards contain jumbo chunks

    last check just generates perfdata and long output
    :param item: namespace (database_name.collection_name)
    :param params: levels
    :param databases_dict: dictionary with all data
    :return:
    """
    if "databases" not in databases_dict:
        return

    database_name, collection_name = _mongodb_cluster_split_namespace(item)

    #
    # output if collection is sharded or unsharded
    #
    is_sharded = (
        databases_dict.get("databases")
        .get(database_name)
        .get("collstats", {})
        .get(collection_name, {})
        .get("sharded", False)
    )
    yield 0, "Collection: %s" % ("sharded" if is_sharded else "unsharded")

    #
    # check if collection is balanced
    #
    if is_sharded:
        yield _mongodb_cluster_collection_is_balanced(
            databases_dict.get("databases")
            .get(database_name)
            .get("collstats", {})
            .get(collection_name, {})
        )

    #
    # check if balancer is disabled for collection
    #
    if is_sharded:
        yield _mongodb_cluster_is_balancer_disabled(
            databases_dict.get("databases")
            .get(database_name)
            .get("collstats", {})
            .get(collection_name, {})
        )

    #
    # check if shard contains jumbo chunks
    #
    if is_sharded:
        yield _mongodb_cluster_shard_has_jumbos(
            params.get("levels_number_jumbo"),
            databases_dict.get("databases")
            .get(database_name)
            .get("collstats", {})
            .get(collection_name, {}),
        )

    #
    # generate long output
    #
    perf_data = _mongodb_cluster_generate_performance_data(
        databases_dict.get("databases")
        .get(database_name)
        .get("collstats", {})
        .get(collection_name, {}),
        params,
    )
    yield _generate_mongodb_cluster_long_output(
        is_sharded,
        databases_dict.get("databases")
        .get(database_name)
        .get("collstats", {})
        .get(collection_name, {}),
        databases_dict.get("databases").get(database_name).get("primary", "unknown"),
        databases_dict.get("settings", {}),
        databases_dict.get("shards", {}),
        perf_data,
    )


def _mongodb_cluster_is_balancer_disabled(collection_dict):
    """
    check if balancer is enabled for the collection.
    if balancer is disabled, "noBalance" is present and set to true
    :param collection_dict: dictionary holding collections information
    :return:
    """
    if "noBalance" in collection_dict and collection_dict.get("noBalance", False):
        return 1, "Balancer: disabled"
    return 0, "Balancer: enabled"


def _mongodb_cluster_shard_has_jumbos(levels, collection_dict):
    """
    loop through shards and check if jumbo key is set

    :param collection_dict: dictionary holding collections information
    :param levels: warn/crit levels for jumbo chunks
    :return:
    """
    warning_info = []
    warning_level = 0
    # loop through all shards and check if number of jumbo > levels
    for shard_name in sorted(collection_dict.get("shards", {})):
        number_of_jumbos = collection_dict.get("shards").get(shard_name).get("numberOfJumbos", 0)
        if number_of_jumbos >= levels[1]:
            warning_level = 2
        elif number_of_jumbos >= levels[0]:
            warning_level = 1

        if number_of_jumbos >= levels[0]:
            warning_info.append(
                "%s (%d jumbo %s)"
                % (shard_name, number_of_jumbos, "chunks" if number_of_jumbos > 1 else "chunk")
            )

    return warning_level, "Jumbo: %s" % (
        "[%s]" % ", ".join(warning_info) if warning_level > 0 else "0"
    )


def _mongodb_cluster_collection_is_balanced(collection_dict):
    """
    get chunks per shard and compare to expected number of chunks per shard. based on mongoDB thresholds (2, 4 or 8)
    for the balancer, mark collections as unbalanced

    :param collection_dict: dictionary holding collections information
    :return:
    """
    # retrieve information from dictionary
    total_number_of_chunks = collection_dict.get("nchunks", 0)
    total_number_of_shards = len(collection_dict.get("shards", 1))
    average_chunks_per_shard = total_number_of_chunks / total_number_of_shards

    warning_info = []

    # loop through all shards and check if one shard is out of balance
    balanced = True
    for shard_name in sorted(collection_dict.get("shards", {})):
        # get shard information
        number_of_chunks_in_shard = (
            collection_dict.get("shards").get(shard_name).get("numberOfChunks", 0)
        )

        # check if shard is in balance
        balanced &= _mongodb_cluster_is_balanced(
            total_number_of_chunks, average_chunks_per_shard, number_of_chunks_in_shard
        )

        # generate additional warning output
        warning_info.append("%s (%d chunks)" % (shard_name, number_of_chunks_in_shard))

    if balanced:
        return 0, "Chunks: balanced"
    return 1, "Chunks: unbalanced [%s]" % ", ".join(warning_info)


def _mongodb_cluster_is_balanced(
    total_number_of_chunks, average_chunks_per_shard, number_of_chunks_in_shard
):
    """
    check if deviation of chunks in shard is in range depended on total number of chunks

    < 20 : deviation more than 2 chunks
    20-79: deviation more than 4 chunks
    >= 80: deviation more than 8 chunks
    :param total_number_of_chunks: total number of chunks of all shards
    :param average_chunks_per_shard: expected number of chunks per shard
    :param number_of_chunks_in_shard: actual number of chunks in shard
    :return: true if in balance, else false
    """
    diff_chunks = number_of_chunks_in_shard - average_chunks_per_shard

    for threshold in BALANCE_THRESHOLDS:
        if threshold[0][0] >= total_number_of_chunks > threshold[0][1]:
            if diff_chunks > threshold[1]:
                return False
    return True


def _generate_mongodb_cluster_long_output(
    is_sharded, collection_dict, primary_shard_name, settings_dict, shards_dict, perf_data
):
    """
    create long output with collection and shard information
    :param is_sharded: flag if collection is sharded
    :param collection_dict: dictionary holding collections information
    :param primary_shard_name: name of the primary shard of the collection
    :param shards_dict: shards dictionary (mondoDB config.shards)
    :param settings_dict: dictionary holding settings information (mondoDB config.settings)
    :return:
    """
    # set flags
    has_chunks = "nchunks" in collection_dict
    has_shards = "shards" in collection_dict

    # retrieve information from dictionary
    total_number_of_chunks = collection_dict.get("nchunks", 0)
    chunk_size = settings_dict.get("chunkSize", 65536)
    collection_shards_dict = collection_dict.get("shards", {})
    total_number_of_documents = collection_dict.get("count", 0)
    total_collection_size = collection_dict.get("size", 0)
    storage_size = collection_dict.get("storageSize", 0)
    balancer_status = (
        "disabled"
        if "noBalance" in collection_dict and collection_dict.get("noBalance", False)
        else "enabled"
    )

    # output per collection
    collections_info = ["Collection"]
    if has_shards:
        collections_info.append("- Shards: %d" % len(collection_shards_dict))
    if has_chunks:
        collections_info.append(
            "- Chunks: %d (Default chunk size: %s)"
            % (total_number_of_chunks, get_bytes_human_readable(chunk_size))
        )
    collections_info.append("- Docs: %d" % total_number_of_documents)
    collections_info.append("- Size: %s" % get_bytes_human_readable(total_collection_size))
    collections_info.append("- Storage: %s" % get_bytes_human_readable(storage_size))
    if is_sharded:
        collections_info.append("- Balancer: %s" % balancer_status)

    # output per shard
    shard_info = []
    for shard_name in sorted(collection_dict.get("shards", {})):
        aggregated_shards_dict = collection_dict.get("shards").get(shard_name).copy()
        aggregated_shards_dict["hostname"] = shards_dict.get(shard_name, {}).get("host", "unknown")
        aggregated_shards_dict["name"] = shard_name
        aggregated_shards_dict["is_primary"] = shard_name == primary_shard_name
        shard_info.append(
            "\n"
            + _mongodb_cluster_get_shard_statistic_info(
                is_sharded, aggregated_shards_dict, total_collection_size, total_number_of_documents
            )
        )

    return 0, "\n{}\n{}".format("\n".join(collections_info), "\n".join(shard_info)), perf_data


def _mongodb_cluster_get_shard_statistic_info(
    is_sharded, shard_dict, total_shard_size, total_number_of_documents
):
    """
    create output for shard information
    :param is_sharded: boolean, is shard sharded or unsharded
    :param shard_dict: dictionary with shard information
    :param total_shard_size: total size of all shards
    :param total_number_of_documents: total number of all documents
    :return:
    """
    # get shard information
    number_of_chunks = shard_dict.get("numberOfChunks", 0)
    number_of_jumbos = shard_dict.get("numberOfJumbos", 0)
    shard_size = shard_dict.get("size", 0)
    number_of_documents = shard_dict.get("count", 0)
    hostname = shard_dict.get("hostname", "unknown")
    shard_name = shard_dict.get("name", "unknown")
    is_primary = shard_dict.get("is_primary", False)

    # calculate some stuff
    estDataPercent = (float(shard_size) / total_shard_size * 100) if total_shard_size > 0 else 0
    estDocPercent = (
        (float(number_of_documents) / total_number_of_documents * 100)
        if total_number_of_documents > 0
        else 0
    )
    estChunkData = (float(shard_size) / number_of_chunks) if number_of_chunks > 0 else 0
    estChunkCount = (float(number_of_documents) / number_of_chunks) if number_of_chunks > 0 else 0

    shard_name_info = "{}{}".format(shard_name, " (primary)" if is_primary else "")

    output = ["Shard %s" % shard_name_info]
    output.append("- Chunks: %d" % number_of_chunks)
    output.append("- Jumbos: %d" % number_of_jumbos)
    output.append(
        "- Docs: %d%s" % (number_of_documents, " (%1.2f%%)" % estDocPercent if is_sharded else "")
    )
    if is_sharded:
        output.append("--- per chunk: " + "\u2248" + " %d" % estChunkCount)
    output.append(
        "- Size: %s%s"
        % (
            get_bytes_human_readable(shard_size),
            " (%1.2f%%)" % estDataPercent if is_sharded else "",
        )
    )
    if is_sharded:
        output.append("--- per chunk: " + "\u2248" + " %s" % get_bytes_human_readable(estChunkData))
    output.append("- Host: %s" % hostname)
    return "\n".join(output)


def _mongodb_cluster_generate_performance_data(collection_dict, params):
    """
    create all performance data
    :param collection_dict: dictionary holding collections information
    :param params: thresholds
    :return:
    """
    # set flags
    has_chunks = "nchunks" in collection_dict

    # get perfdata data
    number_of_chunks = collection_dict.get("nchunks", 0)
    number_of_documents = collection_dict.get("count", 0)
    collection_size = collection_dict.get("size", 0)
    storage_size = collection_dict.get("storageSize", 0)

    # count jumbo chunks
    total_number_of_jumbos = 0
    has_shards = collection_dict.get("shards", False)
    if has_shards:
        for shard_name in collection_dict.get("shards"):
            number_of_jumbos = (
                collection_dict.get("shards").get(shard_name).get("numberOfJumbos", 0)
            )
            total_number_of_jumbos += number_of_jumbos

    perfdata = []
    perfdata.append(("mongodb_collection_size", collection_size))
    perfdata.append(("mongodb_collection_storage_size", storage_size))
    perfdata.append(("mongodb_document_count", number_of_documents))
    if has_chunks:
        perfdata.append(("mongodb_chunk_count", number_of_chunks))
    if has_shards:
        perfdata.append(
            (
                "mongodb_jumbo_chunk_count",
                total_number_of_jumbos,
                params.get("levels_number_jumbo", [0, 0])[0],
                params.get("levels_number_jumbo", [0, 0])[1],
            )
        )
    return perfdata


def _mongodb_cluster_split_namespace(namespace):
    """
    split namespace into database name and collection name
    :param namespace:
    :return:
    """
    try:
        names = namespace.split(".", 1)
        if len(names) > 1:
            return names[0], names[1]
        if len(names) > 0:
            return names[0], ""
    except ValueError:
        pass
    except AttributeError:
        pass
    raise ValueError("error parsing namespace %s" % namespace)


check_info["mongodb_cluster.collections"] = LegacyCheckDefinition(
    service_name="MongoDB Cluster: %s",
    sections=["mongodb_cluster"],
    discovery_function=inventory_mongodb_cluster_shards,
    check_function=check_mongodb_cluster_shards,
    check_ruleset_name="mongodb_cluster",
    check_default_parameters={"levels_number_jumbo": (1, 2)},
)

#   .--balancer------------------------------------------------------------.
#   |               _           _                                          |
#   |              | |__   __ _| | __ _ _ __   ___ ___ _ __                |
#   |              | '_ \ / _` | |/ _` | '_ \ / __/ _ \ '__|               |
#   |              | |_) | (_| | | (_| | | | | (_|  __/ |                  |
#   |              |_.__/ \__,_|_|\__,_|_| |_|\___\___|_|                  |
#   |                                                                      |
#   +----------------------------------------------------------------------+
# .


def discover_mongodb_cluster_balancer(section: Section) -> Iterable[tuple[None, dict]]:
    if section:
        yield None, {}


def check_mongodb_cluster_balancer(_item, _params, databases_dict):
    if "balancer" not in databases_dict:
        return

    if databases_dict.get("balancer").get("balancer_enabled"):
        yield 0, "Balancer: enabled"
    else:
        yield 2, "Balancer: disabled"


check_info["mongodb_cluster.balancer"] = LegacyCheckDefinition(
    service_name="MongoDB Balancer",
    sections=["mongodb_cluster"],
    discovery_function=discover_mongodb_cluster_balancer,
    check_function=check_mongodb_cluster_balancer,
)
