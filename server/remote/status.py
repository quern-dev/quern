"""What the remote quern servers this machine is paired with look like now.

`quern connect` records each remote -- its URL, certificate fingerprint and
client key -- and `quern remote list` used to print just that record, which
says nothing about whether the server is up, what it runs, or what it could
lend. This asks each one, with the key it was paired with and the pinned
certificate: whether it answers, its version, and its devices, with which are
free. It reads what a client key may already read and adds no exposure.

Each remote is asked in parallel with a short timeout, so one that is down
costs one timeout, not one per remote. A remote that could not be asked says
why; it is never reported as having no devices.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from server.remote import client

#: Per request. A remote on the LAN answers in milliseconds; one that is off
#: is a connection timeout, and the list must not hang on it.
TIMEOUT_SECONDS = 4.0
#: The session a status read names: a client key must name one, even to read.
_SESSION = "quern-remote-status"


def _get(remote: client.Remote, path: str, timeout: float) -> Any:
    req = urllib.request.Request(remote.url + path, headers={
        "Authorization": f"Bearer {remote.key}", "X-Quern-Session": _SESSION})
    with client.opener(remote).open(req, timeout=timeout) as resp:
        return json.loads(resp.read() or b"null")


def _reason(e: BaseException) -> str:
    if isinstance(e, client.PinMismatch):
        return f"certificate changed: {e}"
    if isinstance(e, urllib.error.HTTPError):
        if e.code in (401, 403):
            return f"refused its key (HTTP {e.code}): it was revoked, or the server was reset"
        return f"HTTP {e.code}"
    if isinstance(e, urllib.error.URLError):
        reason = e.reason
        if isinstance(reason, client.PinMismatch):
            return f"certificate changed: {reason}"
        return f"not reachable ({reason})"
    if isinstance(e, TimeoutError):
        return "not reachable (timed out)"
    return f"could not be asked ({type(e).__name__}: {e})"


def probe(remote: client.Remote, local_version: str | None,
          timeout: float = TIMEOUT_SECONDS) -> dict:
    """One remote's state now. Never raises."""
    out: dict = {"name": remote.name, "url": remote.url, "label": remote.label,
                 "reachable": False, "version": None, "version_matches": None,
                 "devices": None, "free": None, "error": None}
    try:
        health = _get(remote, "/health", timeout)
    except Exception as e:  # noqa: BLE001 -- every failure is reported, never raised
        out["error"] = _reason(e)
        return out
    out["reachable"] = True
    version = health.get("version") if isinstance(health, dict) else None
    out["version"] = version
    if version and local_version:
        out["version_matches"] = version == local_version
    try:
        listing = _get(remote, "/api/v1/device/list", timeout)
    except Exception as e:  # noqa: BLE001
        # Answered /health but not this: say so, rather than "no devices".
        out["error"] = f"devices could not be listed: {_reason(e)}"
        return out
    devices = []
    for d in (listing or {}).get("devices") or []:
        held = d.get("reservation")
        devices.append({
            "udid": d.get("udid"), "name": d.get("name"),
            "os_version": d.get("os_version"), "device_type": d.get("device_type"),
            "state": d.get("state"),
            # Free: nobody holds it. A device this key holds is "yours".
            "free": held is None,
            "yours": bool(held and held.get("yours")),
        })
    out["devices"] = devices
    out["free"] = sum(1 for d in devices if d["free"])
    return out


def statuses(local_version: str | None, timeout: float = TIMEOUT_SECONDS) -> list[dict]:
    """Every paired remote's state, in name order. A record that cannot be read
    is listed with why, not dropped: the rest still answer."""
    try:
        names = sorted(client._read())
    except client.RemoteError as e:
        return [{"name": None, "error": str(e)}]
    loaded: list[client.Remote | dict] = []
    for name in names:
        try:
            loaded.append(client.load(name))
        except client.RemoteError as e:
            loaded.append({"name": name, "url": None, "reachable": False, "error": str(e)})
    remotes = [r for r in loaded if isinstance(r, client.Remote)]
    with ThreadPoolExecutor(max_workers=max(1, min(8, len(remotes)))) as pool:
        probed = dict(zip([r.name for r in remotes],
                          pool.map(lambda r: probe(r, local_version, timeout), remotes)))
    return [probed[r.name] if isinstance(r, client.Remote) else r for r in loaded]
