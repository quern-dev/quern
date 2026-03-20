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
