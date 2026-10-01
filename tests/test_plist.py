"""Unit tests for server/device/plist.py."""

from __future__ import annotations

import plistlib

import pytest

from server.device.plist import (
    diff_plists,
    read_plist,
    remove_plist_key,
    set_plist_value,
    set_plist_values,
)
from server.models import AppStateNotFoundError, DeviceError


class TestReadPlist:
    async def test_read_plist_parses_output(self, tmp_path):
        # Write a real plist file and read it back
        import plistlib

        plist_path = tmp_path / "test.plist"
        with open(plist_path, "wb") as f:
            plistlib.dump({"foo": "bar", "count": 42}, f)
        result = await read_plist(plist_path)
        assert result == {"foo": "bar", "count": 42}

    async def test_read_plist_raises_on_nonzero(self, tmp_path):
        with pytest.raises(DeviceError, match="plistlib read failed"):
            await read_plist(tmp_path / "missing.plist")


def _write(path, data, fmt=plistlib.FMT_XML):
    path.write_bytes(plistlib.dumps(data, fmt=fmt))
    return path


def _load(path):
    return plistlib.loads(path.read_bytes())


class TestSetPlistValue:
    """Against real files: the old tests asserted plutil's argv, so a dotted
    key -- which plutil reads as a nested key path -- could never fail one."""

    @pytest.mark.parametrize("value", [True, False, 42, 3.14, "hello"])
    async def test_each_type_round_trips_as_itself(self, tmp_path, value):
        path = _write(tmp_path / "p.plist", {})
        await set_plist_value(path, "k", value)
        stored = _load(path)["k"]
        assert stored == value and type(stored) is type(value)

    async def test_a_dotted_key_is_one_top_level_key(self, tmp_path):
        path = _write(tmp_path / "p.plist", {"other": 1})
        await set_plist_value(path, "probe.greeting", "hi")
        assert _load(path) == {"other": 1, "probe.greeting": "hi"}

    async def test_a_dotted_key_never_reaches_a_matching_nested_dict(self, tmp_path):
        """With key-path semantics this wrote into `com -> example -> flag`."""
        path = _write(tmp_path / "p.plist", {"com": {"example": {}}})
        await set_plist_value(path, "com.example.flag", True)
        assert _load(path) == {"com": {"example": {}}, "com.example.flag": True}

    async def test_a_binary_plist_stays_binary(self, tmp_path):
        """cfprefsd writes binary; rewriting it as XML would be a silent change."""
        path = _write(tmp_path / "p.plist", {"a": 1}, fmt=plistlib.FMT_BINARY)
        await set_plist_value(path, "b", 2)
        assert path.read_bytes().startswith(b"bplist00")
        assert _load(path) == {"a": 1, "b": 2}

    async def test_permissions_survive_the_rewrite(self, tmp_path):
        path = _write(tmp_path / "p.plist", {})
        path.chmod(0o600)
        await set_plist_value(path, "k", 1)
        assert path.stat().st_mode & 0o777 == 0o600

    async def test_a_batch_is_one_write(self, tmp_path):
        path = _write(tmp_path / "p.plist", {"keep": "x"})
        await set_plist_values(path, {"a.b": "s", "c.d": 7, "e.f": False})
        assert _load(path) == {"keep": "x", "a.b": "s", "c.d": 7, "e.f": False}

    async def test_a_failed_write_leaves_the_file_untouched(self, tmp_path):
        """All or nothing -- the old batch could leave some keys written."""
        path = _write(tmp_path / "p.plist", {"keep": "x"})
        before = path.read_bytes()
        with pytest.raises(DeviceError, match="editing"):
            await set_plist_values(path, {"ok": 1, "too_big": 2**70})
        assert path.read_bytes() == before
        assert [p.name for p in tmp_path.iterdir()] == ["p.plist"], "temp file left behind"

    async def test_a_missing_file_is_an_error(self, tmp_path):
        with pytest.raises(DeviceError, match="editing"):
            await set_plist_value(tmp_path / "missing.plist", "k", 1)

    @pytest.mark.parametrize("body", [
        b'<?xml version="1.0"?><plist><dict><key>a</key></plist>',
        b'<?xml version="1.0"?><plist><dict><key>d</key><date>nope</date></dict></plist>',
    ], ids=["broken-xml", "bad-date"])
    async def test_a_malformed_plist_is_a_device_error(self, tmp_path, body):
        """plistlib raises ExpatError and AttributeError here, neither of which
        is an OSError or ValueError; both escaped as undetailed 500s."""
        path = tmp_path / "p.plist"
        path.write_bytes(body)
        with pytest.raises(DeviceError, match="editing"):
            await set_plist_value(path, "k", 1)
        assert path.read_bytes() == body

    async def test_a_plist_that_is_not_a_dict_is_refused(self, tmp_path):
        path = _write(tmp_path / "p.plist", ["a"])
        with pytest.raises(DeviceError, match="not a dictionary"):
            await set_plist_value(path, "k", 1)


class TestRemovePlistKey:
    async def test_a_dotted_key_is_removed_literally(self, tmp_path):
        path = _write(tmp_path / "p.plist", {"probe.counter": 3, "probe": {"counter": 9}})
        await remove_plist_key(path, "probe.counter")
        assert _load(path) == {"probe": {"counter": 9}}

    async def test_a_missing_key_is_not_found(self, tmp_path):
        path = _write(tmp_path / "p.plist", {"a": 1})
        before = path.read_bytes()
        with pytest.raises(AppStateNotFoundError, match="not found"):
            await remove_plist_key(path, "b")
        assert path.read_bytes() == before


class TestDiffPlists:
    def test_no_changes(self):
        d = {"a": 1, "b": "hello"}
        result = diff_plists(d, d.copy())
        assert result == {"added": {}, "removed": {}, "changed": {}}

    def test_added_keys(self):
        old = {"a": 1}
        new = {"a": 1, "b": 2, "c": 3}
        result = diff_plists(old, new)
        assert result["added"] == {"b": 2, "c": 3}
        assert result["removed"] == {}
        assert result["changed"] == {}

    def test_removed_keys(self):
        old = {"a": 1, "b": 2, "c": 3}
        new = {"a": 1}
        result = diff_plists(old, new)
        assert result["added"] == {}
        assert result["removed"] == {"b": 2, "c": 3}
        assert result["changed"] == {}

    def test_changed_keys(self):
        old = {"a": 1, "b": "hello"}
        new = {"a": 1, "b": "world"}
        result = diff_plists(old, new)
        assert result["added"] == {}
        assert result["removed"] == {}
        assert result["changed"] == {"b": {"old": "hello", "new": "world"}}

    def test_mixed_changes(self):
        old = {"a": 1, "b": "hello", "c": True}
        new = {"b": "world", "c": True, "d": 42}
        result = diff_plists(old, new)
        assert result["added"] == {"d": 42}
        assert result["removed"] == {"a": 1}
        assert result["changed"] == {"b": {"old": "hello", "new": "world"}}

    def test_empty_to_populated(self):
        result = diff_plists({}, {"a": 1, "b": 2})
        assert result["added"] == {"a": 1, "b": 2}
        assert result["removed"] == {}

    def test_populated_to_empty(self):
        result = diff_plists({"a": 1, "b": 2}, {})
        assert result["removed"] == {"a": 1, "b": 2}
        assert result["added"] == {}
