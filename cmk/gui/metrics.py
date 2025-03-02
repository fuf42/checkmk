#!/usr/bin/env python3
# Copyright (C) 2019 Checkmk GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.

# Frequently used variable names:
# perf_data_string:   Raw performance data as sent by the core, e.g "foo=17M;1;2;4;5"
# perf_data:          Split performance data, e.g. [("foo", "17", "M", "1", "2", "4", "5")]
# translated_metrics: Completely parsed and translated into metrics, e.g. { "foo" : { "value" : 17.0, "unit" : { "render" : ... }, ... } }
# color:              RGB color representation ala HTML, e.g. "#ffbbc3" or "#FFBBC3", len() is always 7!
# color_rgb:          RGB color split into triple (r, g, b), where r,b,g in (0.0 .. 1.0)
# unit_name:          The ID of a unit, e.g. "%"
# unit:               The definition-dict of a unit like in unit_info
# graph_template:     Template for a graph. Essentially a dict with the key "metrics"

import json
from collections.abc import Callable, Sequence
from typing import Any

from livestatus import SiteId

import cmk.utils
import cmk.utils.plugin_registry
import cmk.utils.render
from cmk.utils.hostaddress import HostName
from cmk.utils.servicename import ServiceName

import cmk.gui.pages
import cmk.gui.utils as utils
from cmk.gui.exceptions import MKUserError
from cmk.gui.graphing import _color as graphing_color
from cmk.gui.graphing import _unit_info as graphing_unit_info
from cmk.gui.graphing import _utils as graphing_utils
from cmk.gui.graphing import parse_perfometers, perfometer_info
from cmk.gui.graphing._graph_specification import (
    CombinedSingleMetricSpec,
    GraphMetric,
    parse_raw_graph_specification,
)
from cmk.gui.graphing._html_render import (
    host_service_graph_dashlet_cmk,
    host_service_graph_popup_cmk,
)
from cmk.gui.graphing._type_defs import TranslatedMetric
from cmk.gui.graphing._utils import parse_perf_data, translate_metrics
from cmk.gui.http import request
from cmk.gui.i18n import _

PerfometerExpression = str | int | float
RequiredMetricNames = set[str]

#   .--Plugins-------------------------------------------------------------.
#   |                   ____  _             _                              |
#   |                  |  _ \| |_   _  __ _(_)_ __  ___                    |
#   |                  | |_) | | | | |/ _` | | '_ \/ __|                   |
#   |                  |  __/| | |_| | (_| | | | | \__ \                   |
#   |                  |_|   |_|\__,_|\__, |_|_| |_|___/                   |
#   |                                 |___/                                |
#   +----------------------------------------------------------------------+
#   |  Typical code for loading Multisite plugins of this module           |
#   '----------------------------------------------------------------------'


def load_plugins() -> None:
    """Plugin initialization hook (Called by cmk.gui.main_modules.load_plugins())"""
    _register_pre_21_plugin_api()
    utils.load_web_plugins("metrics", globals())
    parse_perfometers(perfometer_info)


def _register_pre_21_plugin_api() -> None:
    """Register pre 2.1 "plugin API"

    This was never an official API, but the names were used by built-in and also 3rd party plugins.

    Our built-in plugin have been changed to directly import from the .utils module. We add these old
    names to remain compatible with 3rd party plugins for now.

    In the moment we define an official plugin API, we can drop this and require all plugins to
    switch to the new API. Until then let's not bother the users with it.

    CMK-12228
    """
    # Needs to be a local import to not influence the regular plugin loading order
    import cmk.gui.plugins.metrics as legacy_api_module  # pylint: disable=cmk-module-layer-violation
    import cmk.gui.plugins.metrics.utils as legacy_plugin_utils  # pylint: disable=cmk-module-layer-violation

    for name in (
        "check_metrics",
        "G",
        "GB",
        "graph_info",
        "GraphTemplate",
        "K",
        "KB",
        "m",
        "M",
        "MAX_CORES",
        "MAX_NUMBER_HOPS",
        "MB",
        "metric_info",
        "P",
        "PB",
        "scale_symbols",
        "skype_mobile_devices",
        "T",
        "TB",
        "time_series_expression_registry",
    ):
        legacy_api_module.__dict__[name] = graphing_utils.__dict__[name]
        legacy_plugin_utils.__dict__[name] = graphing_utils.__dict__[name]

    legacy_api_module.__dict__["perfometer_info"] = perfometer_info
    legacy_plugin_utils.__dict__["perfometer_info"] = perfometer_info

    legacy_api_module.__dict__["unit_info"] = graphing_unit_info.__dict__["unit_info"]
    legacy_plugin_utils.__dict__["unit_info"] = graphing_unit_info.__dict__["unit_info"]

    for name in (
        "darken_color",
        "indexed_color",
        "lighten_color",
        "MONITORING_STATUS_COLORS",
        "parse_color",
        "parse_color_into_hexrgb",
        "render_color",
        "scalar_colors",
    ):
        legacy_api_module.__dict__[name] = graphing_color.__dict__[name]
        legacy_plugin_utils.__dict__[name] = graphing_color.__dict__[name]

    # Avoid needed imports, see CMK-12147
    globals().update(
        {
            "indexed_color": graphing_color.indexed_color,
            "metric_info": graphing_utils.metric_info,
            "check_metrics": graphing_utils.check_metrics,
            "graph_info": graphing_utils.graph_info,
        }
    )


# .
#   .--Helpers-------------------------------------------------------------.
#   |                  _   _      _                                        |
#   |                 | | | | ___| |_ __   ___ _ __ ___                    |
#   |                 | |_| |/ _ \ | '_ \ / _ \ '__/ __|                   |
#   |                 |  _  |  __/ | |_) |  __/ |  \__ \                   |
#   |                 |_| |_|\___|_| .__/ \___|_|  |___/                   |
#   |                              |_|                                     |
#   +----------------------------------------------------------------------+
#   |  Various helper functions                                            |
#   '----------------------------------------------------------------------'
# A few helper function to be used by the definitions


def metric_to_text(metric: dict[str, Any], value: int | float | None = None) -> str:
    if value is None:
        value = metric["value"]
    return metric["unit"]["render"](value)


# aliases to be compatible to old plugins
physical_precision = cmk.utils.render.physical_precision
age_human_readable = cmk.utils.render.approx_age

# .
#   .--Evaluation----------------------------------------------------------.
#   |          _____            _             _   _                        |
#   |         | ____|_   ____ _| |_   _  __ _| |_(_) ___  _ __             |
#   |         |  _| \ \ / / _` | | | | |/ _` | __| |/ _ \| '_ \            |
#   |         | |___ \ V / (_| | | |_| | (_| | |_| | (_) | | | |           |
#   |         |_____| \_/ \__,_|_|\__,_|\__,_|\__|_|\___/|_| |_|           |
#   |                                                                      |
#   +----------------------------------------------------------------------+
#   |  Parsing of performance data into metrics, evaluation of expressions |
#   '----------------------------------------------------------------------'


def translate_perf_data(
    perf_data_string: str, check_command: str | None = None
) -> dict[str, TranslatedMetric]:
    perf_data, check_command = parse_perf_data(perf_data_string, check_command)
    return translate_metrics(perf_data, check_command)


# .
#   .--Hover-Graph---------------------------------------------------------.
#   |     _   _                           ____                 _           |
#   |    | | | | _____   _____ _ __      / ___|_ __ __ _ _ __ | |__        |
#   |    | |_| |/ _ \ \ / / _ \ '__|____| |  _| '__/ _` | '_ \| '_ \       |
#   |    |  _  | (_) \ V /  __/ | |_____| |_| | | | (_| | |_) | | | |      |
#   |    |_| |_|\___/ \_/ \___|_|        \____|_|  \__,_| .__/|_| |_|      |
#   |                                                   |_|                |
#   '----------------------------------------------------------------------'


# This page is called for the popup of the graph icon of hosts/services.
def page_host_service_graph_popup(
    resolve_combined_single_metric_spec: Callable[
        [CombinedSingleMetricSpec], Sequence[GraphMetric]
    ],
) -> None:
    """Registered as `host_service_graph_popup`."""
    host_service_graph_popup_cmk(
        SiteId(raw_site_id) if (raw_site_id := request.var("site")) else None,
        HostName(request.get_str_input_mandatory("host_name")),
        ServiceName(request.get_str_input_mandatory("service")),
        resolve_combined_single_metric_spec,
    )


# .
#   .--Graph Dashlet-------------------------------------------------------.
#   |    ____                 _       ____            _     _      _       |
#   |   / ___|_ __ __ _ _ __ | |__   |  _ \  __ _ ___| |__ | | ___| |_     |
#   |  | |  _| '__/ _` | '_ \| '_ \  | | | |/ _` / __| '_ \| |/ _ \ __|    |
#   |  | |_| | | | (_| | |_) | | | | | |_| | (_| \__ \ | | | |  __/ |_     |
#   |   \____|_|  \__,_| .__/|_| |_| |____/ \__,_|___/_| |_|_|\___|\__|    |
#   |                  |_|                                                 |
#   +----------------------------------------------------------------------+
#   |  This page handler is called by graphs embedded in a dashboard.      |
#   '----------------------------------------------------------------------'


def page_graph_dashlet(
    resolve_combined_single_metric_spec: Callable[
        [CombinedSingleMetricSpec], Sequence[GraphMetric]
    ],
) -> None:
    """Registered as `graph_dashlet`."""
    spec = request.var("spec")
    if not spec:
        raise MKUserError("spec", _("Missing spec parameter"))
    graph_specification = parse_raw_graph_specification(
        json.loads(request.get_str_input_mandatory("spec"))
    )

    render = request.var("render")
    if not render:
        raise MKUserError("render", _("Missing render parameter"))
    custom_graph_render_options = json.loads(request.get_str_input_mandatory("render"))

    host_service_graph_dashlet_cmk(
        graph_specification,
        custom_graph_render_options,
        resolve_combined_single_metric_spec,
        graph_display_id=request.get_str_input_mandatory("id"),
    )
