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
import contextlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
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
    """The UDIDs whose TLS may be decrypted.

    Always a list now. `skip_cert_check` used to answer None, "decrypt every
    simulator" -- including one this very check had just found does not trust
    the CA, which is the one simulator whose every HTTPS request is certain to
    fail (#414). It now widens the list to every booted simulator except those,
    so it still covers what it is for: a CA that is installed but could not be
    verified.

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
        _note_newly_trusted(app, trust)
        app.state.simulator_trust = trust
        app.state.simulator_trust_failed = False
    if getattr(app.state, "decrypt_all_simulators", False):
        # Everything not known to fail. A simulator booted after this check is
        # picked up on its first connection (`on_passthrough`); one that cannot
        # be attributed at all stays passed through, the safe side.
        return [t["udid"] for t in (trust or []) if t["trusted"] is not False]
    return [t["udid"] for t in (trust or []) if t["trusted"] is True]


def _note_newly_trusted(app: Any, trust: list[dict]) -> None:
    """When each simulator was last found to trust the CA after not doing so,
    so a certificate refusal from before that is not reported as current."""
    before = {t["udid"]: t["trusted"] for t in getattr(app.state, "simulator_trust", None) or []}
    since = getattr(app.state, "sim_trusted_since", None)
    if not isinstance(since, dict):
        since = {}
        app.state.sim_trusted_since = since
    now = datetime.now(UTC).isoformat()
    for t in trust:
        if t["trusted"] is True and before.get(t["udid"]) is not True:
            since[t["udid"].upper()] = now


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
        if decrypted and t["trusted"] is False:
            # `compute_trusted` no longer produces this (#414); kept for any set
            # handed to the addon some other way, because it must not go quiet.
            reason = (
                "decrypted though it does not trust the CA: HTTPS from it will fail"
            )
            fix = _fix(udid)
        elif decrypted and t["trusted"] is not True:
            reason = (
                "skip_cert_check was passed, so this simulator is decrypted though "
                "the check could not tell whether it trusts the CA: if it does "
                "not, HTTPS from it fails"
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


class CaptureNotReady(Exception):
    """A capture of one simulator was asked for and cannot capture (#414).

    Carries the structured body and the status the API returns: 428 when the
    simulator does not trust the CA, 500 when `auto_install_cert` is set and
    installing it failed -- the same split `_ensure_ca_is_trusted` makes.
    """

    def __init__(self, detail: dict, status_code: int = 428):
        super().__init__(detail.get("message", "capture is not ready"))
        self.detail = detail
        self.status_code = status_code


@dataclass
class CaptureCheck:
    """What a capture start learned about its simulator, for the response."""

    #: What is in effect for it now; None when there was nothing to check.
    entry: SimulatorTls | None = None
    #: What the caller should know: a check that could not run, a simulator
    #: whose HTTPS will fail. Never empty when the check did not complete.
    warnings: list[str] = field(default_factory=list)


def _not_ready(udid: str, name: str | None, *, system_proxy: bool,
               install_error: str | None) -> dict:
    who = name or udid
    if system_proxy:
        message = (f"{who} does not trust the mitmproxy CA, and the system proxy "
                   "routes its traffic through quern: every HTTPS request from it "
                   "would fail, and none be recorded.")
    else:
        message = (f"{who} does not trust the mitmproxy CA, so its HTTPS would be "
                   "passed through and none of it recorded.")
    if install_error:
        message += f" auto_install_cert is set, but installing it failed: {install_error}"
    resolutions = [
        {"action": "install_proxy_cert",
         "detail": "Install the CA on this simulator, then start again."},
        {"action": "set_auto_install_cert",
         "detail": ("Set auto_install_cert in ~/.quern/config.json, and a start "
                    "installs it when it is missing.")},
    ]
    if system_proxy:
        resolutions.append(
            {"action": "set_local_capture",
             "detail": ("Capture with local capture instead of the system proxy: a "
                        "simulator that does not trust the CA is passed through there.")})
    resolutions.append(
        {"action": "allow_passthrough",
         "detail": ("Start anyway with allow_passthrough. " + (
             "Under the system proxy its HTTPS then fails for the whole run."
             if system_proxy else "Its apps work, and its HTTPS is not captured."))})
    return {"error": "capture_without_cert", "message": message,
            "devices": [{"udid": udid, "name": who}], "resolutions": resolutions}


def _system_proxy_configured() -> bool:
    try:
        from server.lifecycle.state import read_state
        return bool((read_state() or {}).get("system_proxy_configured"))
    except Exception:
        return False


#: Why a simulator the server meant to decrypt is reported passed through: the
#: addon has not confirmed the set yet, or confirmed one without it.
UNCONFIRMED = ("the proxy has not yet confirmed it decrypts this simulator: its requests "
               "are passed through until it does")
NOT_TAKEN = "the proxy did not take this simulator into its decrypted set"
#: Why a simulator that has stopped trusting the CA may still be decrypted: the
#: addon kept it, or has not confirmed the set without it yet.
STILL_DECRYPTS = ("the proxy still decrypts this simulator, which no longer trusts the "
                  "CA: its HTTPS requests fail until it stops")
UNCONFIRMED_REMOVAL = ("the proxy has not yet confirmed it stopped decrypting this "
                       "simulator, which no longer trusts the CA: its HTTPS requests fail "
                       "until it does")


async def ensure_capturable(
    app: Any, udid: str, *, allow_passthrough: bool = False,
) -> CaptureCheck:
    """Before a capture of one simulator says "started", make it able to capture.

    A recording or capture session that starts against a simulator without the
    CA used to report started and record nothing -- or, decrypted anyway, have
    every request fail for the whole run (#414, #412). So:

    - ask the simulator itself whether it trusts the CA, never a record of it;
    - install the CA if `auto_install_cert` says to, then ask again;
    - otherwise refuse (`CaptureNotReady`, 428), unless the caller accepts the
      consequence with `allow_passthrough` -- passed through under local
      capture, failing under the system proxy;
    - refresh the trusted set only when it is stale for this simulator -- left
      out though it trusts the CA, or still in though it no longer does (the
      latter even on a refusal) -- so a start does not make the addon rebind
      every simulator it decrypts, and report what the addon itself confirms,
      not what was sent.

    The check runs under local capture and under a configured system proxy --
    the two ways a simulator's traffic reaches quern. A check that cannot run
    lets the start through and says so in `warnings`: a gate that fails closed
    blocks capture over its own bug, and one that is silent reads as passed.
    """
    check = CaptureCheck()
    #: What the addon confirmed, where it differs from the set that was sent:
    #: the report reads the sent set (review).
    override: tuple[str, str] | None = None
    adapter = getattr(app.state, "proxy_adapter", None)
    controller = _controller(app)
    local = adapter is not None and bool(adapter.local_capture)
    system_proxy = not local and _system_proxy_configured()
    if adapter is None or controller is None or not (local or system_proxy):
        return check
    from server.models import DeviceType
    from server.proxy import cert_manager

    try:
        await controller._ensure_device_type_cached(udid)
        is_simulator = controller._device_type(udid) == DeviceType.SIMULATOR
    except Exception as e:
        check.warnings.append(
            f"could not tell what device {udid} is ({e}), so whether it trusts the "
            "CA was not checked")
        return check
    if not is_simulator:
        return check
    if not cert_manager.get_cert_path().exists():
        check.warnings.append(
            "the proxy has not created its CA yet (it does on first start), so this "
            "simulator's trust could not be checked")
        return check

    try:
        trusted: bool | None = bool(await cert_manager.is_cert_installed(controller, udid))
    except Exception as e:
        check.warnings.append(f"could not check whether {udid} trusts the CA: {e}")
        trusted = None

    installed = False
    listed = adapter.trusted_simulators
    # In the set the addon was given -- None is every simulator -- though the
    # simulator has just said it does not trust the CA: trust removed under a
    # running proxy, which until the next periodic check went on decrypting it,
    # failing every request, whatever this start decides (review).
    revoked = local and trusted is False and (listed is None or udid.upper() in listed)
    if trusted is False:
        from server.config import get_auto_install_cert

        install_error = None
        if get_auto_install_cert():
            try:
                await cert_manager.install_cert(controller, udid)
                installed = True
                trusted = bool(await cert_manager.is_cert_installed(controller, udid))
                logger.info("Installed the CA on %s before capturing it", udid[:8])
            except Exception as e:
                install_error = str(e)
                logger.warning("Installing the CA on %s failed: %s", udid[:8], e)
        if trusted is False and not allow_passthrough:
            if revoked:
                # Refused or not, it must stop being decrypted now.
                await refresh_after(app, f"{udid[:8]} no longer trusting the CA")
            name = None
            with contextlib.suppress(Exception):
                name = await cert_manager._get_device_name(controller, udid)
            raise CaptureNotReady(
                _not_ready(udid, name, system_proxy=system_proxy, install_error=install_error),
                status_code=500 if install_error else 428,
            )
        if trusted is False and system_proxy:
            check.warnings.append(
                f"{udid} does not trust the CA and the system proxy is on: its HTTPS "
                "requests fail for as long as this runs")

    if local:
        stale = (trusted is True and udid.upper() not in (listed or [])) or revoked
        if stale or installed:
            seq = adapter.addon_trust_seq
            try:
                await refresh(app)
            except Exception:
                logger.exception("Refreshing the trusted set before capturing %s failed",
                                 udid[:8])
            if adapter.is_running and trusted is True:
                confirmed = await adapter.addon_decrypts(udid, after_seq=seq)
                # Unconfirmed, "decrypted" is what was asked for, not what is in
                # effect -- and passed through is the side that costs
                # visibility, not requests.
                if confirmed is None:
                    override = ("passed_through", UNCONFIRMED)
                elif confirmed is False:
                    override = ("passed_through", NOT_TAKEN)
            elif adapter.is_running and revoked:
                confirmed = await adapter.addon_decrypts(udid, after_seq=seq)
                if confirmed is True:
                    override = ("decrypted", STILL_DECRYPTS)
                elif confirmed is None:
                    override = ("decrypted", UNCONFIRMED_REMOVAL)
        if override:
            check.warnings.append(override[1])
        check.entry = next((e for e in report(app) or []
                            if e.udid.upper() == udid.upper()), None)
        if override and check.entry is not None:
            check.entry = check.entry.model_copy(
                update={"tls": override[0], "reason": override[1]})
        if trusted is False and check.entry is None:
            # The report lists booted simulators only, so a start against one
            # that is shut down has no entry to warn from -- and once it boots,
            # its HTTPS is passed through for the whole run, unannounced.
            check.warnings.append(
                f"{udid} does not trust the CA: once it boots, its HTTPS is passed "
                "through, not captured")
    return check


def _trusted_since(app: Any, udid: str) -> str | None:
    return (getattr(app.state, "sim_trusted_since", None) or {}).get(udid.upper())


def rejection_note(app: Any, udid: str | None, since: str | None = None,
                   until: str | None = None) -> str | None:
    """Say so when a simulator refused the proxy's certificate, in a window.

    A refusal is the one thing that turns capture from "missing traffic" into
    "failing requests": the app's HTTPS fails, and the failures are not flows,
    so a flow query or a recording shows nothing at all. Reported, never acted
    on -- changing trust from a rejection is #149's question.

    Only rejections inside the window, and none from before the simulator was
    last found to trust the CA: installing it needs no restart (#354), and a
    note still blaming the certificate after the fix would send the reader back
    to a problem that is solved.
    """
    adapter = getattr(app.state, "proxy_adapter", None)
    if not udid or adapter is None:
        return None
    floor = max(filter(None, (since, _trusted_since(app, udid))), default=None)
    hits = [
        r for r in getattr(adapter, "_tls_rejections", ())
        if (r.simulator_udid or "").upper() == udid.upper()
        and (floor is None or (r.last_at or "") >= floor)
        and (until is None or (r.first_at or "") <= until)
    ]
    if not hits:
        return None
    count = sum(r.count for r in hits)
    # A record collapses repeats per host, so one that began before the window
    # counts some refusals from outside it.
    partial = floor is not None and any((r.first_at or "") < floor for r in hits)
    hosts = sorted({r.sni for r in hits if r.sni})[:5]
    return (
        f"This simulator refused the proxy's certificate {count} time(s)"
        + (" (some of them before this window)" if partial else "")
        + (f" (hosts: {', '.join(hosts)})" if hosts else "")
        + f", last at {max(r.last_at or '' for r in hits)}: those HTTPS requests failed "
        f"and are not flows. {_fix(udid)}."
    )
