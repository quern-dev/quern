"""An error names the backend that produced it, and carries its message.

Two defects with one root: `controller_ui.py` is a dispatcher that routes to
four backends, and it knew which one it had picked everywhere except in its
error messages.

**#186** — `tool="idb"` was hardcoded at ten sites, and three more chose
between `wda` and `idb` with sim-bridge missing from the choice. So an Android
device, a physical iPhone, or a simulator driven by sim-bridge all reported
failures as `[idb]`. Not cosmetic: on Xcode 27 idb is genuinely broken, so an
`[idb]` label on an error sim-bridge produced sent the reader to debug a tool
that was never involved.

**#178** — `sim_bridge.py` raised bare `RuntimeError`, while `api/device_ui.py`
catches `DeviceError`. So the message the server had already composed was
discarded and the caller got `500 Internal Server Error` with no body, while
the real reason sat in the server log where the agent driving quern cannot see
it.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from server.device.controller import DeviceController
from server.device.idb import IdbBackend
from server.device.sim_bridge import SimBridgeBackend
from server.device.u2_client import U2Backend
from server.device.wda_client import WdaBackend
from server.models import DeviceError


class TestTheNameComesFromTheBackend:
    """Read off whichever backend was selected, never re-decided."""

    def test_every_backend_declares_one(self):
        for cls in (SimBridgeBackend, IdbBackend, U2Backend, WdaBackend):
            assert getattr(cls, "TOOL_NAME", None), f"{cls.__name__} has no TOOL_NAME"

    def test_the_names_are_distinct(self):
        """Two backends sharing a name would make the label useless in exactly
        the case it exists for."""
        names = [
            c.TOOL_NAME
            for c in (SimBridgeBackend, IdbBackend, U2Backend, WdaBackend)
        ]
        assert len(set(names)) == len(names), names

    @pytest.mark.parametrize(
        ("android", "physical", "sim_bridge_ok", "expected"),
        [
            (True, False, False, "u2"),
            (True, False, True, "u2"),       # Android never reaches sim-bridge
            (False, True, True, "wda"),      # physical never reaches sim-bridge
            (False, False, True, "sim-bridge"),
            (False, False, False, "idb"),    # the fallback, and only then
        ],
    )
    def test_it_matches_the_backend_actually_selected(
        self, android, physical, sim_bridge_ok, expected,
    ):
        """The property that matters: `_backend_name` and `_ui_backend` cannot
        disagree, because the first is derived from the second.

        Parametrised over every routing case rather than the one that was
        broken — the bug was that a second copy of this if-chain had drifted,
        so a test covering one branch would not have caught it.
        """
        ctrl = DeviceController()
        ctrl._is_android = lambda udid: android
        ctrl._is_physical = lambda udid: physical
        ctrl._sim_bridge_ok = sim_bridge_ok

        assert ctrl._backend_name("any-udid") == expected
        assert ctrl._ui_backend("any-udid").TOOL_NAME == expected

    def test_an_unknown_backend_does_not_invent_a_name(self):
        """A backend with no TOOL_NAME reports `unknown` rather than borrowing
        a real tool's name, which is the failure being fixed."""
        ctrl = DeviceController()
        ctrl._ui_backend = lambda udid: object()

        assert ctrl._backend_name("any-udid") == "unknown"


class TestTheDispatcherDoesNotHardcodeAToolName:
    """The guard, because the fix is twelve edits and the next one is easy to
    miss. This is the shape that kept #267 broken across six endpoints while
    three were fixed individually."""

    #: Resolved from this file, not from cwd. Every other test module here
    #: does the same; a cwd-relative path reads whichever tree pytest happens
    #: to be launched from, which during mutation testing is not the tree
    #: under test.
    SOURCE = (
        Path(__file__).resolve().parents[1] / "server" / "device" / "controller_ui.py"
    )

    #: Names of actual UI backends. `web-content` and the like are different
    #: subsystems, not backends the dispatcher chooses between, so a literal
    #: there is correct.
    BACKEND_NAMES = {"idb", "sim-bridge", "wda", "u2"}

    @staticmethod
    def _hardcoded_backend_names(source, names):
        """Backend names written as literals in *code*, found via the AST.

        Not a regex over the text. A regex cannot tell code from commentary,
        and this file and the dispatcher both quote `tool="idb"` in prose
        while explaining the bug -- so a text scan reports the explanation as
        the offence. It also has to see two syntactic forms: the third `wda`/
        `idb` ternary escaped both the original grep and the first version of
        this guard by being an assignment (`tool = "wda" if ...`) rather than
        a keyword argument.
        """
        import ast

        found = []

        def note(lineno, value):
            if isinstance(value, ast.Constant) and value.value in names:
                found.append((lineno, value.value))

        def note_expr(lineno, value):
            note(lineno, value)
            if isinstance(value, ast.IfExp):
                note(lineno, value.body)
                note(lineno, value.orelse)

        for node in ast.walk(ast.parse(source)):
            for kw in getattr(node, "keywords", None) or []:
                if kw.arg == "tool":
                    note_expr(getattr(node, "lineno", 0), kw.value)
            if isinstance(node, ast.Assign) and any(
                isinstance(tgt, ast.Name) and tgt.id == "tool" for tgt in node.targets
            ):
                note_expr(node.lineno, node.value)
        return found

    def test_no_backend_name_is_written_as_a_literal(self):
        offenders = self._hardcoded_backend_names(
            self.SOURCE.read_text(encoding="utf-8"), self.BACKEND_NAMES,
        )

        assert not offenders, (
            "a backend name is hardcoded in the dispatcher; use "
            f"_backend_name(...) so it names the backend that ran: {offenders}"
        )

    @pytest.mark.parametrize(
        "snippet",
        [
            'raise DeviceError("boom", tool="idb")',
            'raise DeviceError("boom", tool="u2")',
            'raise DeviceError("boom", tool="wda")',
            'raise DeviceError("boom", tool="sim-bridge")',
            'tool = "wda" if physical else "idb"',
            'raise DeviceError("boom", tool="wda" if physical else "idb")',
        ],
    )
    def test_the_check_can_find_every_form(self, snippet):
        """The control, across every name and both spellings.

        The first version matched `[a-z-]+`, which silently excluded `u2` --
        the digit -- so any site could be reverted to `tool="u2"` with all
        fifteen tests still green. Measured by mutation, not supposed. A
        control that only produces its negative for one member of a set is
        decoration for the rest.
        """
        assert self._hardcoded_backend_names(snippet, self.BACKEND_NAMES), (
            f"the detector cannot see a hardcoded name in {snippet!r}"
        )

    def test_prose_is_not_mistaken_for_code(self):
        """The inverse control: a docstring explaining the bug is not an
        instance of it."""
        prose = "\n".join([
            "def f():",
            '    "Once read tool=\'idb\' regardless of which backend ran."',
            '    # and tool="u2" is mentioned here too',
            "    raise DeviceError('boom', tool=self._backend_name(udid))",
            "",
        ])

        assert not self._hardcoded_backend_names(prose, self.BACKEND_NAMES)

    def test_the_source_file_was_actually_read(self):
        """And that it read the real file rather than an empty string."""
        source = self.SOURCE.read_text(encoding="utf-8")

        assert "_backend_name" in source
        assert len(source) > 10_000, "the dispatcher source looks truncated"


class TestASimBridgeFailureReachesTheCaller:
    """#178. The message existed; the API layer just never saw it."""

    def test_it_raises_device_error_not_runtime_error(self):
        """`api/device_ui.py` catches `DeviceError`. A `RuntimeError` walks
        past that handler into FastAPI's default 500, which has no body — so
        the reason is written to the server log and nowhere the caller looks.
        """
        import asyncio

        backend = SimBridgeBackend.__new__(SimBridgeBackend)
        backend._mgr = MagicMock()
        backend._mgr.send = AsyncMock(
            return_value={"ok": False, "error": "tap failed"},
        )

        with pytest.raises(DeviceError) as caught:
            asyncio.run(backend._send_admitted({"cmd": "tap"}))

        assert not isinstance(caught.value, RuntimeError), (
            "still a RuntimeError, which the API layer does not catch"
        )

    def test_the_reason_survives_into_the_error(self):
        """The whole point: `tap failed` has to reach the caller, not just
        the log."""
        import asyncio

        backend = SimBridgeBackend.__new__(SimBridgeBackend)
        backend._mgr = MagicMock()
        backend._mgr.send = AsyncMock(
            return_value={"ok": False, "error": "tap failed"},
        )

        with pytest.raises(DeviceError, match="tap failed"):
            asyncio.run(backend._send_admitted({"cmd": "tap"}))

    def test_it_names_sim_bridge_and_not_idb(self):
        """Both issues meeting: the error the caller finally receives is
        labelled with the tool that actually failed."""
        import asyncio

        backend = SimBridgeBackend.__new__(SimBridgeBackend)
        backend._mgr = MagicMock()
        backend._mgr.send = AsyncMock(
            return_value={"ok": False, "error": "tap failed"},
        )

        with pytest.raises(DeviceError) as caught:
            asyncio.run(backend._send_admitted({"cmd": "tap"}))

        assert caught.value.tool == "sim-bridge"
        assert caught.value.tool != "idb"

    def test_the_manager_and_the_backend_agree_on_the_spelling(self):
        """The manager raises before any backend exists, so it cannot read
        `TOOL_NAME` off one. Two literals would let those drift apart."""
        from server.device.sim_bridge import _TOOL

        assert SimBridgeBackend.TOOL_NAME == _TOOL
