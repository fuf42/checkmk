#!/usr/bin/env python3
# Copyright (C) 2019 Checkmk GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.

from ast import literal_eval
from pathlib import Path
from unittest.mock import Mock

import pytest

from cmk.ccc import store

from cmk.utils.hostaddress import HostName

from cmk.checkengine.checking import CheckPluginName, ServiceID
from cmk.checkengine.value_store._api import (
    _ValueStore,
    ValueStoreManager,
)
from cmk.checkengine.value_store._utils import (
    _DynamicDiskSyncedMapping,
    _StaticDiskSyncedMapping,
    DiskSyncedMapping,
)

_TEST_KEY = ("check", "item", "user-key")


class Test_DynamicDiskSyncedMapping:
    @staticmethod
    def _get_ddsm() -> _DynamicDiskSyncedMapping[tuple[str, str, str], object]:
        return _DynamicDiskSyncedMapping()

    def test_init(self) -> None:
        ddsm = self._get_ddsm()
        assert not ddsm
        assert not ddsm.removed_keys

    def test_removed_del(self) -> None:
        ddsm = self._get_ddsm()
        try:
            del ddsm[_TEST_KEY]
        except KeyError:
            pass
        assert ddsm.removed_keys == {_TEST_KEY}

    def test_removed_pop(self) -> None:
        ddsm = self._get_ddsm()
        ddsm.pop(_TEST_KEY, None)
        assert ddsm.removed_keys == {_TEST_KEY}

    def test_setitem(self) -> None:
        ddsm = self._get_ddsm()
        value = object()
        ddsm[_TEST_KEY] = value
        assert ddsm[_TEST_KEY] is value
        assert not ddsm.removed_keys

        # remove and re-add (no, this is not trivial!)
        ddsm.pop(_TEST_KEY)
        ddsm[_TEST_KEY] = value
        assert not ddsm.removed_keys

    def test_delitem(self) -> None:
        ddsm = self._get_ddsm()
        ddsm[_TEST_KEY] = None
        assert _TEST_KEY in ddsm  # setup

        del ddsm[_TEST_KEY]
        assert _TEST_KEY not in ddsm

    def test_popitem(self) -> None:
        ddsm = self._get_ddsm()
        ddsm[_TEST_KEY] = None
        assert _TEST_KEY in ddsm  # setup

        ddsm.pop(_TEST_KEY)
        assert _TEST_KEY not in ddsm


class Test_StaticDiskSyncedMapping:
    def _mock_load(self, mocker):
        stored_item_states = (
            '{("check1", None, "stored-user-key-1"): 23,'
            ' ("check2", "item", "stored-user-key-2"): 42}'
        )

        mocker.patch.object(
            store,
            "load_text_from_file",
            side_effect=lambda *a, **kw: stored_item_states,
        )

    def _mock_store(self, mocker):
        mocker.patch.object(
            store,
            "save_text_to_file",
            autospec=True,
        )

    @staticmethod
    def _get_sdsm(
        tmp_path: Path,
    ) -> _StaticDiskSyncedMapping[tuple[str, str | None, str], object]:
        return _StaticDiskSyncedMapping(
            path=tmp_path / "test-host",
            log_debug=lambda msg: None,
            serializer=repr,
            deserializer=literal_eval,
        )

    def test_mapping_features(self, mocker: Mock, tmp_path: Path) -> None:
        self._mock_load(mocker)
        sdsm = self._get_sdsm(tmp_path)
        assert sdsm.get(("check_no", None, "moo")) is None
        with pytest.raises(KeyError):
            _ = sdsm[("check_no", None, "moo")]
        assert len(sdsm) == 2

        assert sdsm.get(("check1", None, "stored-user-key-1")) == 23
        assert sdsm[("check2", "item", "stored-user-key-2")] == 42
        assert list(sdsm) == [
            ("check1", None, "stored-user-key-1"),
            ("check2", "item", "stored-user-key-2"),
        ]
        assert len(sdsm) == 2

    def test_store(self, mocker: Mock, tmp_path: Path) -> None:
        self._mock_load(mocker)
        self._mock_store(mocker)

        sdsm = self._get_sdsm(tmp_path)

        sdsm.disksync(
            removed={("check2", "item", "stored-user-key-2")},
            updated=[(("check3", "el Barto", "Ay caramba"), "ASDF")],
        )

        expected_values = {
            ("check1", None, "stored-user-key-1"): 23,
            ("check3", "el Barto", "Ay caramba"): "ASDF",
        }
        written = store.save_text_to_file.call_args.args[1]  # type: ignore[attr-defined]
        assert written == repr(expected_values)
        assert list(sdsm.items()) == list(expected_values.items())


class Test_DiskSyncedMapping:
    @staticmethod
    def _get_dsm() -> DiskSyncedMapping:
        dynstore: _DynamicDiskSyncedMapping[tuple[str, str, str], str] = _DynamicDiskSyncedMapping()
        dynstore.update(
            {
                ("dyn", "key", "1"): "dyn-val-1",
                ("dyn", "key", "2"): "dyn-val-2",
            }
        )
        return DiskSyncedMapping(
            dynamic=dynstore,
            static={  # type: ignore[arg-type]
                ("stat", "key", "1"): "stat-val-1",
                ("stat", "key", "2"): "stat-val-2",
            },
        )

    def test_getitem(self) -> None:
        dsm = self._get_dsm()
        assert dsm[("stat", "key", "1")] == "stat-val-1"
        assert dsm.get(("stat", "key", "2")) == "stat-val-2"
        assert dsm.get(("stat", "key", "3")) is None
        assert dsm[("dyn", "key", "1")] == "dyn-val-1"
        assert dsm.get(("dyn", "key", "2")) == "dyn-val-2"
        assert dsm.get(("dyn", "key", "3")) is None

    def test_delitem(self) -> None:
        dsm = self._get_dsm()
        assert ("stat", "key", "1") in dsm
        assert ("dyn", "key", "2") in dsm

        del dsm[("stat", "key", "1")]
        assert ("stat", "key", "1") not in dsm

        with pytest.raises(KeyError):
            del dsm[("stat", "key", "1")]

        del dsm[("dyn", "key", "1")]
        assert ("dyn", "key", "1") not in dsm

    def test_pop(self) -> None:
        dsm = self._get_dsm()
        assert ("stat", "key", "1") in dsm
        assert ("dyn", "key", "2") in dsm

        dsm.pop(("stat", "key", "1"))
        assert ("stat", "key", "1") not in dsm

        dsm.pop(("dyn", "key", "1"))
        assert ("dyn", "key", "1") not in dsm

    def test_iter(self) -> None:
        dsm = self._get_dsm()
        assert sorted(dsm) == [
            ("dyn", "key", "1"),
            ("dyn", "key", "2"),
            ("stat", "key", "1"),
            ("stat", "key", "2"),
        ]

        dsm.pop(("stat", "key", "1"))
        assert sorted(dsm) == [("dyn", "key", "1"), ("dyn", "key", "2"), ("stat", "key", "2")]

        dsm.pop(("dyn", "key", "2"))
        assert sorted(dsm) == [("dyn", "key", "1"), ("stat", "key", "2")]


class Test_ValueStore:
    @staticmethod
    def _get_store() -> _ValueStore:
        host_name = HostName("moritz")
        return _ValueStore(
            data={
                (host_name, "check1", "item", "key1"): "42",
                (host_name, "check2", "item", "key2"): "23",
            },
            service_id=(CheckPluginName("check1"), "item"),
            host_name=host_name,
        )

    def test_separation(self) -> None:
        s_store = self._get_store()
        assert "key1" in s_store
        assert "key2" not in s_store

    def test_invalid_key(self) -> None:
        s_store = self._get_store()
        with pytest.raises(TypeError):
            s_store[2] = "key must be string!"  # type: ignore[index]

    def test_serialization_happens_in_plugin_scope(self) -> None:
        s_store = self._get_store()
        s_store["key"] = float("inf")  # gets serialized here
        with pytest.raises(ValueError):
            _ = s_store["key"]  # deserialization failes here, not upon loading the store.


class TestValueStoreManager:
    @staticmethod
    def test_namespace_context() -> None:
        vsm = ValueStoreManager(HostName("test-host"))
        service_inner = ServiceID(CheckPluginName("unit_test_inner"), None)
        service_outer = ServiceID(CheckPluginName("unit_test_outer"), None)

        assert vsm.active_service_interface is None

        with vsm.namespace(service_outer):
            assert vsm.active_service_interface is not None
            vsm.active_service_interface["key"] = "outer"

            with vsm.namespace(service_inner):
                assert vsm.active_service_interface is not None
                assert "key" not in vsm.active_service_interface
                vsm.active_service_interface["key"] = "inner"

            assert vsm.active_service_interface["key"] == "outer"

        assert vsm.active_service_interface is None
