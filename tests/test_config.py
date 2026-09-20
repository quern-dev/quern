"""Tests for server.config — user-config persistence helpers.

Focused on the channel helpers added in #41. Other config helpers in
this module are exercised indirectly by the routes that use them.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """Redirect ~/.quern/config.json to a temp dir for every test."""
    monkeypatch.setattr("server.config.CONFIG_DIR", tmp_path)
    monkeypatch.setattr(
        "server.config.USER_CONFIG_FILE", tmp_path / "config.json",
    )
    return tmp_path


def test_get_update_channel_defaults_to_stable():
    from server.config import get_update_channel
    assert get_update_channel() == "stable"


def test_set_update_channel_round_trip():
    from server.config import get_update_channel, set_update_channel
    set_update_channel("beta")
    assert get_update_channel() == "beta"


def test_set_update_channel_rejects_unknown_name():
    from server.config import set_update_channel
    with pytest.raises(ValueError, match="Unknown update channel"):
        set_update_channel("nightly")


def test_unknown_channel_in_config_falls_back_to_default(isolated_config):
    """A typo in config.json should not break the daemon — falls back to
    the safe default rather than throwing."""
    import json
    (isolated_config / "config.json").write_text(
        json.dumps({"update_channel": "very_unstable"})
    )
    from server.config import get_update_channel
    assert get_update_channel() == "stable"


def test_channel_to_release_branch():
    from server.config import channel_to_release_branch
    assert channel_to_release_branch("stable") == "release/stable"
    assert channel_to_release_branch("beta") == "release/beta"


class TestTheApiKeyFileMustBeSendable:
    """`~/.quern/api-key` is hand-edited, and its contents become an HTTP
    header value."""

    def test_a_non_ascii_key_is_refused_and_the_message_names_the_file(
        self, tmp_path, monkeypatch,
    ):
        """No client can send one: httpx raises UnicodeEncodeError and node's
        Headers a TypeError, each naming a character rather than the file the
        character came from. Without this the server starts and then refuses
        every request, which reads as "authentication is broken"."""
        from server import config as config_mod

        key_file = tmp_path / "api-key"
        key_file.write_text("abc\u2019def\n")          # a pasted smart quote
        monkeypatch.setattr(config_mod, "API_KEY_FILE", key_file)
        monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)

        with pytest.raises(ValueError, match=str(key_file)):
            config_mod.ServerConfig()

    @pytest.mark.parametrize(
        "bad,name",
        [("abc\rdef", "CR"), ("abc\ndef", "LF"), ("abc\x00def", "NUL"),
         ("abc\x7fdef", "DEL")],
    )
    def test_a_key_with_a_control_character_is_refused(
        self, tmp_path, monkeypatch, bad, name,
    ):
        """`isascii()` calls these ASCII and `.strip()` only takes them off the
        ends, so an embedded one passed both checks. Measured with httpx
        against a running server: the request is never sent -- it fails with
        `LocalProtocolError: Illegal header value`, which names the character
        and not the file it came from. Identical outcome to the non-ASCII case
        above, so identical refusal."""
        from server import config as config_mod

        key_file = tmp_path / "api-key"
        key_file.write_text(bad)
        monkeypatch.setattr(config_mod, "API_KEY_FILE", key_file)
        monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)

        with pytest.raises(ValueError, match=str(key_file)):
            config_mod.ServerConfig()

    def test_a_key_is_not_refused_for_surrounding_whitespace(self, tmp_path, monkeypatch):
        """The trailing newline every editor adds is stripped, not refused.

        The control-character check runs after `.strip()` for exactly this
        reason; run before it, it would reject every hand-written file."""
        from server import config as config_mod

        key_file = tmp_path / "api-key"
        key_file.write_text("\n  plain-key  \r\n")
        monkeypatch.setattr(config_mod, "API_KEY_FILE", key_file)
        monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)

        assert config_mod.ServerConfig().api_key == "plain-key"

    def test_an_ascii_key_is_used_as_written(self, tmp_path, monkeypatch):
        from server import config as config_mod

        key_file = tmp_path / "api-key"
        key_file.write_text("  plain-key  \n")
        monkeypatch.setattr(config_mod, "API_KEY_FILE", key_file)
        monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)

        assert config_mod.ServerConfig().api_key == "plain-key"


class TestStartingWithAnUnusableKeyFile:
    def test_it_stops_with_the_reason_and_no_traceback(self, tmp_path, monkeypatch, capsys):
        """The check is only worth having if its sentence is what the operator
        sees; a traceback buries it."""
        from server import main as main_mod

        def boom(**kwargs):
            raise ValueError(f"The API key in {tmp_path}/api-key is unusable")

        monkeypatch.setattr(main_mod, "ServerConfig", boom)

        with pytest.raises(SystemExit) as exit_info:
            main_mod._config_or_exit(host="127.0.0.1", port=9100, ring_buffer_size=10)

        assert exit_info.value.code == 1
        assert "api-key is unusable" in capsys.readouterr().out

    def test_a_usable_key_file_is_returned(self, tmp_path, monkeypatch):
        from server import main as main_mod

        key_file = tmp_path / "api-key"
        key_file.write_text("usable-key")
        monkeypatch.setattr("server.config.API_KEY_FILE", key_file)
        # Redirected as well as the key file: loading creates CONFIG_DIR before
        # it reads the key, so a test that patches only the file still makes
        # ~/.quern on a machine that has none.
        monkeypatch.setattr("server.config.CONFIG_DIR", tmp_path)

        config = main_mod._config_or_exit(host="127.0.0.1", port=9100, ring_buffer_size=10)

        assert config.api_key == "usable-key"


class TestAnUnusableKeyFileStopsBeforeAnythingIsTornDown:
    """`quern start` does real damage before it reaches the config.

    Between the healthy-instance early exit and the config, it removes stale
    state, reclaims ports from stale quern processes, and daemonizes. Failing
    after that leaves the previous instance's leftovers destroyed for a start
    that never happens -- and past `daemonize()` the message goes to the child
    log, so the shell sees `quern start` succeed with no server running.
    """

    def _args(self, **over):
        import argparse

        base = dict(host="127.0.0.1", port=9100, buffer_size=10, foreground=True,
                    verbose=False, syslog=False, no_syslog=True, oslog=False,
                    no_oslog=True, no_crash=True, no_proxy=True, proxy_port=9101)
        base.update(over)
        return argparse.Namespace(**base)

    def test_nothing_is_torn_down_when_the_key_file_is_unusable(
        self, tmp_path, monkeypatch, capsys,
    ):
        from server import main as main_mod

        torn_down: list[str] = []
        # Imported inside _cmd_start from server.__main__, so they are not
        # attributes of server.main.
        monkeypatch.setattr("server.__main__._ensure_python_deps", lambda **k: None)
        monkeypatch.setattr("server.__main__._ensure_mcp_built", lambda **k: True)
        monkeypatch.setattr(main_mod, "read_state",
                            lambda: {"server_port": 9100, "pid": 1})
        monkeypatch.setattr(main_mod, "is_server_healthy", lambda *a, **k: False)
        monkeypatch.setattr(main_mod, "remove_state",
                            lambda: torn_down.append("remove_state"))
        monkeypatch.setattr(main_mod, "reclaim_port",
                            lambda *a, **k: torn_down.append("reclaim_port") or True)
        monkeypatch.setattr(main_mod, "daemonize",
                            lambda *a, **k: torn_down.append("daemonize"))

        key_file = tmp_path / "api-key"
        key_file.write_text("abc\rdef")
        monkeypatch.setattr("server.config.API_KEY_FILE", key_file)
        monkeypatch.setattr("server.config.CONFIG_DIR", tmp_path)

        with pytest.raises(SystemExit) as exit_info:
            main_mod._cmd_start(self._args())

        assert exit_info.value.code == 1
        assert torn_down == [], (
            f"the start tore things down before refusing: {torn_down}"
        )
        assert str(key_file) in capsys.readouterr().out

    def test_a_running_server_is_still_reported_before_the_config_is_read(
        self, tmp_path, monkeypatch, capsys,
    ):
        """The early exit stays first: "Server already running" must not turn
        into a config complaint on a machine whose key file is broken."""
        from server import main as main_mod

        # Imported inside _cmd_start from server.__main__, so they are not
        # attributes of server.main.
        monkeypatch.setattr("server.__main__._ensure_python_deps", lambda **k: None)
        monkeypatch.setattr("server.__main__._ensure_mcp_built", lambda **k: True)
        monkeypatch.setattr(main_mod, "read_state",
                            lambda: {"server_port": 9100, "pid": 1})
        monkeypatch.setattr(main_mod, "is_server_healthy", lambda *a, **k: True)
        monkeypatch.setattr(main_mod, "_print_status", lambda *a, **k: None)

        key_file = tmp_path / "api-key"
        key_file.write_text("abc\rdef")
        monkeypatch.setattr("server.config.API_KEY_FILE", key_file)
        monkeypatch.setattr("server.config.CONFIG_DIR", tmp_path)

        with pytest.raises(SystemExit) as exit_info:
            main_mod._cmd_start(self._args())

        assert exit_info.value.code == 0
        assert "already running" in capsys.readouterr().out
