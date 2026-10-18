"""`quern session run|list|release`: held sessions from a shell or CI (#254).

A CI job made of shell steps can't hold a WebSocket, so `run` holds it for
them: it opens a session, holds its live socket, reserves devices, and runs a
command with the session in its environment --

    quern session run --reserve 2 --device-type simulator -- xcodebuild test ...

The child gets `QUERN_SESSION` (so its quern calls are attributed to the
session and act on its devices), `QUERN_SESSION_TOKEN` and `QUERN_DEVICES`
(the reserved udids, comma-separated). When the child exits, the session is
released cleanly. If `run` itself is killed, the server sees the socket drop
and holds the devices for the grace period, as for any crashed client.

Exit codes: the child's own when it ran; 1 when the server could not be
asked; 2 when it refused (a device held, nothing free, a bad argument); 4
when the child succeeded but the hold was lost while it ran -- the server
restarted, say -- so its devices were not protected for the whole run.

A dropped connection is reconnected with the session's resume token, inside
the server's grace period, so a blip costs nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

#: Exit code when the command succeeded but the session's hold was lost.
HOLD_LOST = 4

USAGE = """\
Usage: quern session run [--name NAME] [--reserve N | --udid UDID ... | --group GROUP]
                         [--device-type TYPE] [--os-version V] [--device-name NAME]
                         [--no-boot] [--on-release none|shutdown_booted] -- COMMAND ...
       quern session list
       quern session release SESSION_ID

`run` holds a session for COMMAND: it reserves the devices, exports
QUERN_SESSION, QUERN_SESSION_TOKEN and QUERN_DEVICES to it, and releases them
when it exits. Another caller changing a reserved device gets 423; reading it
still works. `--on-release shutdown_booted` shuts down the simulators the
session booted to satisfy the reservation.

`list` shows held sessions, their state (live, or in grace after a dropped
connection) and their devices. `release` ends one -- yours, or a stuck one.
"""


def _server() -> tuple[str, str]:
    from server.__main__ import _server_base_url
    from server.config import API_KEY_FILE, client_api_key

    url = _server_base_url()
    if url is None:
        raise ConnectionError("no server answering: start it with `quern start`")
    key = client_api_key()
    if not key:
        raise ConnectionError(f"no API key at {API_KEY_FILE} and no QUERN_CLIENT_KEY: "
                              "run `quern setup`")
    return url, key


def _call(method: str, url: str, key: str, path: str, session: str = "",
          body: dict | None = None) -> tuple[int, dict]:
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if session:
        headers["X-Quern-Session"] = session
    req = urllib.request.Request(url + path, method=method, headers=headers,
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read() or b"{}").get("detail", "")
        except ValueError:
            detail = ""
        return e.code, {"detail": detail or str(e)}
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise ConnectionError(f"the server did not answer: {e}") from e


def _detail(answer: dict) -> str:
    detail = answer.get("detail")
    if isinstance(detail, dict):
        return detail.get("message", json.dumps(detail))
    return str(detail)


class _Socket:
    """The session's live socket, held on a thread until `close()`.

    A connection that drops is reconnected with the resume token while the
    server's grace period lasts; it used to end silently, leaving the command
    running unprotected with nothing said (review). If the server no longer
    knows the session -- it restarted, or grace ran out -- `lost` is set and
    said on stderr at once."""

    def __init__(self, url: str, key: str, session: str, token: str,
                 grace_seconds: float = 120) -> None:
        self._url = "ws" + url[len("http"):] + f"/api/v1/sessions/{session}/live"
        self._headers = {"Authorization": f"Bearer {key}", "X-Quern-Session": session,
                         "X-Quern-Resume-Token": token}
        self._session = session
        self._grace = grace_seconds
        self._closing = False
        self.lost = False
        # Guards _ws against close() racing a reconnect, and the loss warning
        # against printing twice (review).
        self._lock = threading.Lock()
        self._ws = self._connect()
        self._thread = threading.Thread(target=self._hold, daemon=True)
        self._thread.start()

    def _connect(self):
        from websockets.sync.client import connect

        ws = connect(self._url, additional_headers=self._headers, open_timeout=10)
        first = json.loads(ws.recv(timeout=10))
        if first.get("event") != "attached":
            raise ConnectionError(f"the session socket said {first!r}, not attached")
        return ws

    def _hold(self) -> None:
        while not self._closing:
            code = None
            try:
                for _ in self._ws:          # events are informational; reading
                    pass                    # keeps pings answered
                code = getattr(self._ws, "close_code", None)
            except Exception as e:  # noqa: BLE001 -- any end is handled below
                code = getattr(getattr(e, "rcvd", None), "code", None)
            if self._closing:
                return
            if code in (4404, 4403, 4000):
                self._lose(code)
                return
            if not self._reconnect():
                return

    def _reconnect(self) -> bool:
        deadline = time.monotonic() + self._grace
        delay = 1.0
        while not self._closing and time.monotonic() < deadline:
            time.sleep(delay)
            try:
                ws = self._connect()
            except Exception as e:  # noqa: BLE001 -- retried until grace runs out
                code = getattr(getattr(e, "rcvd", None), "code", None)
                if code in (4404, 4403, 4000):
                    self._lose(code)
                    return False
                delay = min(delay * 2, 5.0)
                continue
            with self._lock:
                if self._closing:
                    # Reconnected after the command finished: close this one
                    # cleanly too, or the server holds the devices for the
                    # whole grace period after the run (review).
                    try:
                        ws.close(code=1000, reason="done")
                    except Exception:  # noqa: BLE001
                        pass
                    return False
                self._ws = ws
                return True
        if not self._closing:
            self._lose(None)
        return False

    def _lose(self, code) -> None:
        with self._lock:
            if self.lost:
                return
            self.lost = True
        why = {4404: "the server no longer knows it (it restarted, or its grace ran out)",
               4000: "another connection took it over",
               4403: "its resume token was refused",
               "unconfirmed": "the server could not be asked whether it still holds it",
               }.get(code, "the server could not be reached within its grace period")
        print(f"quern session run: WARNING: session {self._session} lost its hold -- {why}. "
              "Its devices are no longer reserved for this command.", file=sys.stderr)

    def close(self) -> None:
        """A clean close: the server releases the session at once."""
        with self._lock:
            self._closing = True
            ws = self._ws
        try:
            ws.close(code=1000, reason="done")
        except Exception:  # noqa: BLE001 -- already gone: the server's grace applies
            pass


def _run(args: argparse.Namespace) -> int:
    if not args.command:
        print("quern session run: give a command after --", file=sys.stderr)
        return 2
    from server.session import session_from_env

    url, key = _server()
    # Its own name, never the caller's: under Claude Code the caller's is the
    # agent's own MCP session, which a `run` took over or collided with
    # (review). Prefixed with it, so a reader can still tell whose run it was.
    base = session_from_env(dict(os.environ), fallback="cli")
    # 33 characters of it: with ".run-xxxx" that is 42, which leaves room for
    # a client key's `<label>:` prefix (up to 17) inside the server's 64.
    name = args.name or f"{base[:33]}.run-{secrets.token_hex(2)}"
    status, opened = _call("POST", url, key, "/api/v1/sessions", session=name,
                           body={"name": name} if name else {})
    if status != 200:
        print(f"quern session run: {_detail(opened)}", file=sys.stderr)
        return 2
    session, token = opened["id"], opened["resume_token"]
    try:
        socket = _Socket(url, key, session, token,
                         grace_seconds=opened.get("grace_seconds", 120))
    except Exception as e:  # noqa: BLE001 -- any failure to hold it is the same answer
        print(f"quern session run: could not hold the session's socket: {e}", file=sys.stderr)
        _call("DELETE", url, key, f"/api/v1/sessions/{session}", session=session)
        return 1
    try:
        body: dict = {"on_release": args.on_release, "auto_boot": not args.no_boot}
        if args.udid:
            body["udids"] = args.udid
        elif args.group:
            body["group"] = args.group
            if args.reserve:
                body["count"] = args.reserve
        else:
            body["count"] = args.reserve or 1
            for field, value in (("device_type", args.device_type),
                                 ("os_version", args.os_version), ("name", args.device_name)):
                if value:
                    body[field] = value
        status, reserved = _call("POST", url, key, f"/api/v1/sessions/{session}/reservations",
                                 session=session, body=body)
        if status != 200:
            print(f"quern session run: {_detail(reserved)}", file=sys.stderr)
            return 2
        udids = reserved["holding"]
        print(f"quern session run: session {session} holds {', '.join(udids)}", file=sys.stderr)
        env = {**os.environ, "QUERN_SESSION": session, "QUERN_SESSION_TOKEN": token,
               "QUERN_DEVICES": ",".join(udids)}
        rc = _run_child(args.command, env)
        holding = _still_holding(url, key, session, udids)
        if holding is not True:
            # The socket thread may still have been reconnecting when the
            # command ended -- a server restart takes seconds -- so ask the
            # server directly whether the hold survived (found live). "Could
            # not ask" is said as that, not as "gone" (review).
            socket._lose(4404 if holding is False else "unconfirmed")
        if socket.lost:
            print(f"quern session run: the command ran partly without its reservation "
                  f"(exit {rc if rc else HOLD_LOST})", file=sys.stderr)
            return rc if rc else HOLD_LOST
        return rc
    finally:
        socket.close()


def _run_child(command: list[str], env: dict[str, str]) -> int:
    """Run the command as its own job, forwarding SIGINT and SIGTERM to it
    exactly once, and return its status as the shell would report it.

    The child gets its own process group, as a shell's job does. A signal
    sent to this process -- or to the group it was started in, as CI runners
    do -- then reaches the child only through the forwarding below, so it
    arrives once. When there is a terminal, the child's group is made its
    foreground group: Ctrl-C goes to the child alone, from the terminal, and
    the child can still read input. Inferring "this came from the terminal"
    from foreground-group membership could not tell a terminal's Ctrl-C from
    a `kill -INT` aimed at this process, and dropped the latter (CodeRabbit);
    a terminal's Ctrl-C forwarded on top of its own was a second SIGINT,
    which xcodebuild and pytest read as "abort, skip cleanup".
    """
    tty = _terminal()
    try:
        child = subprocess.Popen(command, env=env, process_group=0)
    except OSError as e:
        # A traceback, before (CodeRabbit). The shell's statuses: 127 for a
        # command that isn't there, 126 for one that cannot be run.
        print(f"quern session run: cannot run {command[0]}: {e.strerror or e}", file=sys.stderr)
        return 127 if isinstance(e, FileNotFoundError) else 126
    group = child.pid                     # process_group=0: the child leads its group
    if tty is not None and _foreground(tty):
        _hand_terminal_to(tty, group)
        # A child that read the terminal before it was handed over was stopped
        # by SIGTTIN; the handover does not resume it (CodeRabbit).
        _signal_group(group, signal.SIGCONT)

    def forward(signum: int, _frame: object) -> None:
        # To the whole group, not just the child: descendants -- xcodebuild's
        # own children -- would otherwise outlive the hold (CodeRabbit).
        _signal_group(group, signum)

    previous = {sig: signal.signal(sig, forward) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        return _wait_as_a_shell(child.pid, group, tty)
    finally:
        # Put things back as soon as the child is gone: the hold check that
        # follows talks to the server, and must not run with Ctrl-C forwarded
        # to a process that no longer exists (CodeRabbit).
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        # Only if the child's group still holds the terminal: taking it when
        # this process is in the background would take it from the shell.
        if tty is not None and _owner(tty) == group:
            _hand_terminal_to(tty, os.getpgrp())


def _wait_as_a_shell(pid: int, group: int, tty: int | None) -> int:
    """Wait for the child, handling stops the way a shell's job control does,
    and return its status as the shell would report it.

    A stopped child -- Ctrl-Z, which the terminal sends only to its group now
    that it is the foreground job -- used to leave this waiting and the
    terminal held by a stopped group, with no way back but killing it from
    elsewhere (CodeRabbit). Now: take the terminal back and stop this process
    too, so the user's shell gets control; when it is continued (`fg`), hand
    the terminal back to the child and continue it."""
    while True:
        _, status = os.waitpid(pid, os.WUNTRACED)
        if os.WIFEXITED(status):
            return os.WEXITSTATUS(status)
        if os.WIFSIGNALED(status):
            return 128 + os.WTERMSIG(status)
        if os.WIFSTOPPED(status) and tty is not None:
            if _owner(tty) == group:
                _hand_terminal_to(tty, os.getpgrp())
            os.kill(os.getpid(), signal.SIGSTOP)    # until the shell continues us
            # Resumed: `fg` made this the foreground job, `bg` did not. Read
            # it now, not at start -- handing the terminal over after a `bg`
            # took it from the shell, and a CLI started with `&` never handled
            # a stop at all (CodeRabbit).
            if _foreground(tty):
                _hand_terminal_to(tty, group)
            _signal_group(group, signal.SIGCONT)


def _signal_group(group: int, signum: int) -> None:
    try:
        os.killpg(group, signum)
    except (ProcessLookupError, PermissionError):
        pass                              # already gone


def _terminal() -> int | None:
    """stdin's file descriptor when it is a terminal, foreground or not."""
    try:
        fd = sys.stdin.fileno()
        return fd if os.isatty(fd) else None
    except (OSError, ValueError):
        return None


def _owner(fd: int) -> int | None:
    """The terminal's foreground process group, or None if it can't be read."""
    try:
        return os.tcgetpgrp(fd)
    except OSError:
        return None


def _foreground(fd: int) -> bool:
    """Whether this process is in the terminal's foreground group."""
    return _owner(fd) == os.getpgrp()


def _hand_terminal_to(fd: int, pgrp: int) -> None:
    """Make `pgrp` the terminal's foreground group. SIGTTOU is ignored for the
    call: a background group changing the foreground is otherwise stopped."""
    old = signal.signal(signal.SIGTTOU, signal.SIG_IGN)
    try:
        os.tcsetpgrp(fd, pgrp)
    except OSError:
        pass                              # the terminal went away: nothing to hand over
    finally:
        signal.signal(signal.SIGTTOU, old)


def _still_holding(url: str, key: str, session: str, udids: list[str]) -> bool | None:
    """Whether the server still has `session`, holding every one of `udids`;
    None when it could not be asked."""
    try:
        status, answer = _call("GET", url, key, f"/api/v1/sessions/{session}", session=session)
    except ConnectionError:
        return None
    if status == 404:
        return False
    if status != 200:
        return None
    held = {r.get("udid") for r in answer.get("reservations") or []}
    return set(udids) <= held


def _list() -> int:
    url, key = _server()
    status, answer = _call("GET", url, key, "/api/v1/sessions")
    if status != 200:
        print(f"quern session list: {_detail(answer)}", file=sys.stderr)
        return 2
    sessions = answer.get("sessions") or []
    if not sessions:
        print("No held sessions.")
        return 0
    for s in sessions:
        devices = ", ".join(r["udid"] for r in s["reservations"]) or "no devices"
        state = s["state"]
        if state == "grace" and s.get("grace_remaining_s") is not None:
            state = f"grace, {int(s['grace_remaining_s'])}s left"
        print(f"{s['id']}  [{state}]  {devices}")
    return 0


def _release(session: str) -> int:
    url, key = _server()
    status, answer = _call("DELETE", url, key, f"/api/v1/sessions/{session}")
    if status != 200:
        print(f"quern session release: {_detail(answer)}", file=sys.stderr)
        return 2
    freed = ", ".join(answer.get("freed") or []) or "no devices"
    print(f"Released {session} ({freed})")
    return 0


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0 if argv else 2
    command: list[str] = []
    if "--" in argv:
        at = argv.index("--")
        argv, command = argv[:at], argv[at + 1:]
    parser = argparse.ArgumentParser(prog="quern session", add_help=False)
    sub = parser.add_subparsers(dest="what")
    run = sub.add_parser("run", add_help=False)
    run.add_argument("--name")
    choice = run.add_mutually_exclusive_group()
    choice.add_argument("--udid", action="append")
    choice.add_argument("--group")
    run.add_argument("--reserve", type=int)
    run.add_argument("--device-type",
                     choices=("simulator", "device", "android_emulator", "android_device"))
    run.add_argument("--os-version")
    run.add_argument("--device-name")
    run.add_argument("--no-boot", action="store_true")
    run.add_argument("--on-release", choices=("none", "shutdown_booted"), default="none")
    sub.add_parser("list", add_help=False)
    release = sub.add_parser("release", add_help=False)
    release.add_argument("session_id")
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        print(USAGE, file=sys.stderr)
        return 2
    try:
        if args.what == "run":
            args.command = command
            return _run(args)
        if args.what == "list":
            return _list()
        if args.what == "release":
            return _release(args.session_id)
    except ConnectionError as e:
        print(f"quern session: {e}", file=sys.stderr)
        return 1
    print(USAGE, file=sys.stderr)
    return 2
