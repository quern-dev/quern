"""Which simulators' TLS local capture decrypts, and which it passes through.

Local capture spans every simulator on the Mac, and one that does not trust the
mitmproxy CA fails every HTTPS request the proxy terminates. So the addon
decrypts TLS only from simulators this module has confirmed trust the CA, and
passes the rest through untouched (#354). Their network works; their HTTPS is
invisible to quern.

The addon is told the TRUSTED simulators, never the untrusted ones. Trust
changes under a running proxy -- a simulator boots, is created, or is erased --
and a simulator nobody has checked must land on the side that costs visibility
rather than the side that breaks it. With a trusted list a stale answer means
"passed through until the next refresh"; with an untrusted list it would mean
"decrypted and failing, silently".

Trust is bound to a simulator's *boot*, not just its UDID: the addon records
the launchd_sim pid each trusted UDID was running as when the set arrived. An
erase keeps the UDID and empties the TrustStore, and needs a reboot to be
used, so a rebooted simulator -- erased or not -- is a new launchd_sim and is
passed through until it has been checked again.

What keeps the set current:

- the adapter calls `compute_trusted` before every spawn (`trust_provider`),
  so no start path launches mitmdump with an old set;
- a passed-through connection from a simulator that is not known to be
  untrusted -- one rebooted, booted unseen, or not identified yet -- triggers
  a check at once (`on_passthrough`). That covers every way a simulator gets
  booted, inside quern or out, without the server having to see the boot;
- `refresh` runs straight after quern itself installs the CA, erases or boots
  a simulator;
- `refresh_loop` catches the one change nothing announces: the CA installed
  on a simulator from outside quern.

Refreshes are serialised (`_lock`), so an older answer can never land after a
newer one.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from server.models import SimulatorTls
from server.proxy.cert_preflight import simulator_trust

logger = logging.getLogger(__name__)

#: How often the set is re-checked while local capture runs. Events handle the
#: urgent cases (a boot, an erase, an install through quern), so this only has
#: to bound how long a CA installed from outside quern goes unnoticed. A check
#: shells out to openssl and writes cert state per booted simulator, so it is
#: not free to run every few seconds.
REFRESH_INTERVAL = 60.0

#: Minimum gap between event-driven checks, so a burst of passed-through
#: connections from one simulator costs one check, not one each.
EVENT_DEBOUNCE = 2.0

#: Mirrors the addon's sentinel for a simulator whose UDID is not known yet.
UNKNOWN_SIMULATOR = "unknown-simulator"


def _controller(app: Any):
    return getattr(app.state, "device_controller", None)


def _lock(app: Any) -> asyncio.Lock:
    lock = getattr(app.state, "sim_tls_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        app.state.sim_tls_lock = lock
    return lock


def install(app: Any, adapter: Any) -> None:
    """Wire an adapter to this module. Called once, by the lifespan.

    Every spawn then asks for the current set first -- so no start path (the
    lifespan, the endpoints, the watchdog) launches mitmdump with a stale list --
    and every passed-through connection can prompt a check.
    """
    app.state.decrypt_all_simulators = False
    adapter.trust_provider = lambda: compute_trusted(app)
    adapter.on_passthrough = lambda udid: on_passthrough(app, udid)


async def compute_trusted(app: Any) -> list[str] | None:
    """The UDIDs whose TLS may be decrypted; None means every simulator.

    Serialised with `refresh`, so the set handed to a spawning mitmdump is
    never older than one a concurrent refresh has already sent.
    """
    async with _lock(app):
        return await _compute(app)


async def _compute(app: Any) -> list[str] | None:
    """Records what it found on ``app.state.simulator_trust`` for the report.
    When the device list cannot be read at all, trusts nobody: a previous set
    may name a simulator erased since, and decrypting that one is the mistake
    this exists to prevent.
    """
    controller = _controller(app)
    trust = await simulator_trust(controller)
    failed = trust is None and controller is not None
    if failed and not getattr(app.state, "simulator_trust_failed", False):
        # Once per outage, not once per check.
        logger.warning(
            "Could not list simulators; passing every simulator's TLS through "
            "until a check succeeds",
        )
    if trust is None:
        app.state.simulator_trust = []
        app.state.simulator_trust_failed = controller is not None
    else:
        app.state.simulator_trust = trust
        app.state.simulator_trust_failed = False
    if getattr(app.state, "decrypt_all_simulators", False):
        return None
    return [t["udid"] for t in (trust or []) if t["trusted"] is True]


async def refresh(app: Any) -> None:
    """Re-check every booted simulator and give the addon the new set.

    Safe to call whenever: with the proxy stopped it updates the set the next
    spawn will start from.
    """
    adapter = getattr(app.state, "proxy_adapter", None)
    if adapter is None:
        return
    async with _lock(app):
        trusted = await _compute(app)
        await adapter.set_trusted_simulators(trusted)


def on_passthrough(app: Any, udid: str) -> None:
    """Re-check now when a passed-through simulator might deserve decryption.

    Called for every passed-through connection. A simulator the last check
    found untrusted is left to the periodic check -- re-asking on each of its
    connections would only repeat the answer. Anything else is unconfirmed:
    not identified yet, booted since the last check, or a trusted simulator
    that has rebooted and so has a new instance the addon will not decrypt
    until it is checked again. One check per `EVENT_DEBOUNCE`.
    """
    known = {t["udid"]: t["trusted"] for t in getattr(app.state, "simulator_trust", None) or []}
    if known.get(udid.upper()) is False:
        return
    loop = asyncio.get_running_loop()
    now = loop.time()
    if now - getattr(app.state, "sim_tls_last_event_check", -1e9) < EVENT_DEBOUNCE:
        return
    app.state.sim_tls_last_event_check = now
    task = asyncio.create_task(refresh_after(app, f"a connection from {udid[:8]}"))
    # Held so it is not collected mid-flight.
    app.state.sim_tls_event_task = task


async def refresh_after(app: Any, event: str) -> None:
    """`refresh`, for a call whose own outcome must not depend on it.

    After quern installs the CA or erases a simulator, the new answer should
    take effect now rather than at the next periodic check -- decryption
    starting, or a just-erased simulator ceasing to be trusted. But the install
    or erase succeeded either way, so a failure here is logged, not raised; the
    loop corrects it within `REFRESH_INTERVAL`.
    """
    adapter = getattr(app.state, "proxy_adapter", None)
    if adapter is None or not adapter.local_capture:
        return
    try:
        await refresh(app)
    except Exception:
        logger.exception("Refreshing the trusted-simulator set after %s failed", event)


async def refresh_loop(app: Any, interval: float = REFRESH_INTERVAL) -> None:
    """Keep the set current while local capture runs."""
    while True:
        await asyncio.sleep(interval)
        try:
            adapter = getattr(app.state, "proxy_adapter", None)
            if adapter is not None and adapter.is_running and adapter.local_capture:
                await refresh(app)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Refreshing the trusted-simulator set failed")


def _fix(udid: str) -> str:
    return (
        f"install_proxy_cert for {udid} (decryption starts without restarting "
        "the proxy), or set auto_install_cert"
    )


def report(app: Any) -> list[SimulatorTls] | None:
    """Per simulator, decrypted or passed through. None when local capture is off.

    Built from the last check plus what the addon has actually passed through,
    so a simulator booted since the last check still shows up the moment it
    makes a connection.
    """
    adapter = getattr(app.state, "proxy_adapter", None)
    if adapter is None or not adapter.local_capture:
        return None

    trusted = adapter.trusted_simulators
    counts = adapter.passthrough_counts()
    out: list[SimulatorTls] = []
    seen: set[str] = set()

    for t in getattr(app.state, "simulator_trust", None) or []:
        udid = t["udid"]
        seen.add(udid)
        c = counts.get(udid, {})
        decrypted = trusted is None or udid in trusted
        if decrypted and t["trusted"] is not True:
            reason = (
                "skip_cert_check was passed, so this simulator is decrypted even "
                "though it does not trust the CA: HTTPS from it will fail"
            )
            fix = None
        elif decrypted and c.get("connections"):
            # Trusted now, but some of its connections were passed through
            # before this boot was confirmed. Those stay undecrypted for as long
            # as they stay open, which for HTTP/2 can be the whole session.
            reason = (
                f"{c['connections']} connection(s) opened before this boot was "
                "confirmed were passed through; any still open stay undecrypted "
                "until they close"
            )
            fix = None
        elif decrypted:
            reason, fix = None, None
        elif t["trusted"] is False:
            reason, fix = "does not trust the mitmproxy CA", _fix(udid)
        else:
            reason, fix = "could not check whether it trusts the CA", _fix(udid)
        out.append(SimulatorTls(
            udid=udid,
            name=t.get("name"),
            tls="decrypted" if decrypted else "passed_through",
            reason=reason,
            fix=fix,
            connections_passed_through=c.get("connections", 0),
            last_host=c.get("last_host"),
        ))

    for udid, c in counts.items():
        if udid in seen:
            continue
        unknown = udid == UNKNOWN_SIMULATOR
        if getattr(app.state, "simulator_trust_failed", False):
            reason = (
                "could not list simulators, so every simulator is passed "
                "through until a check succeeds"
            )
        elif unknown:
            reason = "a simulator not identified yet"
        else:
            reason = "booted since the last trust check; it is checked on its first connection"
        out.append(SimulatorTls(
            udid=udid,
            tls="passed_through",
            reason=reason,
            fix=None if unknown else _fix(udid),
            connections_passed_through=c.get("connections", 0),
            last_host=c.get("last_host"),
        ))
    return out


def passthrough_note(app: Any, udid: str | None) -> str | None:
    """A note for a flow query filtered to a simulator whose TLS is passed through.

    Present even -- especially -- when the query returns nothing: zero flows
    from a passed-through simulator reads exactly like an app that made no
    requests, and the agent reading the result has no other way to know.
    """
    if not udid:
        return None
    for entry in report(app) or []:
        if entry.udid.upper() == udid.upper() and entry.tls == "passed_through":
            return (
                f"This simulator's TLS is passed through, not decrypted "
                f"({entry.reason}), so its HTTPS requests do not appear here. "
                + (f"To see them: {entry.fix}." if entry.fix else "")
            ).strip()
    return None
