#!/usr/bin/env python3
# Copyright (C) 2022 Checkmk GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.
from __future__ import annotations

import abc
import dataclasses
import datetime
import json
import multiprocessing
import pprint
import queue
import urllib.parse
from collections.abc import Mapping, Sequence
from typing import Any, cast, Literal, NoReturn

from typing_extensions import TypedDict

from cmk.utils import version

from cmk.gui.http import HTTPMethod
from cmk.gui.rest_api_types.notifications_rule_types import APINotificationRule
from cmk.gui.rest_api_types.site_connection import SiteConfig

JSON = int | str | bool | list[Any] | dict[str, Any] | None
JSON_HEADERS = {"Accept": "application/json", "Content-Type": "application/json"}
IF_MATCH_HEADER_OPTIONS = Literal["valid_etag", "invalid_etag", "star"] | None


API_DOMAIN = Literal[
    "licensing",
    "activation_run",
    "user_config",
    "host",
    "host_config",
    "folder_config",
    "aux_tag",
    "time_period",
    "rule",
    "ruleset",
    "host_tag_group",
    "password",
    "agent",
    "downtime",
    "host_group_config",
    "service_group_config",
    "contact_group_config",
    "site_connection",
    "notification_rule",
    "comment",
    "event_console",
    "audit_log",
    "bi_pack",
    "bi_aggregation",
    "bi_rule",
    "user_role",
]


def _only_set_keys(body: dict[str, Any | None]) -> dict[str, Any]:
    return {k: v for k, v in body.items() if v is not None}


def set_if_match_header(
    if_match: IF_MATCH_HEADER_OPTIONS,
) -> Mapping[str, str] | None:
    match if_match:
        case "star":
            return {"If-Match": "*"}
        case "invalid_etag":
            return {"If-Match": "asdf"}
        case _:
            return None


@dataclasses.dataclass(frozen=True)
class Response:
    status_code: int
    body: bytes | None
    headers: Mapping[str, str]

    def assert_status_code(self, status_code: int) -> Response:
        assert self.status_code == status_code
        return self

    @property
    def json(self) -> Any:
        assert self.body is not None
        return json.loads(self.body.decode("utf-8"))


class RestApiRequestException(Exception):
    def __init__(
        self,
        url: str,
        method: str,
        body: Any | None = None,
        headers: Mapping[str, str] | None = None,
        query_params: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(url, method, body, headers)
        self.url = url
        self.query_params = query_params
        self.method = method
        self.body = body
        self.headers = headers

    def __str__(self) -> str:
        return pprint.pformat(
            {
                "request": {
                    "method": self.method,
                    "url": self.url,
                    "query_params": self.query_params,
                    "body": self.body,
                    "headers": self.headers,
                },
            },
            compact=True,
        )


class RestApiException(Exception):
    def __init__(
        self,
        url: str,
        method: str,
        body: Any,
        headers: Mapping[str, str],
        response: Response,
        query_params: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(url, method, body, headers, response)
        self.url = url
        self.query_params = query_params
        self.method = method
        self.body = body
        self.headers = headers
        self.response = response

    def __str__(self) -> str:
        try:
            formatted_body = json.loads(cast(bytes, self.response.body))
        except (ValueError, TypeError):
            formatted_body = self.response.body

        return pprint.pformat(
            {
                "request": {
                    "method": self.method,
                    "url": self.url,
                    "query_params": self.query_params,
                    "body": self.body,
                    "headers": self.headers,
                },
                "response": {
                    "status": self.response.status_code,
                    "body": formatted_body,
                    "headers": self.response.headers,
                },
            },
            compact=True,
        )


def get_link(resp: dict, rel: str) -> Mapping:
    for link in resp.get("links", []):
        if link["rel"].startswith(rel):
            return link  # type: ignore[no-any-return]
    if "result" in resp:
        for link in resp["result"].get("links", []):
            if link["rel"].startswith(rel):
                return link  # type: ignore[no-any-return]
    for member in resp.get("members", {}).values():
        if member["memberType"] == "action":
            for link in member["links"]:
                if link["rel"].startswith(rel):
                    return link  # type: ignore[no-any-return]
    raise KeyError(f"{rel!r} not found")


def expand_rel(rel: str) -> str:
    if rel.startswith(".../"):
        rel = rel.replace(".../", "urn:org.restfulobjects:rels/")
    if rel.startswith("cmk/"):
        rel = rel.replace("cmk/", "urn:com.checkmk:rels/")
    return rel


class RequestHandler(abc.ABC):
    """A class representing a way to do HTTP Requests."""

    @abc.abstractmethod
    def set_credentials(self, username: str, password: str) -> None:
        ...

    @abc.abstractmethod
    def request(
        self,
        method: HTTPMethod,
        url: str,
        query_params: Mapping[str, Any] | None = None,
        body: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Response:
        ...


# types used in RestApiClient
class TimeRange(TypedDict):
    start: str
    end: str


class RuleProperties(TypedDict, total=False):
    description: str
    comment: str
    documentation_url: str
    disabled: bool


def default_rule_properties() -> RuleProperties:
    return {"disabled": False}


class StringMatcher(TypedDict):
    match_on: list[str]
    operator: Literal["one_of", "none_of"]


class HostTagMatcher(TypedDict):
    key: str
    operator: Literal["is", "is_not", "none_of", "one_if"]
    value: str


class LabelMatcher(TypedDict):
    key: str
    operator: Literal["is", "is_not"]
    value: str


class RuleConditions(TypedDict, total=False):
    host_name: StringMatcher
    host_tags: list[HostTagMatcher]
    host_labels: list[LabelMatcher]
    service_labels: list[LabelMatcher]
    service_description: StringMatcher


class RestApiClient:
    """API Client for the REST API.

    This class offers convenient methods for accessing the REST API.
    Not that this is (as of now) not intended to be able to handle all endpoints the REST API provides,
    instead it makes assumptions in the name of usability that hold true for almost all the API.
    Also, as of now this is far away from being a complete wrapper for the API, so please add
    functions as you need them.

    The general pattern for adding functions for an endpoint is:
    * inline all path params as function parameters
    * inline the top level keys of the request body as function parameters
    * inline all query parameters as function parameters
    * add the following arg: `expect_ok: bool = True`
    * call and return `self.request()` with the following args:
      * `url` should be the url of the endpoint with all path parameters filled in
      * `body` should be a dict with all the keys you inlined in to the function signature
      * `query_params` should be a dict with all the query parameters you inlined into the function signature
      * `expect_ok` should be passed on from the function signature
    * if the endpoint needs an etag, get it and pass it as a header to `self.request()` (see `edit_host`)

    A good example to start from would be the `create_host` method of this class.

    Please feel free to shuffle or convert function arguments if you believe it will increase the usability of the client.
    """

    def __init__(self, request_handler: RequestHandler, url_prefix: str):
        self.request_handler = request_handler
        self._url_prefix = url_prefix

    def set_credentials(self, username: str, password: str) -> None:
        self.request_handler.set_credentials(username, password)

    # This is public for quick debugging sessions
    def request(
        self,
        method: HTTPMethod,
        url: str,
        body: JSON | None = None,
        query_params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        expect_ok: bool = True,
        follow_redirects: bool = True,
        url_is_complete: bool = False,
        use_default_headers: bool = True,
    ) -> Response:
        if use_default_headers:
            default_headers = JSON_HEADERS.copy()
            default_headers.update(headers or {})
        else:
            default_headers = cast(
                dict[str, str], headers
            )  # TODO FIX this. Need this to test exceptions

        if not url_is_complete:
            url = self._url_prefix + url

        if body is not None:
            request_body = json.dumps(body)
        else:
            request_body = ""

        resp = self.request_handler.request(
            method=method,
            url=url,
            query_params=query_params,
            body=request_body,
            headers=default_headers,
        )

        if expect_ok and resp.status_code >= 400:
            raise RestApiException(
                url, method, body, default_headers, resp, query_params=query_params
            )
        if follow_redirects and 300 <= resp.status_code < 400:
            return self.request(
                method=method,
                url=resp.headers["Location"],
                query_params=query_params,
                body=body,
                headers=default_headers,
                url_is_complete=url_is_complete,
            )
        return resp

    def follow_link(
        self,
        links: dict[str, Any],
        relation: str,
        extra_params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        expect_ok: bool = True,
    ) -> Response:
        params = extra_params or {}
        body = {}
        link = get_link(links, expand_rel(relation))
        if "body_params" in link and link["body_params"]:
            assert isinstance(link["body_params"], dict)  # for mypy
            body.update(link["body_params"])
        body.update(params)
        kwargs = {
            "method": link["method"],
            "url": link["href"],
            "body": body,
            "headers": headers,
        }
        if not body:
            del kwargs["body"]
        return self.request(**kwargs, url_is_complete=True, expect_ok=expect_ok)

    def get_graph(
        self,
        host_name: str,
        service_description: str,
        type_: Literal["single_metric", "graph"],
        time_range: TimeRange,
        graph_or_metric_id: str,
        site: str | None = None,
        expect_ok: bool = True,
    ) -> Response:
        body = {
            "host_name": host_name,
            "service_description": service_description,
            "type": type_,
            "time_range": time_range,
        }
        if type_ == "graph":
            body["graph_id"] = graph_or_metric_id
        if type_ == "single_metric":
            body["metric_id"] = graph_or_metric_id

        if site is not None:
            body["site"] = site

        return self.request(
            "post", url="/domain-types/metric/actions/get/invoke", body=body, expect_ok=expect_ok
        )


class LicensingClient(RestApiClient):
    domain: API_DOMAIN = "licensing"

    def call_online_verification(self, expect_ok: bool = False) -> Response:
        return self.request(
            "post",
            url="/domain-types/licensing/actions/verify/invoke",
            expect_ok=expect_ok,
        )

    def call_configure_licensing_settings(
        self, settings: Mapping[str, str | Mapping[str, str]], expect_ok: bool = False
    ) -> Response:
        body = {"settings": settings} if settings else {}
        return self.request(
            "put",
            url="/domain-types/licensing/actions/configure/invoke",
            body=body,
            expect_ok=expect_ok,
        )


class ActivateChangesClient(RestApiClient):
    domain: API_DOMAIN = "activation_run"

    def get_activation(self, activation_id: str, expect_ok: bool = True) -> Response:
        return self.request(
            "get",
            url=f"/objects/{self.domain}/{activation_id}",
            expect_ok=expect_ok,
        )

    def get_running_activations(self, expect_ok: bool = True) -> Response:
        return self.request(
            "get",
            url=f"/domain-types/{self.domain}/collections/running",
            expect_ok=expect_ok,
        )

    def activate_changes(
        self,
        sites: list[str] | None = None,
        redirect: bool = False,
        force_foreign_changes: bool = False,
        expect_ok: bool = True,
        etag: IF_MATCH_HEADER_OPTIONS = "star",
    ) -> Response:
        if sites is None:
            sites = []
        return self.request(
            "post",
            url=f"/domain-types/{self.domain}/actions/activate-changes/invoke",
            body={
                "redirect": redirect,
                "sites": sites,
                "force_foreign_changes": force_foreign_changes,
            },
            headers=self._set_etag_header(etag),
            expect_ok=expect_ok,
        )

    def call_activate_changes_and_wait_for_completion(
        self,
        sites: list[str] | None = None,
        force_foreign_changes: bool = False,
        timeout_seconds: int = 60,
        etag: IF_MATCH_HEADER_OPTIONS = "star",
    ) -> Response | NoReturn:
        if sites is None:
            sites = []
        response = self.request(
            "post",
            url=f"/domain-types/{self.domain}/actions/activate-changes/invoke",
            body={
                "redirect": True,
                "sites": sites,
                "force_foreign_changes": force_foreign_changes,
            },
            expect_ok=False,
            headers=self._set_etag_header(etag),
            follow_redirects=False,
        )

        if response.status_code != 302:
            return response

        que: multiprocessing.Queue[Response] = multiprocessing.Queue()

        def waiter(result_que: multiprocessing.Queue, initial_response: Response) -> None:
            wait_response = initial_response
            while wait_response.status_code == 302:
                wait_response = self.request(
                    "get",
                    url=wait_response.headers["Location"],
                    expect_ok=False,
                    url_is_complete=True,
                )
            result_que.put(wait_response)

        p = multiprocessing.Process(target=waiter, args=(que, response))
        p.start()
        try:
            result = que.get(timeout=timeout_seconds)
        except queue.Empty:
            raise TimeoutError
        finally:
            p.kill()
            p.join()

        return result

    def list_pending_changes(self, expect_ok: bool = True) -> Response:
        return self.request(
            "get",
            url=f"/domain-types/{self.domain}/collections/pending_changes",
            expect_ok=expect_ok,
        )

    def _set_etag_header(self, etag: IF_MATCH_HEADER_OPTIONS) -> Mapping[str, str] | None:
        if etag == "valid_etag":
            return {"If-Match": self.list_pending_changes().headers["ETag"]}
        return set_if_match_header(etag)


class UserClient(RestApiClient):
    domain: API_DOMAIN = "user_config"

    def create(
        self,
        username: str,
        fullname: str,
        customer: str | None = None,
        authorized_sites: Sequence[str] | None = None,
        contactgroups: Sequence[str] | None = None,
        auth_option: dict[str, Any] | None = None,
        roles: list[str] | None = None,
        idle_timeout: dict[str, Any] | None = None,
        interface_options: dict[str, str] | None = None,
        disable_notifications: dict[str, Any] | None = None,
        disable_login: bool | None = None,
        pager_address: str | None = None,
        language: str | None = None,
        temperature_unit: str | None = None,
        contact_options: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
        expect_ok: bool = True,
    ) -> Response:
        if extra is None:
            extra = {}

        body: dict[str, Any] = {
            k: v
            for k, v in {
                **extra,
                "username": username,
                "fullname": fullname,
                "authorized_sites": authorized_sites,
                "contactgroups": contactgroups,
                "auth_option": auth_option,
                "roles": roles,
                "customer": customer,
                "idle_timeout": idle_timeout,
                "interface_options": interface_options,
                "disable_notifications": disable_notifications,
                "disable_login": disable_login,
                "pager_address": pager_address,
                "language": language,
                "temperature_unit": temperature_unit,
                "contact_options": contact_options,
            }.items()
            if v is not None
        }

        return self.request(
            "post",
            url=f"/domain-types/{self.domain}/collections/all",
            body=body,
            expect_ok=expect_ok,
        )

    def get(
        self, username: str | None = None, url: str | None = None, expect_ok: bool = True
    ) -> Response:
        url_is_complete = False
        actual_url = ""

        if username is not None:
            url_is_complete = False
            actual_url = f"/objects/{self.domain}/{username}"

        elif url is not None:
            url_is_complete = True
            actual_url = url

        else:
            raise ValueError("Must specify username or url parameter")

        return self.request(
            "get",
            url=actual_url,
            url_is_complete=url_is_complete,
            expect_ok=expect_ok,
        )

    def get_all(self, effective_attributes: bool = False, expect_ok: bool = True) -> Response:
        return self.request(
            "get",
            url=f"/domain-types/{self.domain}/collections/all",
            query_params={"effective_attributes": "true" if effective_attributes else "false"},
            expect_ok=expect_ok,
        )

    def edit(
        self,
        username: str,
        fullname: str | None = None,
        customer: str = "provider",
        contactgroups: list[str] | None = None,
        authorized_sites: Sequence[str] | None = None,
        idle_timeout: dict[str, Any] | None = None,
        interface_options: dict[str, str] | None = None,
        auth_option: dict[str, Any] | None = None,
        disable_notifications: dict[str, bool] | None = None,
        disable_login: bool | None = None,
        contact_options: dict[str, Any] | None = None,
        pager_address: str | None = None,
        extra: dict[str, Any] | None = None,
        roles: list[str] | None = None,
        expect_ok: bool = True,
        etag: IF_MATCH_HEADER_OPTIONS = "star",
    ) -> Response:
        if extra is None:
            extra = {}

        body: dict[str, Any] = {
            k: v
            for k, v in {
                **extra,
                "fullname": fullname,
                "contactgroups": contactgroups,
                "authorized_sites": authorized_sites,
                "idle_timeout": idle_timeout,
                "customer": customer,
                "roles": roles,
                "interface_options": interface_options,
                "auth_option": auth_option,
                "disable_notifications": disable_notifications,
                "contact_options": contact_options,
                "disable_login": disable_login,
                "pager_address": pager_address,
            }.items()
            if v is not None
        }

        return self.request(
            "put",
            url=f"/objects/{self.domain}/{username}",
            body=body,
            headers=self._set_etag_header(username, etag),
            expect_ok=expect_ok,
        )

    def delete(
        self,
        username: str,
        expect_ok: bool = True,
        etag: IF_MATCH_HEADER_OPTIONS = "star",
    ) -> Response:
        return self.request(
            "delete",
            url=f"/objects/{self.domain}/{username}",
            expect_ok=expect_ok,
            headers=self._set_etag_header(username, etag),
        )

    def _set_etag_header(
        self, username: str, etag: IF_MATCH_HEADER_OPTIONS
    ) -> Mapping[str, str] | None:
        if etag == "valid_etag":
            return {"If-Match": self.get(username).headers["ETag"]}
        return set_if_match_header(etag)


class HostConfigClient(RestApiClient):
    domain: API_DOMAIN = "host_config"

    def get(
        self, host_name: str, effective_attributes: bool = False, expect_ok: bool = True
    ) -> Response:
        return self.request(
            "get",
            url=f"/objects/host_config/{host_name}",
            query_params={"effective_attributes": "true" if effective_attributes else "false"},
            expect_ok=expect_ok,
        )

    def get_all(self, effective_attributes: bool = False, expect_ok: bool = True) -> Response:
        return self.request(
            "get",
            url=f"/domain-types/{self.domain}/collections/all",
            query_params={"effective_attributes": "true" if effective_attributes else "false"},
            expect_ok=expect_ok,
        )

    def create(
        self,
        host_name: str,
        folder: str = "/",
        attributes: Mapping[str, Any] | Any | None = None,
        bake_agent: bool | None = None,
        expect_ok: bool = True,
    ) -> Response:
        if bake_agent is not None:
            query_params = {"bake_agent": "1" if bake_agent else "0"}
        else:
            query_params = {}
        return self.request(
            "post",
            url=f"/domain-types/{self.domain}/collections/all",
            query_params=query_params,
            body={"host_name": host_name, "folder": folder, "attributes": attributes or {}},
            expect_ok=expect_ok,
        )

    def bulk_create(
        self,
        entries: list[dict[str, Any]],
        bake_agent: Literal["0", "1"] | None = None,
        expect_ok: bool = True,
    ) -> Response:
        url = f"/domain-types/{self.domain}/actions/bulk-create/invoke"
        if bake_agent is not None:
            url += "?bake_agent=" + bake_agent

        return self.request(
            "post",
            url=url,
            body={"entries": entries},
            expect_ok=expect_ok,
        )

    def create_cluster(
        self,
        host_name: str,
        folder: str = "/",
        nodes: list[str] | None = None,
        attributes: Mapping[str, Any] | None = None,
        bake_agent: bool | None = None,
        expect_ok: bool = True,
    ) -> Response:
        if bake_agent is not None:
            query_params = {"bake_agent": "1" if bake_agent else "0"}
        else:
            query_params = {}
        return self.request(
            "post",
            url=f"/domain-types/{self.domain}/collections/clusters",
            query_params=query_params,
            body={
                "host_name": host_name,
                "folder": folder,
                "nodes": nodes or [],
                "attributes": attributes or {},
            },
            expect_ok=expect_ok,
        )

    def edit(
        self,
        host_name: str,
        attributes: Mapping[str, Any] | None = None,
        update_attributes: Mapping[str, Any] | None = None,
        remove_attributes: Sequence[str] | None = None,
        expect_ok: bool = True,
        etag: IF_MATCH_HEADER_OPTIONS = "star",
    ) -> Response:
        body: dict[str, Any] = {}

        if attributes is not None:
            body["attributes"] = attributes

        if remove_attributes is not None:
            body["remove_attributes"] = remove_attributes

        if update_attributes is not None:
            body["update_attributes"] = update_attributes

        return self.request(
            "put",
            url=f"/objects/{self.domain}/" + host_name,
            body=body,
            expect_ok=expect_ok,
            headers=self._set_etag_header(host_name, etag),
        )

    def bulk_edit(
        self,
        entries: list[dict[str, Any]],
        expect_ok: bool = True,
    ) -> Response:
        return self.request(
            "put",
            url=f"/domain-types/{self.domain}/actions/bulk-update/invoke",
            body={"entries": entries},
            expect_ok=expect_ok,
        )

    def bulk_delete(
        self,
        entries: list[str],
        expect_ok: bool = True,
    ) -> Response:
        return self.request(
            "post",
            url=f"/domain-types/{self.domain}/actions/bulk-delete/invoke",
            body={"entries": entries},
            expect_ok=expect_ok,
        )

    def edit_property(
        self,
        host_name: str,
        property_name: str,
        property_value: Any,
        expect_ok: bool = True,
        etag: IF_MATCH_HEADER_OPTIONS = "star",
    ) -> Response:
        return self.request(
            "put",
            url=f"/objects/{self.domain}/{host_name}/properties/{property_name}",
            body=property_value,
            headers=self._set_etag_header(host_name, etag),
            expect_ok=expect_ok,
        )

    def delete(self, host_name: str) -> Response:
        return self.request(
            "delete",
            url=f"/objects/{self.domain}/{host_name}",
        )

    def move(
        self,
        host_name: str,
        target_folder: str,
        expect_ok: bool = True,
        etag: IF_MATCH_HEADER_OPTIONS = "star",
    ) -> Response:
        return self.request(
            "post",
            url=f"/objects/{self.domain}/{host_name}/actions/move/invoke",
            body={"target_folder": target_folder},
            expect_ok=expect_ok,
            headers=self._set_etag_header(host_name, etag),
        )

    def rename(
        self,
        host_name: str,
        new_name: str,
        expect_ok: bool = True,
        follow_redirects: bool = True,
        etag: IF_MATCH_HEADER_OPTIONS = "star",
    ) -> Response:
        return self.request(
            "put",
            url=f"/objects/{self.domain}/{host_name}/actions/rename/invoke",
            body={"new_name": new_name},
            expect_ok=expect_ok,
            follow_redirects=follow_redirects,
            headers=self._set_etag_header(host_name, etag),
        )

    def rename_wait_for_completion(self, expect_ok: bool = True) -> Response:
        return self.request(
            "post",
            url=f"/domain-types/{self.domain}/actions/wait-for-completion/invoke",
            expect_ok=expect_ok,
        )

    def _set_etag_header(
        self, host_name: str, etag: IF_MATCH_HEADER_OPTIONS
    ) -> Mapping[str, str] | None:
        if etag == "valid_etag":
            return {"If-Match": self.get(host_name=host_name).headers["ETag"]}
        return set_if_match_header(etag)


class FolderClient(RestApiClient):
    domain: API_DOMAIN = "folder_config"

    def get(self, folder_name: str, expect_ok: bool = True) -> Response:
        return self.request(
            "get",
            url=f"/objects/{self.domain}/{folder_name}",
            expect_ok=expect_ok,
        )

    def get_all(
        self,
        parent: str | None = None,
        expect_ok: bool = True,
        recursive: bool = False,
        show_hosts: bool = False,
    ) -> Response:
        query_params: dict[str, Any] = {"recursive": recursive, "show_hosts": show_hosts}
        if parent:
            query_params.update({"parent": parent})

        return self.request(
            "get",
            url=f"/domain-types/{self.domain}/collections/all",
            query_params=query_params,
            expect_ok=expect_ok,
        )

    def create(
        self,
        title: str,
        parent: str,
        folder_name: str | None = None,
        attributes: Mapping[str, Any] | None = None,
        expect_ok: bool = True,
    ) -> Response:
        body = {
            "title": title,
            "parent": parent,
            "attributes": attributes or {},
        }
        if folder_name is not None:
            body["name"] = folder_name

        return self.request(
            "post",
            url=f"/domain-types/{self.domain}/collections/all",
            body=body,
            expect_ok=expect_ok,
        )

    def bulk_edit(
        self,
        entries: list[dict[str, Any]],
        expect_ok: bool = True,
    ) -> Response:
        return self.request(
            "put",
            url=f"/domain-types/{self.domain}/actions/bulk-update/invoke",
            body={"entries": entries},
            expect_ok=expect_ok,
        )

    def edit(
        self,
        folder_name: str,
        title: str | None = None,
        attributes: Mapping[str, Any] | None = None,
        update_attributes: Mapping[str, Any] | None = None,
        remove_attributes: list[str] | None = None,
        expect_ok: bool = True,
        etag: IF_MATCH_HEADER_OPTIONS = "star",
    ) -> Response:
        body: dict[str, Any] = {"title": title} if title is not None else {}

        if attributes is not None:
            body["attributes"] = attributes

        if remove_attributes is not None:
            body["remove_attributes"] = remove_attributes

        if update_attributes is not None:
            body["update_attributes"] = update_attributes

        return self.request(
            "put",
            url=f"/objects/{self.domain}/{folder_name}",
            headers=self._set_etag_header(folder_name, etag),
            body=body,
            expect_ok=expect_ok,
        )

    def move(
        self,
        folder_name: str,
        destination: str,
        expect_ok: bool = True,
        etag: IF_MATCH_HEADER_OPTIONS = "star",
    ) -> Response:
        return self.request(
            "post",
            url=f"/objects/{self.domain}/{folder_name}/actions/move/invoke",
            body={"destination": destination},
            expect_ok=expect_ok,
            headers=self._set_etag_header(folder_name, etag),
        )

    def delete(self, folder_name: str) -> Response:
        return self.request(
            "delete",
            url=f"/objects/{self.domain}/{folder_name}",
        )

    def _set_etag_header(
        self, folder_name: str, etag: IF_MATCH_HEADER_OPTIONS
    ) -> Mapping[str, str] | None:
        if etag == "valid_etag":
            return {"If-Match": self.get(folder_name=folder_name).headers["ETag"]}
        return set_if_match_header(etag)


class AuxTagClient(RestApiClient):
    domain: API_DOMAIN = "aux_tag"

    def get(self, aux_tag_id: str, expect_ok: bool = True) -> Response:
        return self.request(
            "get",
            url=f"/objects/{self.domain}/{aux_tag_id}",
            expect_ok=expect_ok,
        )

    def get_all(self, expect_ok: bool = True) -> Response:
        return self.request(
            "get",
            url=f"/domain-types/{self.domain}/collections/all",
            expect_ok=expect_ok,
        )

    def create(self, tag_data: dict[str, Any], expect_ok: bool = True) -> Response:
        return self.request(
            "post",
            url=f"/domain-types/{self.domain}/collections/all",
            body=tag_data,
            expect_ok=expect_ok,
        )

    def edit(
        self,
        aux_tag_id: str,
        tag_data: dict[str, Any],
        expect_ok: bool = True,
        with_etag: bool = True,
    ) -> Response:
        headers = None
        if with_etag:
            headers = {
                "If-Match": self.get(aux_tag_id).headers["ETag"],
                "Accept": "application/json",
            }
        return self.request(
            "put",
            url=f"/objects/{self.domain}/{aux_tag_id}",
            body=tag_data,
            headers=headers,
            expect_ok=expect_ok,
        )

    def delete(self, aux_tag_id: str, expect_ok: bool = True) -> Response:
        etag = self.get(aux_tag_id).headers["ETag"]
        return self.request(
            "post",
            url=f"/objects/{self.domain}/{aux_tag_id}/actions/delete/invoke",
            headers={"If-Match": etag, "Accept": "application/json"},
            expect_ok=expect_ok,
        )


class TimePeriodClient(RestApiClient):
    domain: API_DOMAIN = "time_period"

    def get(self, time_period_id: str, expect_ok: bool = True) -> Response:
        return self.request(
            "get",
            url=f"/objects/{self.domain}/{time_period_id}",
            expect_ok=expect_ok,
        )

    def get_all(self, expect_ok: bool = True) -> Response:
        return self.request(
            "get",
            url=f"/domain-types/{self.domain}/collections/all",
            expect_ok=expect_ok,
        )

    def delete(self, time_period_id: str, expect_ok: bool = True) -> Response:
        etag = self.get(time_period_id).headers["ETag"]
        return self.request(
            "delete",
            url=f"/objects/{self.domain}/{time_period_id}",
            headers={"If-Match": etag, "Accept": "application/json"},
            expect_ok=expect_ok,
        )

    def create(self, time_period_data: dict[str, object], expect_ok: bool = True) -> Response:
        return self.request(
            "post",
            url=f"/domain-types/{self.domain}/collections/all",
            body=time_period_data,
            expect_ok=expect_ok,
        )

    def edit(
        self, time_period_id: str, time_period_data: dict[str, object], expect_ok: bool = True
    ) -> Response:
        etag = self.get(time_period_id).headers["ETag"]
        return self.request(
            "put",
            url=f"/objects/{self.domain}/{time_period_id}",
            body=time_period_data,
            expect_ok=expect_ok,
            headers={"If-Match": etag, "Accept": "application/json"},
        )


class RuleClient(RestApiClient):
    domain: API_DOMAIN = "rule"

    def get(self, rule_id: str, expect_ok: bool = True) -> Response:
        return self.request(
            "get",
            url=f"/objects/{self.domain}/{rule_id}",
            expect_ok=expect_ok,
        )

    def list(self, ruleset: str, expect_ok: bool = True) -> Response:
        url = f"/domain-types/{self.domain}/collections/all"
        if ruleset:
            url = f"/domain-types/{self.domain}/collections/all?ruleset_name={ruleset}"

        return self.request(
            "get",
            url=url,
            expect_ok=expect_ok,
        )

    def delete(self, rule_id: str, expect_ok: bool = True) -> Response:
        etag = self.get(rule_id).headers["ETag"]
        resp = self.request(
            "delete",
            url=f"/objects/{self.domain}/{rule_id}",
            headers={"If-Match": etag, "Accept": "application/json"},
        )
        if expect_ok:
            resp.assert_status_code(204)
        return resp

    def create(
        self,
        ruleset: str,
        conditions: RuleConditions,
        folder: str = "~",
        value_raw: str | None = None,
        properties: RuleProperties | None = None,
        expect_ok: bool = True,
    ) -> Response:
        body = _only_set_keys(
            {
                "ruleset": ruleset,
                "folder": folder,
                "properties": properties if properties is not None else {},
                "value_raw": value_raw,
                "conditions": conditions,
            }
        )

        return self.request(
            "post",
            url=f"/domain-types/{self.domain}/collections/all",
            body=body,
            expect_ok=expect_ok,
        )

    def move(self, rule_id: str, options: dict[str, Any], expect_ok: bool = True) -> Response:
        return self.request(
            "post",
            url=f"/objects/{self.domain}/{rule_id}/actions/move/invoke",
            body=options,
            expect_ok=expect_ok,
        )

    def edit(
        self,
        rule_id: str,
        value_raw: str | None = None,
        conditions: RuleConditions | None = None,
        properties: RuleProperties | None = None,
        expect_ok: bool = True,
    ) -> Response:
        body = _only_set_keys(
            {
                "properties": properties if properties is not None else {},
                "value_raw": value_raw,
                "conditions": conditions,
            }
        )

        return self.request(
            "put",
            url=f"/objects/{self.domain}/{rule_id}",
            body=body,
            expect_ok=expect_ok,
        )


class RulesetClient(RestApiClient):
    domain: API_DOMAIN = "ruleset"

    def get(self, ruleset_id: str, expect_ok: bool = True) -> Response:
        return self.request(
            "get",
            url=f"/objects/{self.domain}/{ruleset_id}",
            expect_ok=expect_ok,
        )

    def list(
        self,
        fulltext: str | None = None,
        folder: str | None = None,
        deprecated: bool | None = None,
        used: bool | None = None,
        group: str | None = None,
        name: str | None = None,
        search_options: str | None = None,
        expect_ok: bool = True,
    ) -> Response:
        url = f"/domain-types/{self.domain}/collections/all"
        if search_options is not None:
            url = f"/domain-types/{self.domain}/collections/all{search_options}"
        else:
            query_params = urllib.parse.urlencode(
                _only_set_keys(
                    {
                        "fulltext": fulltext,
                        "folder": folder,
                        "deprecated": deprecated,
                        "used": used,
                        "group": group,
                        "name": name,
                    }
                )
            )
            url = f"/domain-types/{self.domain}/collections/all?" + query_params
        return self.request("get", url=url, expect_ok=expect_ok)


class HostTagGroupClient(RestApiClient):
    domain: API_DOMAIN = "host_tag_group"

    def create(
        self,
        ident: str,
        title: str,
        tags: list[dict[str, str | list[str]]],
        topic: str | None = None,
        help_text: str | None = None,
        expect_ok: bool = True,
    ) -> Response:
        body = {"ident": ident, "title": title, "tags": tags}
        if help_text is not None:
            body["help"] = help_text
        if topic is not None:
            body["topic"] = topic

        return self.request(
            "post",
            url=f"/domain-types/{self.domain}/collections/all",
            body=body,
            expect_ok=expect_ok,
        )

    def get(self, ident: str, expect_ok: bool = True) -> Response:
        return self.request(
            "get",
            url=f"/objects/{self.domain}/{ident}",
            expect_ok=expect_ok,
        )

    def delete(self, ident: str, repair: bool = False, expect_ok: bool = True) -> Response:
        return self.request(
            "delete",
            url=f"/objects/{self.domain}/{ident}?repair={repair}",
            expect_ok=expect_ok,
        )

    def edit(
        self,
        ident: str,
        title: str | None = None,
        help_text: str | None = None,
        tags: list[dict[str, str]] | None = None,
        expect_ok: bool = True,
    ) -> Response:
        etag = self.get(ident).headers["ETag"]
        body: dict[str, Any] = {"ident": ident}
        if title is not None:
            body["title"] = title
        if help_text is not None:
            body["help"] = help_text
        if tags is not None:
            body["tags"] = tags
        return self.request(
            "put",
            url=f"/objects/{self.domain}/{ident}",
            body=body,
            expect_ok=expect_ok,
            headers={"If-Match": etag, "Accept": "application/json"},
        )


class PasswordClient(RestApiClient):
    domain: API_DOMAIN = "password"

    def create(
        self,
        ident: str,
        title: str,
        owner: str,
        password: str,
        shared: Sequence[str],
        customer: str | None = None,
        expect_ok: bool = True,
    ) -> Response:
        body = {
            "ident": ident,
            "title": title,
            "owner": owner,
            "password": password,
            "shared": shared,
            "customer": "provider" if customer is None else customer,
        }
        return self.request(
            "post",
            url=f"/domain-types/{self.domain}/collections/all",
            body=body,
            expect_ok=expect_ok,
        )

    def get(self, ident: str, expect_ok: bool = True) -> Response:
        return self.request(
            "get",
            url=f"/objects/{self.domain}/{ident}",
            expect_ok=expect_ok,
        )

    def edit(
        self,
        ident: str,
        title: str,
        owner: str,
        password: str,
        shared: Sequence[str],
        customer: str | None = None,
        expect_ok: bool = True,
    ) -> Response:
        body = {
            "title": title,
            "owner": owner,
            "password": password,
            "shared": shared,
            "customer": "provider" if customer is None else customer,
        }
        return self.request(
            "put",
            url=f"/objects/{self.domain}/{ident}",
            body=body,
            expect_ok=expect_ok,
        )


class AgentClient(RestApiClient):
    domain: API_DOMAIN = "agent"

    def bake(self, expect_ok: bool = True) -> Response:
        return self.request(
            "post",
            url=f"/domain-types/{self.domain}/actions/bake/invoke",
            expect_ok=expect_ok,
        )

    def bake_status(self, expect_ok: bool = True) -> Response:
        return self.request(
            "get",
            url=f"/domain-types/{self.domain}/actions/baking_status/invoke",
            expect_ok=expect_ok,
        )

    def bake_and_sign(self, key_id: int, passphrase: str, expect_ok: bool = True) -> Response:
        return self.request(
            "post",
            url=f"/domain-types/{self.domain}/actions/bake_and_sign/invoke",
            body={"key_id": key_id, "passphrase": passphrase},
            expect_ok=expect_ok,
        )


class DowntimeClient(RestApiClient):
    domain: API_DOMAIN = "downtime"

    def get(self, downtime_id: int, site_id: str, expect_ok: bool = True) -> Response:
        return self.request(
            "get",
            url=f"/objects/{self.domain}/{downtime_id}?site_id={site_id}",
            expect_ok=expect_ok,
        )

    def get_all(
        self,
        host_name: str | None = None,
        service_description: str | None = None,
        query: str | None = None,
        downtime_type: Literal["host", "service", "both"] = "both",
        site_id: str | None = None,
        expect_ok: bool = True,
    ) -> Response:
        query_params = urllib.parse.urlencode(
            _only_set_keys(
                {
                    "downtime_type": downtime_type,
                    "host_name": host_name,
                    "service_description": service_description,
                    "query": query,
                    "site_id": site_id,
                }
            )
        )
        return self.request(
            "get",
            url=f"/domain-types/{self.domain}/collections/all?{query_params}",
            expect_ok=expect_ok,
        )

    def create_for_host(
        self,
        start_time: datetime.datetime | str,
        end_time: datetime.datetime | str,
        recur: str | None = None,
        duration: int | None = None,
        comment: str | None = None,
        host_name: str | None = None,
        hostgroup_name: str | None = None,
        query: str | None = None,
        downtime_type: Literal["host", "hostgroup", "host_by_query"] = "host",
        expect_ok: bool = True,
    ) -> Response:
        body = {
            "downtime_type": downtime_type,
            "start_time": start_time if isinstance(start_time, str) else start_time.isoformat(),
            "end_time": end_time if isinstance(end_time, str) else end_time.isoformat(),
            "recur": recur,
            "comment": comment,
            "duration": duration,
        }

        if downtime_type == "host":
            body.update({"host_name": host_name})

        elif downtime_type == "hostgroup":
            body.update({"hostgroup_name": hostgroup_name})

        else:
            body.update({"query": query})

        return self.request(
            "post",
            url=f"/domain-types/{self.domain}/collections/host",
            body={k: v for k, v in body.items() if v is not None},
            expect_ok=expect_ok,
        )

    def create_for_services(
        self,
        start_time: datetime.datetime | str,
        end_time: datetime.datetime | str,
        recur: str | None = None,
        duration: int | None = None,
        comment: str | None = None,
        host_name: str | None = None,
        servicegroup_name: str | None = None,
        query: str | None = None,
        service_descriptions: list[str] | None = None,
        downtime_type: Literal["service", "servicegroup", "service_by_query"] = "service",
        expect_ok: bool = True,
    ) -> Response:
        body: dict[str, Any] = {
            "downtime_type": downtime_type,
            "start_time": start_time if isinstance(start_time, str) else start_time.isoformat(),
            "end_time": end_time if isinstance(end_time, str) else end_time.isoformat(),
            "recur": recur,
            "duration": duration,
            "comment": comment,
            "host_name": host_name,
        }

        if downtime_type == "service":
            body.update({"host_name": host_name, "service_descriptions": service_descriptions})

        elif downtime_type == "servicegroup":
            body.update({"servicegroup_name": servicegroup_name})

        else:
            body.update({"query": query})

        return self.request(
            "post",
            url=f"/domain-types/{self.domain}/collections/service",
            body={k: v for k, v in body.items() if v is not None},
            expect_ok=expect_ok,
        )

    def delete(
        self,
        delete_type: Literal["by_id", "query", "params"],
        site_id: str | None = None,
        downtime_id: str | None = None,
        query: str | None = None,
        host_name: str | None = None,
        service_descriptions: list[str] | None = None,
        expect_ok: bool = True,
    ) -> Response:
        body: dict[str, Any] = {
            "delete_type": delete_type,
        }

        if delete_type == "by_id":
            body.update({"downtime_id": downtime_id, "site_id": site_id})

        elif delete_type == "query":
            body.update({"query": query})

        else:
            body.update({"host_name": host_name, "service_descriptions": service_descriptions})

        return self.request(
            "post",
            url=f"/domain-types/{self.domain}/actions/delete/invoke",
            body={k: v for k, v in body.items() if v is not None},
            expect_ok=expect_ok,
        )


class GroupConfig(RestApiClient):
    domain: API_DOMAIN

    def get(self, group_id: str, expect_ok: bool = True) -> Response:
        return self.request(
            "get",
            url=f"/objects/{self.domain}/{group_id}",
            expect_ok=expect_ok,
        )

    def bulk_create(self, groups: tuple[dict[str, str], ...], expect_ok: bool = True) -> Response:
        return self.request(
            "post",
            f"/domain-types/{self.domain}/actions/bulk-create/invoke",
            body={"entries": groups},
            expect_ok=expect_ok,
        )

    def list(self, expect_ok: bool = True) -> Response:
        return self.request(
            "get",
            f"/domain-types/{self.domain}/collections/all",
            expect_ok=expect_ok,
        )

    def bulk_edit(self, groups: tuple[dict[str, str], ...], expect_ok: bool = True) -> Response:
        return self.request(
            "put",
            f"/domain-types/{self.domain}/actions/bulk-update/invoke",
            body={"entries": groups},
            expect_ok=expect_ok,
        )

    def create(
        self,
        name: str,
        alias: str,
        customer: str = "provider",
        expect_ok: bool = True,
    ) -> Response:
        body = {"name": name, "alias": alias}
        if version.edition() is version.Edition.CME:
            body.update({"customer": customer})

        return self.request(
            "post",
            url=f"/domain-types/{self.domain}/collections/all",
            body=body,
            expect_ok=expect_ok,
        )


class HostGroupClient(GroupConfig):
    domain: Literal["host_group_config"] = "host_group_config"


class ServiceGroupClient(GroupConfig):
    domain: Literal["service_group_config"] = "service_group_config"


class ContactGroupClient(GroupConfig):
    domain: Literal["contact_group_config"] = "contact_group_config"


class SiteManagementClient(RestApiClient):
    domain: API_DOMAIN = "site_connection"

    def get(self, site_id: str, expect_ok: bool = True) -> Response:
        return self.request(
            "get",
            url=f"/objects/{self.domain}/{site_id}",
            expect_ok=expect_ok,
        )

    def get_all(self, expect_ok: bool = True) -> Response:
        return self.request(
            "get",
            url=f"/domain-types/{self.domain}/collections/all",
            expect_ok=expect_ok,
        )

    def login(self, site_id: str, username: str, password: str, expect_ok: bool = True) -> Response:
        return self.request(
            "post",
            url=f"/objects/{self.domain}/{site_id}/actions/login/invoke",
            body={"username": username, "password": password},
            expect_ok=expect_ok,
        )

    def logout(self, site_id: str, expect_ok: bool = True) -> Response:
        return self.request(
            "post",
            url=f"/objects/{self.domain}/{site_id}/actions/logout/invoke",
            expect_ok=expect_ok,
        )

    def create(self, site_config: SiteConfig, expect_ok: bool = True) -> Response:
        return self.request(
            "post",
            url=f"/domain-types/{self.domain}/collections/all",
            body={"site_config": site_config},
            expect_ok=expect_ok,
        )

    def update(self, site_id: str, site_config: SiteConfig, expect_ok: bool = True) -> Response:
        return self.request(
            "put",
            url=f"/objects/{self.domain}/{site_id}",
            body={"site_config": site_config},
            expect_ok=expect_ok,
        )

    def delete(self, site_id: str, expect_ok: bool = True) -> Response:
        return self.request(
            "post",
            url=f"/objects/{self.domain}/{site_id}/actions/delete/invoke",
            expect_ok=expect_ok,
        )


class HostClient(RestApiClient):
    domain: Literal["host"] = "host"

    def get(self, host_name: str, columns: Sequence[str], expect_ok: bool = True) -> Response:
        url = f"/objects/host/{host_name}"
        if columns:
            url = f"{url}?{'&'.join(f'columns={c}' for c in columns)}"

        return self.request(
            "get",
            url=url,
            expect_ok=expect_ok,
        )

    def get_all(
        self,
        query: dict[str, Any],
        columns: Sequence[str] = ("name",),
        expect_ok: bool = True,
    ) -> Response:
        params = {"query": json.dumps(query), "columns": columns}
        return self.request(
            "get",
            url="/domain-types/host/collections/all",
            query_params=params,
            expect_ok=expect_ok,
        )


class RuleNotificationClient(RestApiClient):
    domain: API_DOMAIN = "notification_rule"

    def get(self, rule_id: str, expect_ok: bool = True) -> Response:
        return self.request(
            "get",
            url=f"/objects/{self.domain}/{rule_id}",
            expect_ok=expect_ok,
        )

    def get_all(self, expect_ok: bool = True) -> Response:
        return self.request(
            "get",
            url=f"/domain-types/{self.domain}/collections/all",
            expect_ok=expect_ok,
        )

    def create(self, rule_config: APINotificationRule, expect_ok: bool = True) -> Response:
        return self.request(
            "post",
            url=f"/domain-types/{self.domain}/collections/all",
            body={"rule_config": rule_config},
            expect_ok=expect_ok,
        )

    def edit(
        self, rule_id: str, rule_config: APINotificationRule, expect_ok: bool = True
    ) -> Response:
        return self.request(
            "put",
            url=f"/objects/{self.domain}/{rule_id}",
            body={"rule_config": rule_config},
            expect_ok=expect_ok,
        )

    def delete(self, rule_id: str, expect_ok: bool = True) -> Response:
        return self.request(
            "post",
            url=f"/objects/{self.domain}/{rule_id}/actions/delete/invoke",
            expect_ok=expect_ok,
        )


class EventConsoleClient(RestApiClient):
    domain: API_DOMAIN = "event_console"

    def get(
        self,
        event_id: str,
        site_id: str,
        expect_ok: bool = True,
    ) -> Response:
        return self.request(
            "get",
            url=f"/objects/{self.domain}/{event_id}?site_id={site_id}",
            expect_ok=expect_ok,
        )

    def get_all(
        self,
        query: str | None = None,
        host: str | None = None,
        application: str | None = None,
        state: Literal["warning", "ok", "critical", "unknown"] | None = None,
        phase: Literal["open", "ack"] | None = None,
        site_id: str | None = None,
        expect_ok: bool = True,
    ) -> Response:
        query_params = urllib.parse.urlencode(
            _only_set_keys(
                {
                    "query": query,
                    "host": host,
                    "application": application,
                    "state": state,
                    "phase": phase,
                    "site_id": site_id,
                }
            )
        )

        url = f"/domain-types/{self.domain}/collections/all"
        if query_params:
            url = f"{url}?{query_params}"

        return self.request(
            "get",
            url=url,
            expect_ok=expect_ok,
        )

    def update_and_acknowledge(
        self,
        event_id: str,
        site_id: str | None = None,
        change_comment: str | None = None,
        change_contact: str | None = None,
        expect_ok: bool = True,
        phase: Literal["open", "ack"] | None = None,
    ) -> Response:
        body = {
            "change_comment": change_comment,
            "change_contact": change_contact,
            "phase": phase,
        }

        if site_id is not None:
            body.update({"site_id": site_id})

        return self.request(
            "post",
            url=f"/objects/{self.domain}/{event_id}/actions/update_and_acknowledge/invoke",
            body={k: v for k, v in body.items() if v is not None},
            expect_ok=expect_ok,
        )

    def update_and_acknowledge_multiple(
        self,
        filter_type: Literal["query", "params", "all"],
        site_id: str | None = None,
        phase: Literal["open", "ack"] | None = None,
        change_comment: str | None = None,
        change_contact: str | None = None,
        query: str | None = None,
        host: str | None = None,
        application: str | None = None,
        state: Literal["warning", "ok", "critical", "unknown"] | None = None,
        expect_ok: bool = True,
    ) -> Response:
        body: dict[str, Any] = {
            "site_id": site_id,
            "filter_type": filter_type,
            "change_comment": change_comment,
            "change_contact": change_contact,
            "phase": phase,
        }

        if filter_type == "query":
            body.update({"query": query})
        elif filter_type == "params":
            filters = {"state": state, "host": host, "application": application}
            body.update({"filters": {k: v for k, v in filters.items() if v is not None}})

        return self.request(
            "post",
            url=f"/domain-types/{self.domain}/actions/update_and_acknowledge/invoke",
            body={k: v for k, v in body.items() if v is not None},
            expect_ok=expect_ok,
        )

    def change_event_state(
        self,
        event_id: str,
        site_id: str | None = None,
        new_state: Literal["warning", "ok", "critical", "unknown"] | None = None,
        expect_ok: bool = True,
    ) -> Response:
        body: dict[str, Any] = {
            "new_state": new_state,
            "site_id": site_id,
        }

        return self.request(
            "post",
            url=f"/objects/{self.domain}/{event_id}/actions/change_state/invoke",
            body={k: v for k, v in body.items() if v is not None},
            expect_ok=expect_ok,
        )

    def change_multiple_event_states(
        self,
        filter_type: Literal["query", "params"],
        new_state: Literal["warning", "ok", "critical", "unknown"],
        site_id: str | None = None,
        query: str | None = None,
        host: str | None = None,
        application: str | None = None,
        state: Literal["warning", "ok", "critical", "unknown"] | None = None,
        phase: Literal["open", "ack"] | None = None,
        expect_ok: bool = True,
    ) -> Response:
        body: dict[str, Any] = {
            "site_id": site_id,
            "filter_type": filter_type,
            "new_state": new_state,
        }

        if filter_type == "query":
            body.update({"query": query})
        else:
            filters = {"state": state, "host": host, "application": application, "phase": phase}
            body.update({"filters": {k: v for k, v in filters.items() if v is not None}})

        return self.request(
            "post",
            url=f"/domain-types/{self.domain}/actions/change_state/invoke",
            body={k: v for k, v in body.items() if v is not None},
            expect_ok=expect_ok,
        )

    def delete(
        self,
        filter_type: Literal["by_id", "query", "params"],
        site_id: str,
        query: str | None = None,
        event_id: int | None = None,
        host: str | None = None,
        application: str | None = None,
        state: Literal["warning", "ok", "critical", "unknown"] | None = None,
        phase: Literal["open", "ack"] | None = None,
        expect_ok: bool = True,
    ) -> Response:
        body: dict[str, Any] = {"site_id": site_id, "filter_type": filter_type}

        if filter_type == "by_id":
            body.update({"event_id": event_id})

        elif filter_type == "query":
            body.update({"query": query})

        else:
            filters = {"state": state, "host": host, "application": application, "phase": phase}
            body.update({"filters": {k: v for k, v in filters.items() if v is not None}})

        return self.request(
            "post",
            url=f"/domain-types/{self.domain}/actions/delete/invoke",
            body=body,
            expect_ok=expect_ok,
        )


class CommentClient(RestApiClient):
    domain: API_DOMAIN = "comment"

    def delete(
        self,
        site_id: str,
        delete_type: str,
        comment_id: Any | None = None,
        host_name: str | None = None,
        service_descriptions: Sequence[str] | None = None,
        query: Mapping[str, str] | None = None,
        expect_ok: bool = True,
    ) -> Response:
        body = _only_set_keys(
            {
                "site_id": site_id,
                "delete_type": delete_type,
                "comment_id": comment_id,
                "host_name": host_name,
                "service_descriptions": service_descriptions,
                "query": query,
            }
        )

        res = self.request(
            "post",
            url=f"/domain-types/{self.domain}/actions/delete/invoke",
            body=body,
            expect_ok=expect_ok,
        )

        if expect_ok:
            res.assert_status_code(204)

        return res

    def create_for_host(
        self,
        comment: str,
        comment_type: str = "host",
        host_name: str | None = None,
        query: Mapping[str, Any] | str | None = None,
        expect_ok: bool = True,
    ) -> Response:
        body: dict[str, Any] = _only_set_keys(
            {
                "comment": comment,
                "comment_type": comment_type,
                "host_name": host_name,
                "query": query,
            }
        )

        return self._create(body, "host", expect_ok)

    def create_for_service(
        self,
        comment: str,
        comment_type: str = "service",
        host_name: str | None = None,
        query: Mapping[str, Any] | str | None = None,
        service_description: str | None = None,
        persistent: bool | None = None,
        expect_ok: bool = True,
    ) -> Response:
        body: dict[str, Any] = _only_set_keys(
            {
                "comment_type": comment_type,
                "comment": comment,
                "host_name": host_name,
                "query": query,
                "persistent": persistent,
                "service_description": service_description,
            }
        )

        return self._create(body, "service", expect_ok)

    def _create(self, body: dict[str, Any], collection: str, expect_ok: bool) -> Response:
        return self.request(
            "post",
            url=f"/domain-types/{self.domain}/collections/{collection}",
            body=body,
            expect_ok=expect_ok,
        )

    def get_all(
        self,
        host_name: str | None = None,
        service_description: str | None = None,
        query: Mapping[str, Any] | str | None = None,
        site_id: str | None = None,
        expect_ok: bool = True,
    ) -> Response:
        q: Mapping[str, Any] = _only_set_keys(
            {
                "host_name": host_name,
                "service_description": service_description,
                "query": query,
                "site_id": site_id,
            }
        )

        return self._get("all", q, expect_ok)

    def get_host(
        self,
        host_name: str | None = None,
        service_description: str | None = None,
        expect_ok: bool = True,
    ) -> Response:
        query: Mapping[str, Any] = _only_set_keys(
            {"host_name": host_name, "service_description": service_description}
        )

        return self._get("host", query, expect_ok)

    def get_service(self, expect_ok: bool = True) -> Response:
        return self._get("service", None, expect_ok)

    def _get(
        self, collection: str, query: Mapping[str, Any] | None = None, expect_ok: bool = True
    ) -> Response:
        return self.request(
            "get",
            url=f"/domain-types/{self.domain}/collections/{collection}",
            query_params=query if query else None,
            expect_ok=expect_ok,
        )

    def get(
        self, comment_id: str | int, site_id: str | None = None, expect_ok: bool = True
    ) -> Response:
        # TODO: Agregar el parámetro site_id: str
        qp = _only_set_keys({"site_id": site_id})
        return self.request(
            "get",
            url=f"/objects/{self.domain}/{comment_id}",
            query_params=qp if qp else None,
            expect_ok=expect_ok,
        )


class DcdClient(RestApiClient):
    domain: Literal["dcd"] = "dcd"

    def get(self, dcd_id: str, expect_ok: bool = True) -> Response:
        return self.request(
            "get",
            url=f"/objects/{self.domain}/{dcd_id}",
            expect_ok=expect_ok,
        )

    def get_all(self, expect_ok: bool = True) -> Response:
        return self.request(
            "get",
            url=f"/domain-types/{self.domain}/collections/all",
            expect_ok=expect_ok,
        )

    def create(
        self,
        dcd_id: str,
        site: str,
        title: str
        | None = None,  # Set as optional in order to run tests on missing fields behavior
        comment: str | None = None,
        documentation_url: str | None = None,
        disabled: bool | None = None,
        interval: int | None = None,
        connector_type: str | None = None,
        discover_on_creation: bool | None = None,
        no_deletion_time_after_init: int | None = None,
        validity_period: int | None = None,
        exclude_time_ranges: list[dict[str, str]] | None = None,
        creation_rules: list[dict[str, Any]] | None = None,
        expect_ok: bool = True,
    ) -> Response:
        body: dict[str, Any] = {
            k: v
            for k, v in {
                "dcd_id": dcd_id,
                "title": title,
                "site": site,
                "comment": comment,
                "documentation_url": documentation_url,
                "disabled": disabled,
                "interval": interval,
                "discover_on_creation": discover_on_creation,
                "no_deletion_time_after_init": no_deletion_time_after_init,
                "validity_period": validity_period,
                "creation_rules": creation_rules,
                "exclude_time_ranges": exclude_time_ranges,
                "connector_type": connector_type,
            }.items()
            if v is not None
        }

        return self.request(
            "post",
            url=f"/domain-types/{self.domain}/collections/all",
            body=body,
            expect_ok=expect_ok,
        )

    def edit(
        self,
        dcd_id: str,
        title: str,
        site: str,
        comment: str | None = None,
        documentation_url: str | None = None,
        disabled: bool | None = None,
        interval: int | None = None,
        discover_on_creation: bool | None = None,
        no_deletion_time_after_init: int | None = None,
        validity_period: int | None = None,
        creation_rules: list[dict[str, Any]] | None = None,
        exclude_time_ranges: list[dict[str, str]] | None = None,
        expect_ok: bool = True,
    ) -> Response:
        body: dict[str, Any] = {
            k: v
            for k, v in {
                "dcd_id": dcd_id,
                "title": title,
                "site": site,
                "comment": comment,
                "documentation_url": documentation_url,
                "disabled": disabled,
                "interval": interval,
                "discover_on_creation": discover_on_creation,
                "no_deletion_time_after_init": no_deletion_time_after_init,
                "validity_period": validity_period,
                "creation_rules": creation_rules,
                "exclude_time_ranges": exclude_time_ranges,
            }.items()
            if v is not None
        }

        return self.request(
            "put",
            url=f"/objects/{self.domain}/{dcd_id}",
            body=body,
            expect_ok=expect_ok,
        )

    def delete(self, dcd_id: str, expect_ok: bool = True) -> Response:
        return self.request(
            "delete",
            url=f"/objects/{self.domain}/{dcd_id}",
            expect_ok=expect_ok,
        )


class AuditLogClient(RestApiClient):
    domain: API_DOMAIN = "audit_log"

    def get_all(
        self,
        date: Any = "now",
        object_type: str | None = None,
        object_id: str | None = None,
        user_id: str | None = None,
        regexp: str | None = None,
        expect_ok: bool = True,
    ) -> Response:
        query = _only_set_keys(
            {
                "date": date,
                "object_type": object_type,
                "object_id": object_id,
                "user_id": user_id,
                "regexp": regexp,
            },
        )

        result = self.request(
            "get",
            url=f"/domain-types/{self.domain}/collections/all",
            query_params=query,
            expect_ok=expect_ok,
        )

        if expect_ok:
            result.assert_status_code(200)

        return result

    def clear(self, expect_ok: bool = True) -> Response:
        result = self.request(
            "delete",
            url=f"/domain-types/{self.domain}/collections/all",
            expect_ok=expect_ok,
        )

        if expect_ok:
            result.assert_status_code(204)

        return result


class BiPackClient(RestApiClient):
    domain: API_DOMAIN = "bi_pack"

    def get(self, pack_id: str, expect_ok: bool = True) -> Response:
        return self.request(
            "get",
            url=f"/objects/{self.domain}/{pack_id}",
            expect_ok=expect_ok,
        )

    def get_all(self, expect_ok: bool = True) -> Response:
        return self.request(
            "get",
            url=f"/domain-types/{self.domain}/collections/all",
            expect_ok=expect_ok,
        )

    def create(self, pack_id: str, body: dict[str, Any], expect_ok: bool = True) -> Response:
        return self.request(
            "post",
            url=f"/objects/{self.domain}/{pack_id}",
            body=body,
            expect_ok=expect_ok,
        )

    def edit(self, pack_id: str, body: dict[str, Any], expect_ok: bool = True) -> Response:
        return self.request(
            "put",
            url=f"/objects/{self.domain}/{pack_id}",
            body=body,
            expect_ok=expect_ok,
        )

    def delete(self, pack_id: str, expect_ok: bool = True) -> Response:
        return self.request(
            "delete",
            url=f"/objects/{self.domain}/{pack_id}",
            expect_ok=expect_ok,
        )


class BiAggregationClient(RestApiClient):
    domain: API_DOMAIN = "bi_aggregation"

    def get(self, aggregation_id: str, expect_ok: bool = True) -> Response:
        return self.request(
            "get",
            url=f"/objects/{self.domain}/{aggregation_id}",
            expect_ok=expect_ok,
        )

    def create(self, aggregation_id: str, body: dict[str, Any], expect_ok: bool = True) -> Response:
        return self.request(
            "post",
            url=f"/objects/{self.domain}/{aggregation_id}",
            body=body,
            expect_ok=expect_ok,
        )

    def edit(self, aggregation_id: str, body: dict[str, Any], expect_ok: bool = True) -> Response:
        return self.request(
            "put",
            url=f"/objects/{self.domain}/{aggregation_id}",
            body=body,
            expect_ok=expect_ok,
        )

    def delete(self, aggregation_id: str, expect_ok: bool = True) -> Response:
        return self.request(
            "delete",
            url=f"/objects/{self.domain}/{aggregation_id}",
            expect_ok=expect_ok,
        )

    def get_aggregation_state(self, body: dict[str, Any], expect_ok: bool = True) -> Response:
        return self.request(
            "post",
            url=f"/domain-types/{self.domain}/actions/aggregation_state/invoke",
            expect_ok=expect_ok,
        )


class BiRuleClient(RestApiClient):
    domain: API_DOMAIN = "bi_rule"

    def get(self, rule_id: str, expect_ok: bool = True) -> Response:
        return self.request(
            "get",
            url=f"/objects/{self.domain}/{rule_id}",
            expect_ok=expect_ok,
        )

    def create(self, rule_id: str, body: dict[str, Any], expect_ok: bool = True) -> Response:
        return self.request(
            "post",
            url=f"/objects/{self.domain}/{rule_id}",
            body=body,
            expect_ok=expect_ok,
        )

    def edit(self, rule_id: str, body: dict[str, Any], expect_ok: bool = True) -> Response:
        return self.request(
            "put",
            url=f"/objects/{self.domain}/{rule_id}",
            body=body,
            expect_ok=expect_ok,
        )

    def delete(self, rule_id: str, expect_ok: bool = True) -> Response:
        return self.request(
            "delete",
            url=f"/objects/{self.domain}/{rule_id}",
            expect_ok=expect_ok,
        )


class UserRoleClient(RestApiClient):
    domain: API_DOMAIN = "user_role"

    def get(self, role_id: str, expect_ok: bool = True) -> Response:
        return self.request(
            "get",
            url=f"/objects/{self.domain}/{role_id}",
            expect_ok=expect_ok,
        )

    def get_all(self, expect_ok: bool = True) -> Response:
        return self.request(
            "get",
            url=f"/domain-types/{self.domain}/collections/all",
            expect_ok=expect_ok,
        )

    def clone(self, body: dict[str, Any], expect_ok: bool = True) -> Response:
        return self.request(
            "post",
            url=f"/domain-types/{self.domain}/collections/all",
            body=body,
            expect_ok=expect_ok,
        )

    def edit(self, role_id: str, body: dict[str, Any], expect_ok: bool = True) -> Response:
        return self.request(
            "put",
            url=f"/objects/{self.domain}/{role_id}",
            body=body,
            expect_ok=expect_ok,
        )

    def delete(self, role_id: str, expect_ok: bool = True) -> Response:
        return self.request(
            "delete",
            url=f"/objects/{self.domain}/{role_id}",
            expect_ok=expect_ok,
        )


@dataclasses.dataclass
class ClientRegistry:
    Licensing: LicensingClient
    ActivateChanges: ActivateChangesClient
    User: UserClient
    HostConfig: HostConfigClient
    Host: HostClient
    Folder: FolderClient
    AuxTag: AuxTagClient
    TimePeriod: TimePeriodClient
    Rule: RuleClient
    Ruleset: RulesetClient
    HostTagGroup: HostTagGroupClient
    Password: PasswordClient
    Agent: AgentClient
    Downtime: DowntimeClient
    HostGroup: HostGroupClient
    ServiceGroup: ServiceGroupClient
    ContactGroup: ContactGroupClient
    SiteManagement: SiteManagementClient
    RuleNotification: RuleNotificationClient
    Comment: CommentClient
    EventConsole: EventConsoleClient
    Dcd: DcdClient
    AuditLog: AuditLogClient
    BiPack: BiPackClient
    BiAggregation: BiAggregationClient
    BiRule: BiRuleClient
    UserRole: UserRoleClient


def get_client_registry(request_handler: RequestHandler, url_prefix: str) -> ClientRegistry:
    return ClientRegistry(
        Licensing=LicensingClient(request_handler, url_prefix),
        ActivateChanges=ActivateChangesClient(request_handler, url_prefix),
        User=UserClient(request_handler, url_prefix),
        HostConfig=HostConfigClient(request_handler, url_prefix),
        Host=HostClient(request_handler, url_prefix),
        Folder=FolderClient(request_handler, url_prefix),
        AuxTag=AuxTagClient(request_handler, url_prefix),
        TimePeriod=TimePeriodClient(request_handler, url_prefix),
        Rule=RuleClient(request_handler, url_prefix),
        Ruleset=RulesetClient(request_handler, url_prefix),
        HostTagGroup=HostTagGroupClient(request_handler, url_prefix),
        Password=PasswordClient(request_handler, url_prefix),
        Agent=AgentClient(request_handler, url_prefix),
        Downtime=DowntimeClient(request_handler, url_prefix),
        HostGroup=HostGroupClient(request_handler, url_prefix),
        ServiceGroup=ServiceGroupClient(request_handler, url_prefix),
        ContactGroup=ContactGroupClient(request_handler, url_prefix),
        SiteManagement=SiteManagementClient(request_handler, url_prefix),
        RuleNotification=RuleNotificationClient(request_handler, url_prefix),
        Comment=CommentClient(request_handler, url_prefix),
        EventConsole=EventConsoleClient(request_handler, url_prefix),
        Dcd=DcdClient(request_handler, url_prefix),
        AuditLog=AuditLogClient(request_handler, url_prefix),
        BiPack=BiPackClient(request_handler, url_prefix),
        BiAggregation=BiAggregationClient(request_handler, url_prefix),
        BiRule=BiRuleClient(request_handler, url_prefix),
        UserRole=UserRoleClient(request_handler, url_prefix),
    )
