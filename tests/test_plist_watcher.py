"""Unit tests for server/sources/plist_watcher.py helpers."""

from server.sources.plist_watcher import _fmt, _summarize_keys


class TestFmt:
    def test_short_string(self):
        assert _fmt("hello") == "'hello'"

    def test_long_string_truncated(self):
        result = _fmt("x" * 200)
        assert result == "(200 chars)"

    def test_bool(self):
        assert _fmt(True) == "True"

    def test_int(self):
        assert _fmt(42) == "42"

    def test_long_repr_truncated(self):
        """A dict with many entries should be truncated."""
        big = {f"k{i}": i for i in range(50)}
        result = _fmt(big)
        assert "chars)" in result


class TestSummarizeKeys:
    def test_groups_common_prefixes(self):
        data = {
            "kHasSeenTip1": True,
            "kHasSeenTip2": True,
            "kHasSeenOnboarding": True,
            "uniqueKey": "val",
        }
        result = _summarize_keys(data)
        assert "kHas*" in result or "kHasSeen*" in result
        assert "other" in result

    def test_empty_dict(self):
        result = _summarize_keys({})
        assert result == ""

    def test_all_unique(self):
        data = {"alpha": 1, "beta": 2, "gamma": 3}
        result = _summarize_keys(data)
        assert "other" in result

    def test_single_key(self):
        result = _summarize_keys({"onlyKey": True})
        assert "1 other" in result


class TestStartContainsThePath:
    """`start_simulator_logging` starts watchers straight from the persisted
    config, which `configure_plist_watch` stores unchecked -- so the adapter's
    own check is the only one on that path, not the route's."""

    async def test_a_path_outside_the_container_is_never_read(self, tmp_path):
        from unittest.mock import AsyncMock, patch

        from server.sources.plist_watcher import PlistWatcherAdapter

        container = tmp_path / "container"
        container.mkdir()
        outside = tmp_path / "outside.plist"
        outside.write_bytes(b"")
        adapter = PlistWatcherAdapter(
            udid="U", bundle_id="com.example.App", container="data",
            plist_path="../outside.plist", poll_interval=1.0, on_entry=AsyncMock(),
        )
        with (
            patch(
                "server.sources.plist_watcher.resolve_container",
                AsyncMock(return_value=container),
            ),
            patch("server.sources.plist_watcher.read_plist", AsyncMock()) as read,
        ):
            await adapter.start()
        assert adapter._error and "leaves its container" in adapter._error
        read.assert_not_called()
        assert not adapter.is_running
