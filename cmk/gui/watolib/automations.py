#!/usr/bin/env python3
# Copyright (C) 2019 Checkmk GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.
"""This code section deals with the interaction of Checkmk base code. It is
used for doing inventory, showing the services of a host, deletion of a host
and similar things."""

from __future__ import annotations

import ast
import functools
import json
import os
import re
import subprocess
import time
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from typing import Final, NamedTuple

import requests
import urllib3
from pydantic import BaseModel, field_validator

from livestatus import sanitize_site_configuration, SiteConfiguration, SiteId

import cmk.ccc.version as cmk_version
from cmk.ccc import store  # Some braindead "unit" test monkeypatch this like hell :-/
from cmk.ccc.exceptions import MKGeneralException
from cmk.ccc.site import get_omd_config
from cmk.ccc.store import RealIo

from cmk.utils import paths
from cmk.utils.licensing.handler import LicenseState
from cmk.utils.licensing.registry import get_license_state
from cmk.utils.user import UserId

from cmk.automations.results import result_type_registry, SerializedResult

from cmk.gui import hooks
from cmk.gui.background_job import (
    BackgroundJob,
    BackgroundProcessInterface,
    BackgroundStatusSnapshot,
    InitialStatusArgs,
    JobStatusSpec,
    JobStatusStates,
    JobTarget,
)
from cmk.gui.config import active_config
from cmk.gui.exceptions import MKUserError
from cmk.gui.http import Request, request
from cmk.gui.i18n import _
from cmk.gui.log import logger
from cmk.gui.logged_in import user
from cmk.gui.site_config import get_site_config, is_replication_enabled
from cmk.gui.utils import escaping
from cmk.gui.utils.compatibility import (
    EditionsIncompatible,
    is_distributed_setup_compatible_for_licensing,
    LicenseStateIncompatible,
    LicensingCompatible,
    make_incompatible_info,
)
from cmk.gui.utils.urls import urlencode_vars
from cmk.gui.watolib.automation_commands import AutomationCommand
from cmk.gui.watolib.automation_types import PhaseOneResult
from cmk.gui.watolib.utils import mk_repr

from cmk import trace

from . import automation_helper, automation_subprocess
from .automation_executor import AutomationExecutor

auto_logger = logger.getChild("automations")
tracer = trace.get_tracer()

# Disable python warnings in background job output or logs like "Unverified
# HTTPS request is being made". We warn the user using analyze configuration.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

ENV_VARIABLE_FORCE_CLI_INTERFACE: Final[str] = "_CMK_AUTOMATIONS_FORCE_CLI_INTERFACE"

OMDStatus = Mapping[str, int]


class MKAutomationException(MKGeneralException):
    pass


def cmk_version_of_remote_automation_source(remote_request: Request) -> cmk_version.Version:
    # The header is sent by Checkmk as of 2.0.0p1. In case it is missing, assume we are too old.
    return cmk_version.Version.from_str(remote_request.headers.get("x-checkmk-version", "1.6.0p1"))


def check_mk_local_automation_serialized(
    *,
    command: str,
    args: Sequence[str] | None = None,
    indata: object = "",
    stdin_data: str | None = None,
    timeout: int | None = None,
    force_cli_interface: bool = False,
) -> tuple[Sequence[str], SerializedResult]:
    with tracer.span(
        f"local_automation[{command}]",
        attributes={"cmk.automation.args": repr(args)},
    ) as span:
        if args is None:
            args = []

        if stdin_data is None:
            stdin_data = repr(indata)

        if command in ["restart", "reload"]:
            call_hook_pre_activate_changes()

        executor: AutomationExecutor = (
            automation_subprocess.SubprocessExecutor()
            if force_cli_interface
            or os.environ.get(ENV_VARIABLE_FORCE_CLI_INTERFACE)
            or not _automation_helper_enabled_in_omd_config()
            else automation_helper.HelperExecutor()
        )

        try:
            result = executor.execute(command, args, stdin_data, auto_logger, timeout)
        except Exception as e:
            msg = get_local_automation_failure_message(
                command=command,
                cmdline=executor.command_description(command, args, logger, timeout),
                exc=e,
            )
            raise MKAutomationException(msg)

        span.set_attribute("cmk.automation.exit_code", result.exit_code)
        auto_logger.info("FINISHED: %d" % result.exit_code)
        auto_logger.debug("OUTPUT: %r" % result.output)

        if result.exit_code:
            auto_logger.error(
                "Error running %r (exit code %d)" % (result.command_description, result.exit_code)
            )
            msg = get_local_automation_failure_message(
                command=command,
                cmdline=result.command_description,
                code=result.exit_code,
                out=result.output,
                err=result.error,
            )

            if result.exit_code == 1:
                raise MKUserError(None, msg)

            raise MKAutomationException(msg)

        # On successful "restart" command execute the activate changes hook
        if command in ["restart", "reload"]:
            call_hook_activate_changes()

        return result.command_description, SerializedResult(result.output)


def get_local_automation_failure_message(
    *,
    command: str,
    cmdline: Iterable[str],
    code: int | None = None,
    out: str | None = None,
    err: str | None = None,
    exc: Exception | None = None,
) -> str:
    call = subprocess.list2cmdline(cmdline) if active_config.debug else command
    msg = "Error running automation call <tt>%s</tt>" % call
    if code:
        msg += " (exit code %d)" % code
    if out:
        msg += ", output: <pre>%s</pre>" % _hilite_errors(out)
    if err:
        msg += ", error: <pre>%s</pre>" % _hilite_errors(err)
    if exc:
        msg += ": %s" % exc
    return msg


def _hilite_errors(outdata: str) -> str:
    return re.sub("\nError: *([^\n]*)", "\n<div class=err><b>Error:</b> \\1</div>", outdata)


def check_mk_remote_automation_serialized(
    *,
    site_id: SiteId,
    command: str,
    args: Sequence[str] | None,
    indata: object,
    stdin_data: str | None = None,
    timeout: int | None = None,
    sync: Callable[[SiteId], None],
    non_blocking_http: bool = False,
) -> SerializedResult:
    with tracer.span(
        f"remote_automation[{command}]",
        attributes={
            "cmk.automation.target_site_id": str(site_id),
            "cmk.automation.args": repr(args),
        },
    ):
        site = get_site_config(active_config, site_id)
        if "secret" not in site:
            raise MKGeneralException(
                _('Cannot connect to site "%s": The site is not logged in')
                % site.get("alias", site_id)
            )

        if not is_replication_enabled(site):
            raise MKGeneralException(
                _('Cannot connect to site "%s": The replication is disabled')
                % site.get("alias", site_id)
            )

        sync(site_id)

        if non_blocking_http:
            # This will start a background job process on the remote site to execute the automation
            # asynchronously. It then polls the remote site, waiting for completion of the job.
            return _do_check_mk_remote_automation_in_background_job_serialized(
                site_id, CheckmkAutomationRequest(command, args, indata, stdin_data, timeout)
            )

        # Synchronous execution of the actual remote command in a single blocking HTTP request
        return SerializedResult(
            _do_remote_automation_serialized(
                site_id=site_id,
                site=get_site_config(active_config, site_id),
                command="checkmk-automation",
                vars_=[
                    ("automation", command),  # The Checkmk automation command
                    ("arguments", mk_repr(args).decode("ascii")),  # The arguments for the command
                    ("indata", mk_repr(indata).decode("ascii")),  # The input data
                    ("stdin_data", mk_repr(stdin_data).decode("ascii")),  # The input data for stdin
                    ("timeout", mk_repr(timeout).decode("ascii")),  # The timeout
                ],
                timeout=timeout,
            )
        )


# This hook is executed when one applies the pending configuration changes
# from wato but BEFORE the nagios restart is executed.
#
# It can be used to create custom input files for nagios/Checkmk.
#
# The registered hooks are called with a dictionary as parameter which
# holds all available with the hostnames as keys and the attributes of
# the hosts as values.
def call_hook_pre_activate_changes() -> None:
    if hooks.registered("pre-activate-changes"):
        # TODO: Cleanup this local import
        import cmk.gui.watolib.hosts_and_folders

        hooks.call("pre-activate-changes", cmk.gui.watolib.hosts_and_folders.collect_all_hosts())


# This hook is executed when one applies the pending configuration changes
# from wato.
#
# But it is only excecuted when there is at least one function
# registered for this host.
#
# The registered hooks are called with a dictionary as parameter which
# holds all available with the hostnames as keys and the attributes of
# the hosts as values.
def call_hook_activate_changes() -> None:
    if hooks.registered("activate-changes"):
        # TODO: Cleanup this local import
        import cmk.gui.watolib.hosts_and_folders

        hooks.call("activate-changes", cmk.gui.watolib.hosts_and_folders.collect_all_hosts())


def _do_remote_automation_serialized(
    *,
    site_id: SiteId | None,
    site: SiteConfiguration,
    command: str,
    vars_: Sequence[tuple[str, str]],
    files: Mapping[str, BytesIO] | None = None,
    timeout: float | None = None,
) -> str:
    auto_logger.info("RUN [%s]: %s", site_id or "site id not in config", command)
    auto_logger.debug("Site config: %r", sanitize_site_configuration(site))
    auto_logger.debug("VARS: %r", vars_)

    base_url = site["multisiteurl"]
    secret = site.get("secret")
    if not secret:
        raise MKAutomationException(_("You are not logged into the remote site."))

    url = base_url + "automation.py?" + urlencode_vars([("command", command)])

    post_data = dict(vars_)
    post_data.update(
        {
            "secret": secret,
            "debug": "1" if active_config.debug else "",
        }
    )

    response = get_url(
        url, site.get("insecure", False), data=post_data, files=files, timeout=timeout
    )

    auto_logger.debug("RESPONSE: %r", response)

    if not response:
        raise MKAutomationException(_("Empty output from remote site."))

    return response


def execute_phase1_result(site_id: SiteId, connection_id: str) -> PhaseOneResult | str:
    command_args = [
        ("request_format", "python"),
        (
            "request",
            repr({"action": "get_phase1_result", "kwargs": {"connection_id": connection_id}}),
        ),
    ]
    return ast.literal_eval(
        str(
            do_remote_automation(
                site=get_site_config(active_config, site_id),
                command="execute-dcd-command",
                vars_=command_args,
            )
        )
    )


def fetch_service_discovery_background_job_status(
    site_id: SiteId, hostname: str
) -> BackgroundStatusSnapshot:
    details = json.loads(
        str(
            do_remote_automation(
                site=get_site_config(active_config, site_id),
                command="service-discovery-job-snapshot",
                vars_=[("hostname", hostname)],
            )
        )
    )
    return BackgroundStatusSnapshot(
        job_id=details["job_id"],
        status=JobStatusSpec(**details["status"]),
        exists=details["exists"],
        is_active=details["is_active"],
        has_exception=details["has_exception"],
        acknowledged_by=details["acknowledged_by"],
        may_stop=details["may_stop"],
        may_delete=details["may_delete"],
    )


def do_remote_automation(
    site: SiteConfiguration,
    command: str,
    vars_: Sequence[tuple[str, str]],
    files: Mapping[str, BytesIO] | None = None,
    timeout: float | None = None,
) -> object:
    serialized_response = _do_remote_automation_serialized(
        # callsites currently disagree on whether it should be there.
        site_id=site.get("id"),
        site=site,
        command=command,
        vars_=vars_,
        files=files,
        timeout=timeout,
    )
    try:
        return ast.literal_eval(serialized_response)
    except SyntaxError:
        # The remote site will send non-Python data in case of an error.
        raise MKAutomationException(
            "%s: <pre>%s</pre>"
            % (
                _("Got invalid data"),
                serialized_response,
            )
        )


def get_url_raw(
    url: str,
    insecure: bool,
    auth: tuple[str, str] | None = None,
    data: Mapping[str, str] | None = None,
    files: Mapping[str, BytesIO] | None = None,
    timeout: float | None = None,
    add_headers: dict[str, str] | None = None,
) -> requests.Response:
    headers_ = {
        "x-checkmk-version": cmk_version.__version__,
        "x-checkmk-edition": cmk_version.edition(paths.omd_root).short,
        "x-checkmk-license-state": get_license_state().readable,
    }
    headers_.update(add_headers or {})

    try:
        response = requests.post(
            url,
            data=data,
            verify=not insecure,
            auth=auth,
            files=files,
            timeout=timeout,
            headers=headers_,
        )
    except (ConnectionError, requests.ConnectionError) as e:
        raise MKUserError(None, _("Could not connect to the remote site (%s)") % e)

    response.encoding = "utf-8"  # Always decode with utf-8

    if response.status_code == 401:
        raise MKUserError("_passwd", _("Authentication failed. Invalid login/password."))

    if response.status_code == 503 and "Site Not Started" in response.text:
        raise MKUserError(None, _("Site is not running"))

    if response.status_code != 200:
        raise MKUserError(None, _("HTTP Error - %d: %s") % (response.status_code, response.text))

    _verify_compatibility(response)

    return response


def _verify_compatibility(response: requests.Response) -> None:
    """Ensure we are compatible with the remote site

    In distributed setups the sync from a newer major version central site to an older central site
    is not allowed. Since 2.1.0 the remote site is performing most of the validations. But in case
    the newer site is the central site and the remote site is the older, e.g. 2.1.0 to 2.0.0, there
    is no validation logic on the remote site. To ensure the validation is also performed in this
    case, we execute the validation logic also in the central site when receiving the answer to the
    remote call before processing it's content.

    Since 2.0.0p13 the remote site answers with x-checkmk-version, x-checkmk-edition headers.
    """
    central_version = cmk_version.__version__
    central_edition_short = cmk_version.edition(paths.omd_root).short
    central_license_state = get_license_state()

    remote_version = response.headers.get("x-checkmk-version", "")
    remote_edition_short = response.headers.get("x-checkmk-edition", "")
    remote_license_state = parse_license_state(response.headers.get("x-checkmk-license-state", ""))

    if not remote_version or not remote_edition_short:
        return  # No validation

    if not isinstance(
        compatibility := compatible_with_central_site(
            central_version,
            central_edition_short,
            central_license_state,
            remote_version,
            remote_edition_short,
            remote_license_state,
        ),
        cmk_version.VersionsCompatible,
    ):
        raise MKGeneralException(
            make_incompatible_info(
                central_version,
                central_edition_short,
                central_license_state,
                remote_version,
                remote_edition_short,
                remote_license_state,
                compatibility,
            )
        )


def verify_request_compatibility(ignore_license_compatibility: bool) -> None:
    # - _version and _edition_short were added with 1.5.0p10 to the login call only
    # - x-checkmk-version and x-checkmk-edition were added with 2.0.0p1
    # Prefer the headers and fall back to the request variables for now.
    central_version = (
        request.headers["x-checkmk-version"]
        if "x-checkmk-version" in request.headers
        else request.get_ascii_input_mandatory("_version")
    )
    central_edition_short = (
        request.headers["x-checkmk-edition"]
        if "x-checkmk-edition" in request.headers
        else request.get_ascii_input_mandatory("_edition_short")
    )
    central_license_state = parse_license_state(request.headers.get("x-checkmk-license-state", ""))
    remote_version = cmk_version.__version__
    remote_edition_short = cmk_version.edition(paths.omd_root).short
    remote_license_state = get_license_state()

    compatibility = compatible_with_central_site(
        central_version,
        central_edition_short,
        central_license_state,
        remote_version,
        remote_edition_short,
        remote_license_state,
    )

    if ignore_license_compatibility and isinstance(compatibility, LicenseStateIncompatible):
        return

    if not isinstance(compatibility, cmk_version.VersionsCompatible):
        raise MKGeneralException(
            make_incompatible_info(
                central_version,
                central_edition_short,
                central_license_state,
                remote_version,
                remote_edition_short,
                remote_license_state,
                compatibility,
            )
        )


def parse_license_state(raw_license_state: str) -> LicenseState | None:
    try:
        return LicenseState[raw_license_state]
    except KeyError:
        return None


def get_url(
    url: str,
    insecure: bool,
    auth: tuple[str, str] | None = None,
    data: Mapping[str, str] | None = None,
    files: Mapping[str, BytesIO] | None = None,
    timeout: float | None = None,
) -> str:
    return get_url_raw(url, insecure, auth, data, files, timeout).text


def do_site_login(site: SiteConfiguration, name: UserId, password: str) -> str:
    if not name:
        raise MKUserError("_name", _("Please specify your administrator login on the remote site."))
    if not password:
        raise MKUserError("_passwd", _("Please specify your password."))

    # Trying basic auth AND form based auth to ensure the site login works.
    # Adding _ajaxid makes the web service fail silently with an HTTP code and
    # not output HTML code for an error screen.
    url = site["multisiteurl"] + "login.py"
    post_data = {
        "_login": "1",
        "_username": name,
        "_password": password,
        "_origtarget": f"automation_login.py?_version={cmk_version.__version__}&_edition_short={cmk_version.edition(paths.omd_root).short}",
        "_plain_error": "1",
    }
    response = get_url(
        url, site.get("insecure", False), auth=(name, password), data=post_data
    ).strip()
    if "<html>" in response.lower():
        message = _(
            "Authentication to web service failed.<br>Message:<br>%s"
        ) % escaping.strip_tags(escaping.strip_scripts(response))
        if active_config.debug:
            message += "<br>" + _("Automation URL:") + " <tt>%s</tt><br>" % url
        raise MKAutomationException(message)
    if not response:
        raise MKAutomationException(_("Empty response from web service"))
    try:
        eval_response = ast.literal_eval(response)
    except SyntaxError:
        raise MKAutomationException(response)
    if isinstance(eval_response, dict):
        if (
            cmk_version.edition(paths.omd_root) is cmk_version.Edition.CME
            and eval_response["edition_short"] != "cme"
        ):
            raise MKUserError(
                None,
                _(
                    "The Checkmk Managed Services Edition can only "
                    "be connected with other sites using the CME."
                ),
            )
        return eval_response["login_secret"]
    return eval_response


class CheckmkAutomationRequest(NamedTuple):
    command: str
    args: Sequence[str] | None
    indata: object
    stdin_data: str | None
    timeout: int | None


RemoteAutomationGetStatusResponseRaw = tuple[dict[str, object], str]


class CheckmkAutomationGetStatusResponse(NamedTuple):
    job_status: JobStatusSpec
    result: str


# There are already at least two custom background jobs that are wrapping remote automation
# calls but have been implemented individually. Does it make sense to refactor them to use this?
# - Service discovery of a single host (cmk.gui.wato.pages.services._get_check_table)
# - Fetch agent / SNMP output (cmk.gui.wato.pages.fetch_agent_output.FetchAgentOutputBackgroundJob)
def _do_check_mk_remote_automation_in_background_job_serialized(
    site_id: SiteId, automation_request: CheckmkAutomationRequest
) -> SerializedResult:
    """Execute the automation in a background job on the remote site

    It starts the background job using one call. It then polls the remote site, waiting for
    completion of the job."""
    site_config = get_site_config(active_config, site_id)

    job_id = _start_remote_automation_job(site_config, automation_request)

    auto_logger.info("Waiting for job completion")
    result = None
    while True:
        raw_response = do_remote_automation(
            site_config,
            "checkmk-remote-automation-get-status",
            [
                ("request", repr(job_id)),
            ],
        )
        assert isinstance(raw_response, tuple)
        response = CheckmkAutomationGetStatusResponse(
            JobStatusSpec.model_validate(raw_response[0]),
            raw_response[1],
        )
        auto_logger.debug("Job status: %r", response)

        if not response.job_status.is_active:
            if response.job_status.state == JobStatusStates.EXCEPTION:
                raise MKAutomationException("\n".join(response.job_status.loginfo["JobException"]))

            result = response.result
            auto_logger.debug("Job is not active anymore. Return the result: %s", result)
            break
        time.sleep(0.25)

    assert isinstance(result, str)

    return SerializedResult(result)


def _start_remote_automation_job(
    site_config: SiteConfiguration, automation_request: CheckmkAutomationRequest
) -> str:
    auto_logger.info("Starting remote automation in background job")
    job_id = str(
        do_remote_automation(
            site_config,
            "checkmk-remote-automation-start",
            [
                ("request", repr(tuple(automation_request))),
            ],
        )
    )

    auto_logger.info("Started background job: %s", job_id)
    return job_id


class AutomationCheckmkAutomationStart(AutomationCommand[CheckmkAutomationRequest]):
    """Called by do_remote_automation_in_background_job to execute the background job on a remote site"""

    def command_name(self) -> str:
        return "checkmk-remote-automation-start"

    def get_request(self) -> CheckmkAutomationRequest:
        return CheckmkAutomationRequest(
            *ast.literal_eval(request.get_ascii_input_mandatory("request"))
        )

    def execute(self, api_request: CheckmkAutomationRequest) -> str:
        automation_id = str(uuid.uuid4())
        job_id = f"{CheckmkAutomationBackgroundJob.job_prefix}{api_request.command}-{automation_id}"
        job = CheckmkAutomationBackgroundJob(job_id)
        if (
            result := job.start(
                JobTarget(
                    callable=checkmk_automation_job_entry_point,
                    args=CheckmkAutomationJobArgs(job_id=job_id, api_request=api_request),
                ),
                InitialStatusArgs(
                    title=_("Checkmk automation %s %s") % (api_request.command, automation_id),
                    user=str(user.id) if user.id else None,
                ),
            )
        ).is_error():
            raise result.error

        return job.get_job_id()


class CheckmkAutomationJobArgs(BaseModel, frozen=True):
    job_id: str
    api_request: CheckmkAutomationRequest


def checkmk_automation_job_entry_point(
    job_interface: BackgroundProcessInterface, args: CheckmkAutomationJobArgs
) -> None:
    job = CheckmkAutomationBackgroundJob(args.job_id)
    job.execute_automation(job_interface, args.api_request)


class AutomationCheckmkAutomationGetStatus(AutomationCommand[str]):
    """Called by do_remote_automation_in_background_job to get the background job state from on a
    remote site"""

    def command_name(self) -> str:
        return "checkmk-remote-automation-get-status"

    def get_request(self) -> str:
        return ast.literal_eval(request.get_ascii_input_mandatory("request"))

    @staticmethod
    def _load_result(path: Path) -> str:
        return store.load_text_from_file(path)

    def execute(self, api_request: str) -> RemoteAutomationGetStatusResponseRaw:
        job_id = api_request
        job = CheckmkAutomationBackgroundJob(job_id)
        response = CheckmkAutomationGetStatusResponse(
            job_status=job.get_status_snapshot().status,
            result=self._load_result(Path(job.get_work_dir()) / "result.mk"),
        )
        return dict(response[0]), response[1]


class CheckmkAutomationBackgroundJob(BackgroundJob):
    """The background job is always executed on the site where the host is located on"""

    job_prefix = "automation-"

    @classmethod
    def gui_title(cls) -> str:
        return _("Checkmk automation")

    @staticmethod
    def _store_result(
        *,
        path: Path,
        serialized_result: SerializedResult,
        automation_cmd: str,
        cmdline_cmd: Iterable[str],
    ) -> None:
        try:
            store.save_text_to_file(
                path,
                result_type_registry[automation_cmd]
                .deserialize(serialized_result)
                .serialize(cmk_version_of_remote_automation_source(request)),
            )
        except SyntaxError as e:
            msg = get_local_automation_failure_message(
                command=automation_cmd,
                cmdline=cmdline_cmd,
                out=serialized_result,
                exc=e,
            )
            raise MKAutomationException(msg)

    def execute_automation(
        self,
        job_interface: BackgroundProcessInterface,
        api_request: CheckmkAutomationRequest,
    ) -> None:
        with job_interface.gui_context():
            self._execute_automation(job_interface, api_request)

    def _execute_automation(
        self,
        job_interface: BackgroundProcessInterface,
        api_request: CheckmkAutomationRequest,
    ) -> None:
        self._logger.info("Starting automation: %s", api_request.command)
        self._logger.debug(api_request)
        cmdline_cmd, serialized_result = check_mk_local_automation_serialized(
            command=api_request.command,
            args=api_request.args,
            indata=api_request.indata,
            stdin_data=api_request.stdin_data,
            timeout=api_request.timeout,
        )
        # This file will be read by the get-status request
        self._store_result(
            path=Path(job_interface.get_work_dir()) / "result.mk",
            serialized_result=serialized_result,
            automation_cmd=api_request.command,
            cmdline_cmd=cmdline_cmd,
        )
        job_interface.send_result_message(_("Finished."))


def _edition_from_short(edition_short: str) -> cmk_version.Edition:
    for ed in cmk_version.Edition:
        if ed.short == edition_short:
            return ed
    raise ValueError(edition_short)


def compatible_with_central_site(
    central_version: str,
    central_edition_short: str,
    central_license_state: LicenseState | None,
    remote_version: str,
    remote_edition_short: str,
    remote_license_state: LicenseState | None,
) -> (
    cmk_version.VersionsCompatible
    | cmk_version.VersionsIncompatible
    | EditionsIncompatible
    | LicenseStateIncompatible
):
    """Whether or not a remote site version and edition is compatible with the central site

    The version check is handled by the version utils, the edition check is handled here.

    >>> c = compatible_with_central_site

    C*E != CME is not allowed

    >>> str(c("2.2.0p1", "cce", LicenseState.LICENSED, "2.2.0p1", "cce", LicenseState.FREE))
    'Remote site in license state free is not allowed'
    >>> str(c("2.0.0p3", "cee", LicenseState.LICENSED, "2.0.0p3", "cme", LicenseState.LICENSED))
    'Mix of CME and non-CME is not allowed.'
    >>> str(c("2.0.0p3", "cme", LicenseState.LICENSED, "2.0.0p3", "cee", LicenseState.LICENSED))
    'Mix of CME and non-CME is not allowed.'
    >>> str(c("2.0.0p3", "cre", LicenseState.LICENSED, "2.0.0p3", "cme", LicenseState.LICENSED))
    'Mix of CME and non-CME is not allowed.'
    >>> str(c("2.0.0p3", "cme", LicenseState.LICENSED, "2.0.0p3", "cre", LicenseState.LICENSED))
    'Mix of CME and non-CME is not allowed.'
    >>> isinstance(c("2.0.0p3", "cme", LicenseState.LICENSED, "2.0.0p3", "cme", LicenseState.LICENSED), cmk_version.VersionsCompatible)
    True
    """

    # Pre 2.0.0p1 did not sent x-checkmk-* headers -> Not compabile
    if not all(
        (
            central_edition_short,
            central_version,
            remote_edition_short,
            remote_version,
        )
    ):
        return cmk_version.VersionsIncompatible("Central or remote site are below 2.0.0p1.")

    if not isinstance(
        version_compatibility := cmk_version.versions_compatible(
            cmk_version.Version.from_str(central_version),
            cmk_version.Version.from_str(remote_version),
        ),
        cmk_version.VersionsCompatible,
    ):
        return version_compatibility

    if not isinstance(
        licensing_compatibility := is_distributed_setup_compatible_for_licensing(
            central_edition=_edition_from_short(central_edition_short),
            central_license_state=central_license_state,
            remote_edition=_edition_from_short(remote_edition_short),
            remote_license_state=remote_license_state,
        ),
        LicensingCompatible,
    ):
        return licensing_compatibility

    return cmk_version.VersionsCompatible()


class LastKnownCentralSiteVersion(BaseModel):
    """information about the central site

    this is currently only used to store the central site info. We need that info in order to
    communicate to the central site e.g. with the updater-registration (CEE freature). There we
    decide on the auth scheme based on that info.

    As of now this can go with 2.5 since then it is certain the central site supports the new
    scheme. In 2.4 we could encounter a 2.3 central site that does not support the new scheme."""

    version_str: str

    @property
    def cmk_version(self) -> cmk_version.Version:
        return cmk_version.Version.from_str(self.version_str)

    @field_validator("version_str")
    @classmethod
    def _validate_version(cls, v: str) -> str:
        # just check if it is parse able...
        cmk_version.Version.from_str(v)
        return v


class LastKnownCentralSiteVersionStore:
    def __init__(self) -> None:
        self._io: Final = RealIo(Path(paths.var_dir) / "last_known_site_version.json")

    @contextmanager
    def locked(self) -> Iterator[None]:
        yield from self._io.locked()

    def write_obj(self, obj: LastKnownCentralSiteVersion) -> None:
        return self._io.write(obj.model_dump_json().encode())

    def read_obj(self) -> LastKnownCentralSiteVersion | None:
        return (
            LastKnownCentralSiteVersion.model_validate_json(raw.decode())
            if (raw := self._io.read())
            else None
        )


@functools.cache
def _automation_helper_enabled_in_omd_config() -> bool:
    return get_omd_config(paths.omd_root)["CONFIG_AUTOMATION_HELPER"] == "on"


class AutomationGetRemoteOMDStatus(AutomationCommand[None]):
    """Called to get the status of OMD services on the remote site
    0: running, 1: stopped, 5: disabled
    """

    def command_name(self) -> str:
        return "get-remote-omd-status"

    def get_request(self) -> None:
        pass

    def _parse_omd_status(self, raw_status: str) -> OMDStatus:
        status = {key: int(val) for key, val in (el.split(" ") for el in raw_status.splitlines())}
        auto_logger.debug("OMD remote status: %s", status)
        return status

    def _get_omd_status(self) -> OMDStatus:
        result = subprocess.run(
            ["omd", "-v", "status", "--bare"],
            capture_output=True,
            text=True,
            check=False,
        )

        return self._parse_omd_status(result.stdout)

    def execute(self, api_request: None) -> OMDStatus:
        auto_logger.debug("Executing AutomationGetRemoteOMDStatus")
        return self._get_omd_status()
