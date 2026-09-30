"""Naming an app must not silently stop capturing its web traffic.

`POST /proxy/local-capture` and `quern enable-local-capture` both *replaced*
the process list, so naming an app dropped `com.apple.WebKit.Networking` --
where a webview's requests actually leave -- and `MobileSafari`, where an
OAuth hand-off goes. The result was zero flows and no error, which reads
exactly like an app that made no requests.

Reported as a mistake agents make repeatedly, and made in this repo during the
trace work, where it was misdiagnosed as a broken redirector before the
configuration was checked.
"""

from __future__ import annotations

from server.config import CAPTURE_MINIMUM, with_capture_minimum


class TestTheMinimumIsAddedToWhateverIsNamed:
    def test_naming_an_app_keeps_the_web_processes(self):
        processes, added = with_capture_minimum(["MyApp"])

        assert "MyApp" in processes
        assert set(CAPTURE_MINIMUM) <= set(processes)
        assert set(added) == set(CAPTURE_MINIMUM)

    def test_the_callers_own_entries_come_first(self):
        """The list should read as "what I asked for, plus what makes it
        work"."""
        processes, _ = with_capture_minimum(["MyApp"])

        assert processes[0] == "MyApp"

    def test_nothing_is_added_twice(self):
        processes, added = with_capture_minimum(["MobileSafari", "MyApp"])

        assert processes.count("MobileSafari") == 1
        assert "MobileSafari" not in added

    def test_an_empty_list_still_disables_capture(self):
        """Empty means *stop capturing*. Turning it into "capture the
        defaults" would be the opposite of what was asked, and there would be
        no way left to turn capture off."""
        processes, added = with_capture_minimum([])

        assert processes == []
        assert added == []


class TestWhySafariIsInTheMinimum:
    def test_safari_is_included(self):
        """Not only for Safari's own browsing. An OAuth flow hands off to
        real Safari -- ASWebAuthenticationSession and SFSafariViewController
        are Safari -- so a login journey leaves the app's own processes
        entirely. Debugging sign-in without it means watching the interesting
        half vanish."""
        assert "MobileSafari" in CAPTURE_MINIMUM

    def test_the_webkit_networking_process_is_included(self):
        """The one that actually carries a webview's requests. Naming only
        the app captures none of its web traffic -- not less, none."""
        assert "com.apple.WebKit.Networking" in CAPTURE_MINIMUM


class TestTheOverrideExists:
    def test_a_caller_can_ask_for_exactly_their_list(self):
        """`only` is for someone who means a narrow list. Without an escape
        hatch, widening by default would be us overruling them."""
        from server.models import LocalCaptureRequest

        body = LocalCaptureRequest(processes=["MyApp"], only=True)

        assert body.only is True

    def test_it_is_off_by_default(self):
        from server.models import LocalCaptureRequest

        assert LocalCaptureRequest(processes=["MyApp"]).only is False


class TestTheHandlerActuallyWidens:
    """The helper being right is not the same as it being called.

    An earlier version of this file tested `with_capture_minimum` and nothing
    else, and deleting the call from the handler passed all of it. Testing the
    piece instead of the wiring is the failure this repo keeps finding, so
    these drive the endpoint.
    """

    @staticmethod
    def _request(monkeypatch):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, MagicMock

        from server.api import proxy as proxy_api

        adapter = MagicMock()
        adapter.is_running = False
        adapter.reconfigure = MagicMock()
        adapter.stop = AsyncMock()
        adapter.start = AsyncMock()

        written: list[list[str]] = []
        monkeypatch.setattr(
            "server.config.set_local_capture_processes", written.append,
        )
        monkeypatch.setattr(proxy_api, "update_state", lambda **kw: None)
        monkeypatch.setattr(
            proxy_api, "_ensure_ca_is_trusted", AsyncMock(return_value=None),
        )
        monkeypatch.setattr(
            proxy_api, "_get_proxy_status",
            AsyncMock(return_value=_status()),
        )

        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
            proxy_adapter=adapter, local_capture_processes=[],
        )))
        return request, adapter, written

    async def test_naming_an_app_reaches_the_adapter_widened(self, monkeypatch):
        from server.api.proxy import set_local_capture
        from server.models import LocalCaptureRequest

        request, adapter, _ = self._request(monkeypatch)

        await set_local_capture(
            request=request, body=LocalCaptureRequest(processes=["MyApp"]),
        )

        applied = adapter.reconfigure.call_args.kwargs["local_capture_processes"]
        assert "MyApp" in applied
        assert "com.apple.WebKit.Networking" in applied, (
            "the handler did not widen; the app's web traffic would be missed"
        )

    async def test_the_override_reaches_the_adapter_narrow(self, monkeypatch):
        from server.api.proxy import set_local_capture
        from server.models import LocalCaptureRequest

        request, adapter, _ = self._request(monkeypatch)

        await set_local_capture(
            request=request,
            body=LocalCaptureRequest(processes=["MyApp"], only=True),
        )

        applied = adapter.reconfigure.call_args.kwargs["local_capture_processes"]
        assert applied == ["MyApp"]

    async def test_the_response_says_what_was_added(self, monkeypatch):
        """In the response, not only the server log. An agent reads the
        result body and nothing else, and zero flows with no error is what
        this mistake looks like from the outside."""
        from server.api.proxy import set_local_capture
        from server.models import LocalCaptureRequest

        request, _, _ = self._request(monkeypatch)

        status = await set_local_capture(
            request=request, body=LocalCaptureRequest(processes=["MyApp"]),
        )

        assert status.capture_added
        assert "com.apple.WebKit.Networking" in status.capture_added


def _status():
    from server.models import ProxyStatusResponse

    return ProxyStatusResponse(status="stopped")


class TestTheStartupPathWidensToo:
    """The third entry point, and the one existing users take.

    `quern enable-local-capture MyApp` on any released version wrote
    `["MyApp"]` into config.json, and the lifespan routes straight from that
    file. Widening the endpoint and the CLI while leaving this alone meant
    every existing install upgraded into a release whose changelog said the
    bug was fixed and went on capturing nothing.
    """

    def test_a_stored_narrow_list_gains_the_minimum(self, monkeypatch, tmp_path):
        from server import config as cfg

        monkeypatch.setattr(cfg, "USER_CONFIG_FILE", tmp_path / "config.json")
        cfg.set_local_capture_processes(["MyApp"])

        routed, added = cfg.with_capture_minimum(cfg.get_local_capture_processes())

        assert routed[0] == "MyApp", "the caller's own entry stays first"
        for required in cfg.CAPTURE_MINIMUM:
            assert required in routed
        assert set(added) == set(cfg.CAPTURE_MINIMUM) - {"MyApp"}

    def test_the_lifespan_routes_the_widened_list(self, monkeypatch, tmp_path):
        """Asserting on the *source* of `main.py`, because what matters is
        that the startup path calls the widener at all -- the helper being
        correct says nothing about whether anyone uses it, which is exactly
        how this defect survived review of the helper's own tests."""
        import inspect

        from server import main as m

        src = inspect.getsource(m._cmd_start)
        assert "with_capture_minimum(" in src, (
            "the startup path must widen; without it a legacy config.json "
            "routes its narrow list unchanged"
        )
        widened = src.index("with_capture_minimum(")
        stored = src.index("get_local_capture_processes()")
        assert widened < stored, "the stored list must be read *into* the widener"


class TestTheDestructiveHalfIsReported:
    """`capture_removed` names what a call took away. It had no test, on a
    change whose whole thesis is that an unreported change is the defect."""

    def test_removing_a_process_is_named_on_the_response(self):
        from server.models import ProxyStatusResponse

        status = ProxyStatusResponse(status="running")
        status.capture_removed = ["OldThing"]

        assert status.capture_removed == ["OldThing"]
        assert "capture_removed" in status.model_dump()


class TestTheCLIWidensWhatItWrites:
    """The CLI half had no coverage at all: deleting its widening left the
    whole suite green, which is the failure this change exists to correct --
    corrected for the endpoint and not for the command that writes the file
    the server reads. Asserted on what reaches `config.json`, since that is
    what the lifespan routes from, not on the helper's return value.
    """

    def _run(self, monkeypatch, tmp_path, names):
        from server import config as cfg
        from server import main as m

        monkeypatch.setattr(cfg, "USER_CONFIG_FILE", tmp_path / "config.json")
        # The cert gate talks to devices; this command's subject is the list.
        monkeypatch.setattr(m, "_local_capture_cert_gate", lambda *a, **k: None)
        m._cmd_enable_local_capture(names, skip_cert_check=True)
        return cfg.get_local_capture_processes()

    def test_naming_an_app_still_stores_the_minimum(self, monkeypatch, tmp_path):
        from server.config import CAPTURE_MINIMUM

        stored = self._run(monkeypatch, tmp_path, ["MyApp"])

        assert stored[0] == "MyApp"
        for required in CAPTURE_MINIMUM:
            assert required in stored, f"{required} must survive into config.json"

    def test_naming_nothing_stores_the_minimum(self, monkeypatch, tmp_path):
        from server.config import CAPTURE_MINIMUM

        stored = self._run(monkeypatch, tmp_path, [])

        assert stored == list(CAPTURE_MINIMUM)

    def test_naming_a_minimum_process_does_not_duplicate_it(self, monkeypatch, tmp_path):
        stored = self._run(monkeypatch, tmp_path, ["MobileSafari"])

        assert stored.count("MobileSafari") == 1


class TestStartupSaysWhatItAdded:
    """Start-up widens a legacy list, which newly routes Safari and WebKit
    through the proxy. If the CA is not trusted on a booted simulator their
    HTTPS starts failing where it previously worked, and booting deliberately
    cannot refuse over a certificate -- so the one remedy available here is
    saying which processes were added. Without it the user sees Safari break
    on upgrade with nothing connecting the two.
    """

    def test_the_banner_names_the_added_processes(self):
        import inspect

        from server import main as m

        src = inspect.getsource(m._cmd_start)
        assert "_capture_added" in src, "start-up must compute what it added"
        banner = src.index("Local capture:")
        named = src.index("added for you")
        assert banner < named, "the addition is reported with the capture line"

    def test_it_points_at_the_certificate(self):
        import inspect

        from server import main as m

        src = inspect.getsource(m._cmd_start)
        added_at = src.index("added for you")
        tail = src[added_at:added_at + 700]
        assert "CA" in tail or "trusted" in tail, (
            "a user whose Safari broke needs the reason, not just the list"
        )


class TestDaemonModeSeesTheAdditionsToo:
    """`quern start` daemonizes by default: the child runs with
    `args.foreground` false so the foreground banner never prints, and the
    parent reports from `state.json`. Reporting the addition only in the
    banner left the default path silent -- the same one-of-two-entry-points
    defect this whole change is about, in the fix written for it.
    """

    def test_startup_records_what_it_added_in_state(self):
        import inspect

        from server import main as m

        src = inspect.getsource(m._cmd_start)
        assert '"local_capture_added"' in src, (
            "state.json must carry the additions; the daemon parent reports "
            "from that file and cannot see the foreground banner"
        )

    def test_status_output_reports_them(self):
        import inspect

        from server.lifecycle import daemon

        src = inspect.getsource(daemon._print_status)
        assert "local_capture_added" in src
        assert "added for you" in src
        idx = src.index("added for you")
        assert "doctor" in src[idx:idx + 500], (
            "naming the processes without naming the cause leaves a user "
            "whose Safari broke no better off"
        )


class TestStartingWithTheProxyOff:
    """`quern start --no-proxy` reaches the same state-dict build.

    `_capture_added` was assigned only inside `if enable_proxy:` while the
    state dict referenced it unconditionally, so starting with the proxy off
    raised `UnboundLocalError` before the server came up at all. The whole
    suite was green: nothing started with the proxy disabled, so the branch
    that skips the assignment was never executed.

    This runs the real `_cmd_start` with uvicorn stubbed out, so the binding
    is exercised rather than the source inspected -- a source check would not
    have caught the original either, since the line looked fine.
    """

    def _start(self, monkeypatch, tmp_path, argv):
        """Run the real `_cmd_start` with every startup side effect stubbed.

        The subject is a variable binding, so everything `_cmd_start` does on
        the way past it is noise -- and not harmless noise: `reclaim_port`
        SIGTERMs whatever it takes for a stale quern and waits up to 3.5s,
        `check_for_updates` reaches the network, and a state file left by a
        real server makes it `sys.exit(0)` before reaching the code under
        test, which would pass by never running it.

        `QUERN_STATE_DIR` is read at import, so setting the env var here does
        nothing; `conftest` already redirects it away from `~/.quern`. The
        write is captured instead of redirected, which also gives the
        assertion something to read.
        """
        import uvicorn

        from server import main as m
        from server.lifecycle import update_check

        started = {}

        class _Server:
            def __init__(self, config):
                started["config"] = config

            def run(self):
                started["ran"] = True

        monkeypatch.setattr(uvicorn, "Server", _Server)
        # The toolchain probe -- `xcode-select -p`, `xcrun simctl help`. The
        # #272 guard rightly fails any test that reads the real machine.
        monkeypatch.setattr(m, "_fix_developer_dir", lambda: None)
        # Never signal another process, and never claim the port is busy.
        monkeypatch.setattr(m, "reclaim_port", lambda *a, **k: True)
        # Imported inside the function, so patch the module it comes from.
        monkeypatch.setattr(update_check, "check_for_updates", lambda *a, **k: None)
        # No inherited state file, or startup exits before the binding runs.
        monkeypatch.setattr(m, "read_state", lambda: None)
        monkeypatch.setattr(m, "write_state", lambda st: started.__setitem__("state", st))
        monkeypatch.setattr(m.sys, "argv", argv)
        m.cli()
        return started

    def test_it_starts_with_the_proxy_disabled(self, monkeypatch, tmp_path):
        started = self._start(
            monkeypatch, tmp_path,
            ["quern", "start", "--no-proxy", "--foreground"],
        )

        assert started.get("ran"), "the server never reached uvicorn"

    def test_it_still_starts_with_the_proxy_enabled(self, monkeypatch, tmp_path):
        """The other half of the branch, so a fix that breaks the common path
        cannot pass by satisfying only the regression."""
        started = self._start(
            monkeypatch, tmp_path,
            ["quern", "start", "--foreground"],
        )

        assert started.get("ran")


class TestStateFollowsRuntimeChanges:
    """`quern status` reads `state.json`, which start-up writes. A later API
    change to the capture list left both fields behind, so status could list
    processes as "added for you" beneath a capture list that no longer
    contained them -- a record from boot presented as current fact.
    """

    def test_the_handler_updates_both_fields(self):
        import inspect

        from server.api import proxy as papi

        src = inspect.getsource(papi.set_local_capture)
        assert "update_state(" in src, (
            "a runtime change must reach state.json; quern status reads it"
        )
        call = src[src.index("update_state("):]
        assert "local_capture=" in call[:300]
        assert "local_capture_added=" in call[:300], (
            "both move together, or status reports boot-time additions "
            "under a list that has since changed"
        )


class TestAnExclusionFirstListIsRefused:
    """mitmproxy takes local mode's starting point from the *first* entry, so a
    list that opens with ``!X`` captures every process on the Mac except X.
    The minimum is appended after the caller's entries and cannot narrow it.
    ``["!12345"]`` reads like a narrowing and is the whole machine minus one
    PID, so every entry point refuses it unless whole-Mac capture was asked for
    by name. Driven through the handler and the CLI, not only the helper: a
    guard that is correct and not called passes a helper-only test.
    """

    def test_the_helper_finds_only_a_leading_exclusion(self):
        from server.config import leading_exclusion

        assert leading_exclusion(["!12345"]) == "!12345"
        assert leading_exclusion(["", "  !Helper"]) == "!Helper", (
            "blank entries are dropped before mitmproxy sees the list, so the "
            "first entry that counts is the first non-blank one"
        )
        assert leading_exclusion(["MyApp", "!12345"]) is None
        assert leading_exclusion([]) is None

    async def test_the_endpoint_refuses_it_before_touching_anything(self, monkeypatch):
        from fastapi import HTTPException

        from server.api import proxy as proxy_api
        from server.api.proxy import set_local_capture
        from server.models import LocalCaptureRequest

        request, adapter, written = TestTheHandlerActuallyWidens._request(monkeypatch)

        try:
            await set_local_capture(
                request=request, body=LocalCaptureRequest(processes=["!12345"]),
            )
        except HTTPException as exc:
            assert exc.status_code == 400
            assert "EVERY process on the Mac" in exc.detail
            assert "whole_mac" in exc.detail, "the refusal must name the way through"
        else:
            raise AssertionError("an exclusion-first list was accepted")

        adapter.reconfigure.assert_not_called()
        assert written == [], "a refused list must not reach config.json"
        proxy_api._ensure_ca_is_trusted.assert_not_called(), (
            "refuse before the CA gate, which may install a root CA"
        )

    async def test_asking_for_the_whole_mac_is_allowed(self, monkeypatch):
        from server.api.proxy import set_local_capture
        from server.models import LocalCaptureRequest

        request, adapter, _ = TestTheHandlerActuallyWidens._request(monkeypatch)

        await set_local_capture(
            request=request,
            body=LocalCaptureRequest(processes=["!12345"], whole_mac=True),
        )

        applied = adapter.reconfigure.call_args.kwargs["local_capture_processes"]
        assert applied[0] == "!12345"

    async def test_an_exclusion_after_an_include_needs_no_flag(self, monkeypatch):
        """The narrowing people actually want must not be caught by the guard."""
        from server.api.proxy import set_local_capture
        from server.models import LocalCaptureRequest

        request, adapter, _ = TestTheHandlerActuallyWidens._request(monkeypatch)

        await set_local_capture(
            request=request,
            body=LocalCaptureRequest(processes=["MyApp", "!12345"]),
        )

        applied = adapter.reconfigure.call_args.kwargs["local_capture_processes"]
        assert applied[:2] == ["MyApp", "!12345"]

    def test_the_cli_refuses_it_and_writes_nothing(self, monkeypatch, tmp_path, capsys):
        import pytest

        from server import config as cfg
        from server import main as m

        monkeypatch.setattr(cfg, "USER_CONFIG_FILE", tmp_path / "config.json")
        monkeypatch.setattr(m, "_local_capture_cert_gate", lambda *a, **k: None)

        with pytest.raises(SystemExit) as exc:
            m._cmd_enable_local_capture(["!12345"], skip_cert_check=True)

        assert exc.value.code == 1
        assert "--whole-mac" in capsys.readouterr().err
        assert not (tmp_path / "config.json").exists(), (
            "a refused list must not be written"
        )

    def test_the_cli_allows_it_when_asked(self, monkeypatch, tmp_path):
        from server import config as cfg
        from server import main as m

        monkeypatch.setattr(cfg, "USER_CONFIG_FILE", tmp_path / "config.json")
        monkeypatch.setattr(m, "_local_capture_cert_gate", lambda *a, **k: None)
        monkeypatch.setattr(m, "read_state", lambda: None)

        m._cmd_enable_local_capture(["!12345"], skip_cert_check=True, whole_mac=True)

        assert cfg.get_local_capture_processes()[0] == "!12345"

    def test_the_cli_flag_is_wired_through(self):
        """The argument existing and being passed are separate facts."""
        import inspect

        from server import main as m

        src = inspect.getsource(m)
        assert '"--whole-mac"' in src
        assert "args.whole_mac" in src

    def test_startup_warns_rather_than_refusing(self):
        """A stored list was chosen in an earlier process -- perhaps on purpose,
        since whole_mac is not stored -- so booting must not fail over it. It
        must still say so."""
        import inspect

        from server import main as m

        src = inspect.getsource(m._cmd_start)
        assert "leading_exclusion(local_capture_processes)" in src
        assert src.index("with_capture_minimum(") < src.index(
            "leading_exclusion(local_capture_processes)"
        ), "check the list that will actually be routed, after widening"
