#!/usr/bin/env python3
# Copyright (C) 2023 Checkmk GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.

import json
import logging
import re
from collections import defaultdict
from pathlib import Path
from typing import Literal

from git.repo import Repo

from cmk.werks import load_werk, parse_werk

from .werk import WebsiteWerk

logger = logging.getLogger(__name__)


class Config:
    """
    We read werks from different git repositories, and each repo has its own
    quirks. This config tries to hold all repo specific configuration.
    """

    # only branches matching this regex will be considered for searching for
    # werk files.
    branch_regex: str

    def cleanup_branch_name(self, branch_name: str) -> str:
        # in previous releases of cmk there were branches called 1.2.7i3
        # but we want to store the werks in 1.2.7, we use this function to
        # adapt this.
        return branch_name

    def adapt_werk_string(self, werk_string: str, werk_filename: str) -> str:
        # there is one faulty werk in cmk repo, but in many branches
        # its easier to fix here...
        return werk_string

    @classmethod
    def from_flavor(cls, flavor: str) -> "Config":
        if flavor == "cma":
            return CmaConfig()
        if flavor == "checkmk_kube_agent":
            return KubeConfig()
        if flavor == "cmk":
            return CmkConfig()
        raise NotImplementedError()


class CmaConfig(Config):
    branch_regex = r"^(master$|\d+\.\d+$)"


class CmkConfig(Config):
    branch_regex = r"^master$|^\d+\.\d+\.\d+"

    def cleanup_branch_name(self, branch_name: str) -> str:
        """
        >>> CmkConfig().cleanup_branch_name("1.5.0i3")
        '1.5.0'

        >>> CmkConfig().cleanup_branch_name("1.5.0")
        '1.5.0'

        >>> [CmkConfig().cleanup_branch_name(v) for v in [ "1.2.5", "1.2.6", "1.2.7", "1.2.8"]]
        ['1.2.0', '1.2.0', '1.2.0', '1.2.0']
        """
        if branch_name.startswith("1.2."):
            return "1.2.0"
        return re.sub(r"(i\d+)$", "", branch_name)

    def adapt_werk_string(self, werk_string: str, werk_filename: str) -> str:
        if werk_filename == "1281":
            # werk 1281 of cmk is missing a newline, but it is available in
            # multiple branches so we fix it here
            return werk_string.replace("Class: feature", "Class: feature\n")
        if werk_filename == "3229":
            # don't want to commit to branch 1.2.8
            return werk_string.replace("name inventorized\\", "name inventorized")
        return werk_string


class KubeConfig(Config):
    branch_regex = r"^(main$|\d+\.\d+\.\d+)"


def branch_name_to_sort(branch_name: str) -> tuple[str | int, ...]:
    """
    >>> sorted(["master", "2.1.0", "main", "2.1.0i1"], key=branch_name_to_sort)
    ['2.1.0i1', '2.1.0', 'main', 'master']

    """
    if branch_name in {"master", "main"}:
        return (99, 99, 99, 99, branch_name)
    result = re.match(r"(\d+)\.(\d+)\.(\d+)(i(\d))?$", branch_name)
    if not result:
        logger.info("can not sort branch %s", branch_name)
        return (0, 0, 0, 0, branch_name)
    major, minor, patch, _, innovation = result.groups()
    if innovation is None:
        innovation = 99
    return (int(major), int(minor), int(patch), int(innovation), branch_name)


def main(flavor: Literal["cma", "cmk", "checkmk_kube_agent"], repo_path: Path) -> None:
    logging.basicConfig()

    c = Config.from_flavor(flavor)

    werk_files_by_name_and_branch: dict[str, dict[str, bytes]] = defaultdict(dict)
    r = Repo(repo_path)
    for ref in r.remote().refs:
        if not ref.name.startswith("origin/"):
            logger.info("ignoring ref %s (only considering one from remote origin)", ref)
            continue

        branch_name = ref.name.removeprefix("origin/")
        if not re.match(c.branch_regex, branch_name):
            logger.info("ignoring branch %s (does not match regex)", branch_name)
            continue

        tree = r.tree(r.commit(f"refs/remotes/origin/{branch_name}"))
        try:
            werks = tree[".werks"]
        except KeyError:
            logger.warning("no .werks folder in branch %s", branch_name)
            continue

        for werk_file in werks.blobs:
            werk_files_by_name_and_branch[werk_file.name][
                branch_name
            ] = werk_file.data_stream.read()

    all_werks_by_id: dict[str, dict] = {}
    # TODO: think about whether werks share the same werk_filename on all branches...
    for werk_filename, werk_by_branch in werk_files_by_name_and_branch.items():
        if not werk_filename.isdigit():
            logger.warning("ignoring werk %s (filename is not digit)", werk_filename)
            continue

        versions: dict[str, str] = {}
        # werks are defined multiple times, the werk that is defined in the "latest" branch wins.
        # that's why we sort here by branch name...
        for branch, werk_bytes in sorted(
            werk_by_branch.items(), key=lambda i: branch_name_to_sort(i[0])
        ):
            werk_string = werk_bytes.decode("utf-8")
            werk_string = c.adapt_werk_string(werk_string, werk_filename)

            try:
                parsed = parse_werk(werk_string, werk_filename)
            except Exception as e:
                raise RuntimeError(
                    f"could not parse werk {werk_filename} from branch {branch} of flavor {flavor}"
                ) from e
            versions[c.cleanup_branch_name(branch)] = parsed.metadata["version"]

        try:
            # and here we simply access variables of the latest werk iteration above.
            werk = load_werk(
                file_content=werk_string, file_name=werk_filename
            )  # this could be optimized, as we now parse the werk twice.
        except Exception as e:
            raise RuntimeError(f"could not load werk {werk_filename} from flavor {flavor}") from e

        werk_dict = {k: v for k, v in werk.to_json_dict().items() if v is not None}
        werk_dict.pop("version")
        # WebsiteWerk is not really needed here, but we want to make sure we comply to the WebsiteWerk schema.
        all_werks_by_id[werk_filename] = WebsiteWerk(
            versions=versions,
            product=flavor,
            **werk_dict,  # type: ignore
        ).model_dump(by_alias=True, mode="json")

    print(json.dumps(all_werks_by_id))
