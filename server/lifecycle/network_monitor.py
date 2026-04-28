"""Background network-change monitor.

Polls the Mac's current Wi-Fi SSID and outward-facing IP on a short
interval and surfaces changes through ``app.state.network_state``.

Why this exists: when the laptop running Quern travels with a set of
physical test devices, both the Mac and the devices auto-rejoin the
saved Wi-Fi at each location. The devices' stored proxy address may or
may not still be valid depending on whether the Mac's DHCP lease on
this network matches what was last recorded. The staleness check
(``wifi_proxy_stale`` in proxy_status) already computes this on demand,
but the user has to remember to ask. This module makes Quern *notice*
changes proactively so the next routine status call surfaces "the
network just changed at HH:MM" without anyone having to think about it.

Polling cadence is intentionally modest (15s) — change events are
rare and the syscall (``networksetup -getairportnetwork``) is cheap
but non-zero. If polling proves insufficient we can move to
``SCDynamicStore`` push events, but that's a much bigger lift.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from server.lifecycle.state import detect_current_ssid, detect_local_ip

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL = 15.0


@dataclass
class NetworkState:
    """Current and most recently observed Mac network identity.

    Kept in-memory only — cross-restart history lives in
    ``cert-state.json``'s per-device ``wifi_proxy_configs`` map.
    """

    ssid: str | None = None
    local_ip: str | None = None
    last_polled_at: str | None = None
    last_changed_at: str | None = None
    previous_ssid: str | None = None
    previous_local_ip: str | None = None
    # Why the most recent change fired, for debug visibility.
    last_change_reason: str | None = None
    recent_changes: list[dict] = field(default_factory=list)
    # True once the first poll has populated baseline values. Distinguishes
    # "we haven't seen anything yet" from "we've observed that there's no
    # Wi-Fi right now" — both manifest as ssid=None / local_ip=None.
    _initialized: bool = False

    def as_dict(self) -> dict:
        return {
            "ssid": self.ssid,
            "local_ip": self.local_ip,
            "last_polled_at": self.last_polled_at,
            "last_changed_at": self.last_changed_at,
            "previous_ssid": self.previous_ssid,
            "previous_local_ip": self.previous_local_ip,
            "last_change_reason": self.last_change_reason,
            "recent_changes": list(self.recent_changes),
        }


def _snapshot() -> tuple[str | None, str | None]:
    """Return (ssid, local_ip) for the current moment. Both can be None."""
    return detect_current_ssid(), detect_local_ip()


def _diff_reason(
    prev_ssid: str | None,
    prev_ip: str | None,
    new_ssid: str | None,
    new_ip: str | None,
) -> str | None:
    """Classify a change for human-readable logging.

    Returns None when nothing changed. The string captures the *kind*
    of change (SSID swap vs. DHCP-only) so an agent inspecting the
    state knows whether device proxy reconfig is likely needed.
    """
    if prev_ssid != new_ssid and prev_ip != new_ip:
        return "ssid_and_ip_changed"
    if prev_ssid != new_ssid:
        return "ssid_changed"
    if prev_ip != new_ip:
        return "ip_changed_same_ssid"  # DHCP lease changed on the same network
    return None


def update_network_state(state: NetworkState) -> bool:
    """Refresh ``state`` from a fresh snapshot. Returns True if changed.

    On a change, ``previous_*`` and ``last_changed_at`` are populated
    and a small entry is appended to ``recent_changes`` (capped at 10).
    """
    new_ssid, new_ip = _snapshot()
    now = datetime.now(UTC).isoformat()
    reason = _diff_reason(state.ssid, state.local_ip, new_ssid, new_ip)

    state.last_polled_at = now

    # Establish baseline on the very first poll, regardless of whether
    # we observed real values or (None, None). Subsequent polls compare
    # against this baseline and only fire change events for real diffs.
    if not state._initialized:
        state.ssid = new_ssid
        state.local_ip = new_ip
        state._initialized = True
        return False

    if reason is None:
        return False

    state.previous_ssid = state.ssid
    state.previous_local_ip = state.local_ip
    state.ssid = new_ssid
    state.local_ip = new_ip

    state.last_changed_at = now
    state.last_change_reason = reason
    state.recent_changes.append({
        "at": now,
        "reason": reason,
        "from_ssid": state.previous_ssid,
        "to_ssid": state.ssid,
        "from_local_ip": state.previous_local_ip,
        "to_local_ip": state.local_ip,
    })
    # Cap history. Ten is enough to debug "what just happened" without
    # turning the response into a journal.
    if len(state.recent_changes) > 10:
        state.recent_changes = state.recent_changes[-10:]
    return True


async def network_monitor_loop(
    state: NetworkState,
    interval: float = DEFAULT_POLL_INTERVAL,
) -> None:
    """Forever-loop polling task. Runs as part of the FastAPI lifespan.

    Cancellation-friendly: a CancelledError just exits the loop cleanly.
    Any other exception is logged and the loop continues — we'd rather
    miss a sample than tear down the whole server.
    """
    while True:
        try:
            changed = update_network_state(state)
            if changed:
                logger.info(
                    "Network change: %s (was ssid=%r ip=%r → now ssid=%r ip=%r)",
                    state.last_change_reason,
                    state.previous_ssid, state.previous_local_ip,
                    state.ssid, state.local_ip,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("network monitor poll failed (continuing)")
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise
