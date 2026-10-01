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

Three things keep the set current:

- the adapter calls `compute_trusted` before every spawn (`trust_provider`),
  so no start path launches mitmdump with an old set;
- `refresh_loop` re-checks while local capture runs, which is what picks up a
  simulator booted, erased or given the CA outside quern;
- `refresh` is called straight after quern itself installs the CA or erases a
  simulator, so those take effect without waiting for the loop.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from server.models import SimulatorTls
from server.proxy.cert_preflight import simulator_trust

logger = logging.getLogger(__name__)

#: How often the set is re-checked while local capture runs. A check is one
#: device listing plus ~10 ms per booted simulator (ADR 1 in
#: docs/proposals/cert-trust-model.md), so this is cheap; it bounds how long a
#: simulator booted or given the CA outside quern stays passed through.
REFRESH_INTERVAL = 15.0

#: Mirrors the addon's sentinel for a simulator whose UDID is not known yet.
UNKNOWN_SIMULATOR = "unknown-simulator"


def _controller(app: Any):
    return getattr(app.state, "device_controller", None)


async def compute_trusted(app: Any) -> list[str] | None:
    """The UDIDs whose TLS may be decrypted; None means every simulator.

    Records what it found on ``app.state.simulator_trust`` for the report.
    When the device list cannot be read at all, trusts nobody: a previous set
    may name a simulator erased since, and decrypting that one is the mistake
    this exists to prevent.
    """
    controller = _controller(app)
    trust = await simulator_trust(controller)
    if trust is None and controller is not None:
        logger.warning(
            "Could not list simulators; passing every simulator's TLS through "
            "until the next check",
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
    trusted = await compute_trusted(app)
    await adapter.set_trusted_simulators(trusted)


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
        out.append(SimulatorTls(
            udid=udid,
            tls="passed_through",
            reason=(
                "a simulator not identified yet" if unknown
                else "booted since the last trust check; checked within "
                f"{int(REFRESH_INTERVAL)}s"
            ),
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
