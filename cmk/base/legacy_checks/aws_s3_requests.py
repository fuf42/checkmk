#!/usr/bin/env python3
# Copyright (C) 2019 Checkmk GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.


from cmk.base.check_api import check_levels, get_age_human_readable, LegacyCheckDefinition
from cmk.base.check_legacy_includes.aws import (
    aws_get_bytes_rate_human_readable,
    aws_get_counts_rate_human_readable,
    aws_get_parsed_item_data,
    check_aws_http_errors,
    check_aws_metrics,
    inventory_aws_generic,
)
from cmk.base.config import check_info
from cmk.base.plugins.agent_based.agent_based_api.v1 import IgnoreResultsError, render

from cmk.plugins.lib.aws import extract_aws_metrics_by_labels, parse_aws


def parse_aws_s3(string_table):
    parsed = extract_aws_metrics_by_labels(
        [
            "AllRequests",
            "GetRequests",
            "PutRequests",
            "DeleteRequests",
            "HeadRequests",
            "PostRequests",
            "SelectRequests",
            "ListRequests",
            "4xxErrors",
            "5xxErrors",
            "FirstByteLatency",
            "TotalRequestLatency",
            "BytesDownloaded",
            "BytesUploaded",
            "SelectBytesScanned",
            "SelectBytesReturned",
        ],
        parse_aws(string_table),
    )
    return parsed


#   .--requests------------------------------------------------------------.
#   |                                              _                       |
#   |               _ __ ___  __ _ _   _  ___  ___| |_ ___                 |
#   |              | '__/ _ \/ _` | | | |/ _ \/ __| __/ __|                |
#   |              | | |  __/ (_| | |_| |  __/\__ \ |_\__ \                |
#   |              |_|  \___|\__, |\__,_|\___||___/\__|___/                |
#   |                           |_|                                        |
#   '----------------------------------------------------------------------'


@aws_get_parsed_item_data
def check_aws_s3_requests(item, params, metrics):
    all_requests_rate = metrics.get("AllRequests")
    if all_requests_rate is None:
        raise IgnoreResultsError("Currently no data from AWS")
    yield 0, "Total: %s" % aws_get_counts_rate_human_readable(all_requests_rate)

    for key, perf_key, title in [
        ("GetRequests", "get_requests", "Get"),
        ("PutRequests", "put_requests", "Put"),
        ("DeleteRequests", "delete_requests", "Delete"),
        ("HeadRequests", "head_requests", "Head"),
        ("PostRequests", "post_requests", "Post"),
        ("SelectRequests", "select_requests", "Select"),
        ("ListRequests", "list_requests", "List"),
    ]:
        requests_rate = metrics.get(key, 0)

        yield 0, f"{title}: {aws_get_counts_rate_human_readable(requests_rate)}", [
            (perf_key, requests_rate)
        ]

        try:
            requests_perc = 100.0 * requests_rate / all_requests_rate
        except ZeroDivisionError:
            requests_perc = 0

        yield check_levels(
            requests_perc,
            "%s_perc" % perf_key,
            params.get("%s_perc" % perf_key),
            human_readable_func=render.percent,
            infoname="%s of total requests" % title,
        )


check_info["aws_s3_requests"] = LegacyCheckDefinition(
    parse_function=parse_aws_s3,
    service_name="AWS/S3 Requests %s",
    discovery_function=lambda p: inventory_aws_generic(p, ["AllRequests"]),
    check_function=check_aws_s3_requests,
    check_ruleset_name="aws_s3_requests",
)

# .
#   .--HTTP errors---------------------------------------------------------.
#   |       _   _ _____ _____ ____                                         |
#   |      | | | |_   _|_   _|  _ \    ___ _ __ _ __ ___  _ __ ___         |
#   |      | |_| | | |   | | | |_) |  / _ \ '__| '__/ _ \| '__/ __|        |
#   |      |  _  | | |   | | |  __/  |  __/ |  | | | (_) | |  \__ \        |
#   |      |_| |_| |_|   |_| |_|      \___|_|  |_|  \___/|_|  |___/        |
#   |                                                                      |
#   '----------------------------------------------------------------------'


@aws_get_parsed_item_data
def check_aws_s3_http_errors(item, params, metrics):
    return check_aws_http_errors(
        params.get("levels_load_balancers", params),
        metrics,
        ["4xx", "5xx"],
        "%sErrors",
        key_all_requests="AllRequests",
    )


check_info["aws_s3_requests.http_errors"] = LegacyCheckDefinition(
    service_name="AWS/S3 HTTP Errors %s",
    sections=["aws_s3_requests"],
    discovery_function=lambda p: inventory_aws_generic(
        p, ["AllRequests", "4xxErrors", "5xxErrors"]
    ),
    check_function=check_aws_s3_http_errors,
    check_ruleset_name="aws_s3_http_errors",
)

# .
#   .--latency-------------------------------------------------------------.
#   |                  _       _                                           |
#   |                 | | __ _| |_ ___ _ __   ___ _   _                    |
#   |                 | |/ _` | __/ _ \ '_ \ / __| | | |                   |
#   |                 | | (_| | ||  __/ | | | (__| |_| |                   |
#   |                 |_|\__,_|\__\___|_| |_|\___|\__, |                   |
#   |                                             |___/                    |
#   '----------------------------------------------------------------------'


@aws_get_parsed_item_data
def check_aws_s3_latency(item, params, metrics):
    metric_infos = []
    for key, title, perf_key in [
        ("TotalRequestLatency", "Total request latency", "aws_request_latency"),
        ("FirstByteLatency", "First byte latency", None),
    ]:
        metric_val = metrics.get(key)
        if metric_val:
            metric_val *= 1e-3

        if perf_key is None:
            levels = None
        else:
            levels = params.get("levels_seconds")
            if levels is not None:
                levels = tuple(level * 1e-3 for level in levels)

        metric_infos.append(
            {
                "metric_val": metric_val,
                "metric_name": perf_key,
                "levels": levels,
                "info_name": title,
                "human_readable_func": get_age_human_readable,
            }
        )

    return check_aws_metrics(metric_infos)


check_info["aws_s3_requests.latency"] = LegacyCheckDefinition(
    service_name="AWS/S3 Latency %s",
    sections=["aws_s3_requests"],
    discovery_function=lambda p: inventory_aws_generic(p, ["TotalRequestLatency"]),
    check_function=check_aws_s3_latency,
    check_ruleset_name="aws_s3_latency",
)

# .
#   .--traffic stats-------------------------------------------------------.
#   |         _              __  __ _            _        _                |
#   |        | |_ _ __ __ _ / _|/ _(_) ___   ___| |_ __ _| |_ ___          |
#   |        | __| '__/ _` | |_| |_| |/ __| / __| __/ _` | __/ __|         |
#   |        | |_| | | (_| |  _|  _| | (__  \__ \ || (_| | |_\__ \         |
#   |         \__|_|  \__,_|_| |_| |_|\___| |___/\__\__,_|\__|___/         |
#   |                                                                      |
#   '----------------------------------------------------------------------'


@aws_get_parsed_item_data
def check_aws_s3_traffic_stats(item, params, metrics):
    return check_aws_metrics(
        [
            {
                "metric_val": metrics.get(key),
                "metric_name": perf_key,
                "info_name": title,
                "human_readable_func": aws_get_bytes_rate_human_readable,
            }
            for key, title, perf_key in [
                ("BytesDownloaded", "Downloads", "aws_s3_downloads"),
                ("BytesUploaded", "Uploads", "aws_s3_uploads"),
            ]
        ]
    )


check_info["aws_s3_requests.traffic_stats"] = LegacyCheckDefinition(
    service_name="AWS/S3 Traffic Stats %s",
    sections=["aws_s3_requests"],
    discovery_function=lambda p: inventory_aws_generic(p, ["BytesDownloaded", "BytesUploaded"]),
    check_function=check_aws_s3_traffic_stats,
)

# .
#   .--select objects------------------------------------------------------.
#   |              _           _           _     _           _             |
#   |     ___  ___| | ___  ___| |_    ___ | |__ (_) ___  ___| |_ ___       |
#   |    / __|/ _ \ |/ _ \/ __| __|  / _ \| '_ \| |/ _ \/ __| __/ __|      |
#   |    \__ \  __/ |  __/ (__| |_  | (_) | |_) | |  __/ (__| |_\__ \      |
#   |    |___/\___|_|\___|\___|\__|  \___/|_.__// |\___|\___|\__|___/      |
#   |                                         |__/                         |
#   '----------------------------------------------------------------------'


@aws_get_parsed_item_data
def check_aws_s3_select_object(item, params, metrics):
    return check_aws_metrics(
        [
            {
                "metric_val": metrics.get(key),
                "metric_name": perf_key,
                "info_name": title,
                "human_readable_func": aws_get_bytes_rate_human_readable,
            }
            for key, title, perf_key in [
                ("SelectBytesScanned", "Scanned", "aws_s3_select_object_scanned"),
                ("SelectBytesReturned", "Returned", "aws_s3_select_object_returned"),
            ]
        ]
    )


check_info["aws_s3_requests.select_object"] = LegacyCheckDefinition(
    service_name="AWS/S3 SELECT Object %s",
    sections=["aws_s3_requests"],
    discovery_function=lambda p: inventory_aws_generic(
        p, ["SelectBytesScanned", "SelectBytesReturned"]
    ),
    check_function=check_aws_s3_select_object,
)
