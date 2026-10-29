"""Session proxies: capture apps the main proxy does not, without restarting it (#558).

Local capture is a Mac-wide list of process names, handed to mitmdump at launch,
and changing it restarts the main proxy: open connections drop, for everyone
(#562). So a session that wants another app captured on a simulator it holds
gets a *session proxy* instead -- a second mitmdump, in local mode only,
capturing just the names no proxy already captures.

Measured on a Mac with two simulators (mitmproxy 12.3, 2026-10-29):

- Proxies with different names coexist, each capturing only its own apps, and
  one starting or stopping never disturbed another's capture or connections.
- A name listed by two proxies goes to the older one; the newer one sees
  nothing, silently. So a name already captured by any proxy is served by it,
  never listed again.
- When the holder of a name stops, a proxy that also lists it takes it over
  within seconds, with no restart -- which is how a name handed back here
  reaches the main proxy if the owner has since added it there.

A session proxy follows the main adapter: it takes its rules (mocks, bypass,
intercept, trusted simulators) and reports into the same flow store, where
flows carry the simulator they came from -- so a session sees only its own
devices' traffic, whichever proxy carried it.

A proxy belongs to its names, not to a session: every session that asks for a
name shares the proxy capturing it, and the proxy stops when no claim names any
of its names. Claims are in memory. Their pids are written down too, because a
mitmdump left behind would hold its names and the next server's proxy would
get nothing -- though measured, a SIGKILLed server's mitmdumps (main and
session) exit by themselves within 0.5 s, when their pipes close. The record is
for one that does not, as a mitmdump outliving its worktree once did.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from server.sources.proxy import ADDON_PATH, ProxyAdapter

logger = logging.getLogger(__name__)


class SessionCaptureError(Exception):
    """A session proxy could not be started; the message says why."""


#: Session proxies one client key may be using at once, and names it may ask
#: for in one request (#566, CodeRabbit). Each proxy is a mitmdump, ~60-80 MB;
#: one held simulator bounds nothing here, since names are not per device.
#: The owner is not limited.
MAX_PROXIES_PER_CLIENT = 4
MAX_NAMES_PER_REQUEST = 16


class SessionCaptureLimit(SessionCaptureError):
    """A client key is using as many session proxies as it may."""

    def __init__(self, using: list[list[str]]):
        self.using = using
        super().__init__(
            f"this key is already using {len(using)} session proxies, the most it may: "
            + "; ".join(", ".join(names) for names in using)
            + ". Hand some back with remove_session_capture, or name processes an "
            "existing proxy already captures.")


class MainProxyStopped(SessionCaptureError):
    """The main proxy is not running: session proxies take its rules, and
    `stop_proxy` means nothing is captured."""


@dataclass
class Claim:
    """A principal's names, asked for its work on one device.

    Keyed by principal and device, not session: a key holds a device through
    whichever of its sessions holds it, and the hold is what a claim lasts
    for. Keyed by session, a claim made by one session and outlived by the
    key's hold through another could be released by neither (review)."""

    principal: str
    udid: str
    names: list[str] = field(default_factory=list)
    #: The session that last asked, for status only.
    session: str = ""


@dataclass
class SessionProxy:
    adapter: ProxyAdapter
    names: list[str]


class SessionProxies:
    """The session proxies following one main adapter."""

    def __init__(self, leader: ProxyAdapter, pid_file: Path,
                 make_adapter: Callable[[list[str]], ProxyAdapter] | None = None) -> None:
        self.leader = leader
        self.pid_file = pid_file
        self._make_adapter = make_adapter or self._default_adapter
        self._proxies: list[SessionProxy] = []
        self._claims: list[Claim] = []
        self._lock = asyncio.Lock()

    def _default_adapter(self, names: list[str]) -> ProxyAdapter:
        return ProxyAdapter(
            device_id=self.leader.device_id,
            on_entry=self.leader.on_entry,
            flow_store=self.leader.flow_store,
            listen_port=None,
            local_capture_processes=names,
        )

    # -- reading -------------------------------------------------------------

    def proxy_for(self, name: str) -> SessionProxy | None:
        return next((p for p in self._proxies if name in p.names), None)

    def claims(self, principal: str | None = None) -> list[Claim]:
        return [c for c in self._claims if principal is None or c.principal == principal]

    def describe(self, principal: str | None = None) -> list[dict]:
        """The running session proxies, for status. With `principal`, only
        those its claims use, and of those only its own names and claims: a
        shared proxy's other names are other principals' apps."""
        out = []
        for proxy in self._proxies:
            claims = [c for c in self._claims
                      if set(c.names) & set(proxy.names)
                      and (principal is None or c.principal == principal)]
            if principal is not None and not claims:
                continue
            shown = proxy.names if principal is None else [
                n for n in proxy.names if any(n in c.names for c in claims)]
            process = proxy.adapter._process
            out.append({
                "processes": list(shown),
                "running": proxy.adapter.is_running,
                "pid": getattr(process, "pid", None),
                "error": proxy.adapter._error,
                "udids": sorted({c.udid for c in claims}),
                "sessions": len({c.session for c in claims}),
            })
        return out

    # -- claiming ------------------------------------------------------------

    def using(self, principal: str) -> list[SessionProxy]:
        """The running session proxies `principal`'s claims use."""
        claimed = {n for c in self._claims if c.principal == principal for n in c.names}
        return [p for p in self._proxies
                if p.adapter.is_running and claimed & set(p.names)]

    async def claim(self, *, session: str, principal: str, udid: str,
                    names: list[str], max_proxies: int | None = None) -> dict:
        """Capture `names` for `session`'s work on `udid`.

        Returns which names the main proxy already captures, which an existing
        session proxy does, which a new one was started for, whether that one
        confirmed it was capturing with the rules applied, and which names this
        call added to the principal's claim on `udid` (`added`, for a caller
        that has to undo it). Raises `MainProxyStopped` when the main proxy is
        not running, `SessionCaptureLimit` when a new proxy would take
        `principal` past `max_proxies`, `SessionCaptureError` when the new
        proxy did not start.
        """
        async with self._lock:
            # Inside the lock: a stop_proxy that took it first has stopped the
            # main proxy, and a claim waiting behind it must not start a proxy
            # that would run on after "stopped" (review).
            if not self.leader.is_running:
                raise MainProxyStopped("the proxy is not running")
            await self._prune_dead()
            main = set(self.leader.local_capture_processes)
            by_main, shared, new = [], [], []
            for name in dict.fromkeys(names):
                if name in main:
                    by_main.append(name)
                elif self.proxy_for(name) is not None:
                    shared.append(name)
                else:
                    new.append(name)

            confirmed: bool | None = None
            if new:
                using = self.using(principal)
                # Shared proxies count too: the cap is on what a key keeps
                # running, and a shared one stops only when every user is done.
                after = len(using) + 1 + sum(
                    1 for p in self._proxies if p.adapter.is_running
                    and p not in using and set(p.names) & set(shared))
                if max_proxies is not None and after > max_proxies:
                    raise SessionCaptureLimit([list(p.names) for p in using])
                confirmed = await self._start(new)
            added = self._add_claim(Claim(principal, udid, [*shared, *new], session))
            return {"main_proxy": by_main, "shared": shared, "started": new,
                    "confirmed": confirmed, "added": added}

    async def _start(self, names: list[str]) -> bool:
        adapter = self._make_adapter(names)
        adapter.leader = self.leader
        # One lock for every proxy's rules: a rule change waits for a session
        # proxy's start, which sends it the rules, rather than slipping between
        # the start's send and its being listed as a follower.
        adapter._rules_lock = self.leader._rules_lock
        adapter.trust_provider = self.leader.trust_provider
        adapter.on_passthrough = self.leader.on_passthrough
        adapter.restart_confirm_timeout = self.leader.restart_confirm_timeout
        # Listed before it starts, so no rule change can fall between its start
        # sending the rules and its being listed: before it runs a broadcast to
        # it does nothing, and the start sends the rules as they are by then.
        self.leader.followers.append(adapter)
        before = Counter(adapter._addon_events)
        try:
            await adapter.start()
        except Exception as e:
            # It may have spawned before failing. Left running and unlisted,
            # it would hold its names with nothing to stop it.
            await self._abandon(adapter)
            raise SessionCaptureError(f"the session proxy failed to start: {e}") from e
        if not adapter.is_running:
            await self._abandon(adapter)
            raise SessionCaptureError(
                adapter._error or "the session proxy did not start")
        self._proxies.append(SessionProxy(adapter, list(names)))
        self._write_pids()
        return await adapter._serving_with_rules(before)

    async def _abandon(self, adapter: ProxyAdapter) -> None:
        if adapter in self.leader.followers:
            self.leader.followers.remove(adapter)
        try:
            await adapter.stop()
        except Exception:  # noqa: BLE001 -- already failing; say so and go on
            logger.exception("Could not stop a session proxy that failed to start")

    def _add_claim(self, claim: Claim) -> list[str]:
        """Merge `claim` in; returns the names it added."""
        for existing in self._claims:
            if (existing.principal, existing.udid) == (claim.principal, claim.udid):
                added = [n for n in claim.names if n not in existing.names]
                existing.names = [*existing.names, *added]
                existing.session = claim.session
                return added
        if claim.names:
            self._claims.append(claim)
        return list(claim.names)

    # -- releasing -----------------------------------------------------------

    async def release(self, *, principal: str, udid: str | None = None,
                      names: list[str] | None = None) -> list[str]:
        """Drop `principal`'s claims -- on `udid`, and only `names`, when given
        -- and stop every session proxy no claim uses any more. Returns the
        names whose proxies stopped."""
        async with self._lock:
            for claim in self._claims:
                if claim.principal != principal:
                    continue
                if udid is not None and claim.udid != udid:
                    continue
                claim.names = [] if names is None else [
                    n for n in claim.names if n not in names]
            self._claims = [c for c in self._claims if c.names]
            return await self._stop_unclaimed()

    async def stop_all(self) -> list[str]:
        """Stop every session proxy and forget every claim: the main proxy is
        stopping, or the server is."""
        async with self._lock:
            self._claims.clear()
            return await self._stop_unclaimed()

    async def _stop_unclaimed(self) -> list[str]:
        claimed = {n for c in self._claims for n in c.names}
        stopped: list[str] = []
        for proxy in list(self._proxies):
            if claimed & set(proxy.names):
                continue
            await self._stop(proxy)
            stopped.extend(proxy.names)
        return stopped

    async def _stop(self, proxy: SessionProxy) -> None:
        try:
            await proxy.adapter.stop()
        except Exception:  # noqa: BLE001 -- it is being forgotten either way
            logger.exception("Could not stop the session proxy for %s", proxy.names)
        if proxy.adapter in self.leader.followers:
            self.leader.followers.remove(proxy.adapter)
        self._proxies.remove(proxy)
        self._write_pids()

    async def _prune_dead(self) -> None:
        """Forget proxies whose mitmdump has gone, so their names can be
        started again. Their claims stay: the names are still wanted."""
        for proxy in list(self._proxies):
            if not proxy.adapter.is_running:
                logger.warning("Session proxy for %s had stopped (%s)",
                               proxy.names, proxy.adapter._error or "no error recorded")
                await self._stop(proxy)

    # -- leftovers from a server that died ---------------------------------

    def _write_pids(self) -> None:
        pids = [p.adapter._process.pid for p in self._proxies
                if p.adapter._process is not None]
        try:
            if pids:
                self.pid_file.write_text(json.dumps({"pids": pids}))
            else:
                self.pid_file.unlink(missing_ok=True)
        except OSError:
            logger.exception("Could not record the session proxies' pids")

    @staticmethod
    def clean_up_leftovers(pid_file: Path) -> list[int]:
        """Stop session mitmdumps a previous server left running. Left alone,
        each keeps the names it captures, and a proxy started later for them
        gets nothing. Only a pid whose command line is still a quern
        local-mode-only mitmdump is touched: pids are reused. Never raises."""
        try:
            pids = json.loads(pid_file.read_text()).get("pids", [])
        except (OSError, ValueError, AttributeError):
            return []
        stopped = []
        for pid in pids:
            try:
                cmd = subprocess.run(["ps", "-p", str(int(pid)), "-o", "command="],
                                     capture_output=True, text=True, timeout=5).stdout
            except Exception:  # noqa: BLE001 -- best effort
                continue
            if "mitmdump" not in cmd or str(ADDON_PATH) not in cmd \
                    or "--mode local:" not in cmd or "regular@" in cmd:
                continue
            try:
                os.kill(int(pid), signal.SIGTERM)
                stopped.append(int(pid))
            except OSError:
                pass
        pid_file.unlink(missing_ok=True)
        if stopped:
            logger.warning("Stopped session proxies left by a previous server: %s", stopped)
        return stopped
