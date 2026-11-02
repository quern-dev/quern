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
import threading
import time
import urllib.error
import urllib.request
from typing import Any

from server.remote import client

#: Per request. A remote on the LAN answers in milliseconds; one that is off
#: is a connection timeout, and the list must not hang on it.
TIMEOUT_SECONDS = 4.0
#: For the whole list. The per-request timeout bounds each read, not a body
#: that drips a byte at a time or a name that takes long to resolve, so the
#: list stops waiting here and says which remotes had not answered (review).
DEADLINE_SECONDS = 10.0


class _Unexpected(Exception):
    """An answer that is not what quern sends."""
#: The session a status read names: a client key must name one, even to read.
_SESSION = "quern-remote-status"


#: The name this Mac's own server is listed under, first.
LOCAL = "local"


def _get(remote: client.Remote, path: str, timeout: float) -> Any:
    req = urllib.request.Request(remote.url + path, headers={
        "Authorization": f"Bearer {remote.key}", "X-Quern-Session": _SESSION})
    # This Mac's own server is plain http on loopback, with no pin to check.
    # Told apart by having no pin, never by name: a remote paired as "local"
    # must not skip its pin (CodeRabbit).
    # And never through a proxy from the environment, which would be handed
    # the owner's key (CodeRabbit).
    opener = (urllib.request.build_opener(urllib.request.ProxyHandler({}))
              if not remote.fingerprint else client.opener(remote))
    with opener.open(req, timeout=timeout) as resp:
        body = resp.read()
    try:
        return json.loads(body)
    except ValueError as e:
        raise _Unexpected("not JSON") from e


def _reason(e: BaseException) -> str:
    if isinstance(e, client.PinMismatch):
        return f"certificate changed: {e}"
    if isinstance(e, _Unexpected):
        return f"answered with something that is not quern's ({e})"
    if isinstance(e, urllib.error.HTTPError):
        if e.code == 401:
            return "refused its key (HTTP 401): it was revoked, or the server was reset"
        if e.code == 403:
            return "refused this request (HTTP 403)"
        return f"HTTP {e.code}"
    if isinstance(e, urllib.error.URLError):
        reason = e.reason
        if isinstance(reason, client.PinMismatch):
            return f"certificate changed: {reason}"
        return f"not reachable ({reason})"
    if isinstance(e, TimeoutError):
        return "not reachable (timed out)"
    return f"could not be asked ({type(e).__name__}: {e})"


def _blank(name: str | None, url: str | None, label: str | None) -> dict:
    return {"name": name, "url": url, "label": label, "reachable": False, "version": None,
            "version_matches": None, "devices": None, "free": None, "error": None}


def _health(answer: Any) -> str:
    """The version a quern /health answer carries; _Unexpected otherwise."""
    if not isinstance(answer, dict) or answer.get("status") != "ok" \
            or not isinstance(answer.get("version"), str):
        raise _Unexpected("no quern health answer")
    return answer["version"]


def _devices(answer: Any, label: str = "") -> list[dict]:
    """Devices from a device-list answer; _Unexpected for anything else -- an
    answer that is not a list of devices is never read as "none" (review)."""
    rows = answer.get("devices") if isinstance(answer, dict) else None
    if not isinstance(rows, list) or not all(isinstance(d, dict) for d in rows):
        raise _Unexpected("no device list")
    devices = []
    for d in rows:
        held = d.get("reservation")
        if held is not None and not isinstance(held, dict):
            raise _Unexpected("a device's reservation is not an object")
        devices.append({
            "udid": d.get("udid"), "name": d.get("name"),
            "os_version": d.get("os_version"), "device_type": d.get("device_type"),
            "device_family": d.get("device_family"),
            # What the remote says when it knows the model (newer servers);
            # None otherwise -- a simulator's name is only its default.
            "model": d.get("model"),
            # An Android device's CA path (#582), from servers that say.
            **({"ca_install": d["ca_install"]} if d.get("ca_install") else {}),
            "state": d.get("state"), "is_available": d.get("is_available"),
            "held_by": held.get("held_by") if held else None,
            # Free: nobody holds it. "Yours": held by this key, through any of
            # its sessions. The server's own `yours` means this *session*, and
            # this read names a session of its own, so it never matched; a
            # client key's session ids carry its label (found live).
            "free": held is None,
            # A server that says (`this_key`) is believed; an older one is read
            # by the label its key's sessions carry.
            "yours": bool(held and (held.get("this_key") or held.get("yours") or (
                label and str(held.get("held_by") or "").startswith(f"{label}:")))),
        })
    return devices


def probe(remote: client.Remote, local_version: str | None,
          timeout: float = TIMEOUT_SECONDS) -> dict:
    """One remote's state now. Never raises: one remote's odd answer must not
    cost the list every other remote (review)."""
    out = _blank(remote.name, remote.url, remote.label)
    try:
        version = _health(_get(remote, "/health", timeout))
    except Exception as e:  # noqa: BLE001 -- every failure is reported, never raised
        out["error"] = _reason(e)
        return out
    out["reachable"] = True
    out["version"] = version
    if local_version:
        out["version_matches"] = version == local_version
    try:
        devices = _devices(_get(remote, "/api/v1/device/list", timeout), remote.label)
    except Exception as e:  # noqa: BLE001
        # Answered /health but not this: said, rather than "no devices".
        out["error"] = f"devices could not be listed: {_reason(e)}"
        return out
    out["devices"] = devices
    out["free"] = sum(1 for d in devices if d["free"])
    return out


def _local() -> client.Remote | dict:
    """This Mac's own server, to list beside the remotes, or why it cannot be."""
    from server.__main__ import _server_base_url
    from server.config import client_api_key

    url = _server_base_url()
    if url is None:
        return {**_blank(LOCAL, None, "this Mac"), "local": True,
                "error": "not running here (quern start)"}
    return client.Remote(name=LOCAL, url=url.rstrip("/"), fingerprint="", key=client_api_key(),
                         label="this Mac")


def statuses(local_version: str | None, timeout: float = TIMEOUT_SECONDS,
             deadline: float = DEADLINE_SECONDS, include_local: bool = False) -> list[dict]:
    """Every paired remote's state, in name order. A record that cannot be read
    is listed with why, not dropped: the rest still answer."""
    ends = time.monotonic() + deadline   # the whole list, the local lookup included
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
    if include_local:
        # First, so a caller comparing machines starts from the one it is on.
        try:
            loaded.insert(0, _local())
        except Exception as e:  # noqa: BLE001 -- the remotes still answer
            loaded.insert(0, {**_blank(LOCAL, None, "this Mac"), "local": True,
                              "error": f"could not be asked ({e})"})
    remotes = [r for r in loaded if isinstance(r, client.Remote)]
    probed = _probe_all(remotes, local_version, timeout, deadline, ends)
    # By position, not name: two entries may share one (CodeRabbit).
    answers = iter(probed)
    rows = [next(answers) if isinstance(r, client.Remote) else r for r in loaded]
    for row in rows:
        row["local"] = row.get("name") == LOCAL and row.get("label") == "this Mac"
    return rows


def _probe_all(remotes: list[client.Remote], local_version: str | None, timeout: float,
               deadline: float, ends: float | None = None) -> list[dict]:
    """Each remote probed on its own daemon thread, waited for until `ends`
    (`deadline` from now if not given): a remote still talking then is
    reported as not answering, and its thread -- a daemon -- does not keep the
    process alive. Answers in the order of `remotes`."""
    results: dict[int, dict] = {}
    lock = threading.Lock()

    def run(i: int, remote: client.Remote) -> None:
        row = probe(remote, local_version, timeout)
        with lock:
            results[i] = row

    threads = [threading.Thread(target=run, args=(i, r), daemon=True)
               for i, r in enumerate(remotes)]
    for t in threads:
        t.start()
    if ends is None:
        ends = time.monotonic() + deadline
    for t in threads:
        t.join(max(0.0, ends - time.monotonic()))
    with lock:
        done = dict(results)
    rows = []
    for i, r in enumerate(remotes):
        row = done.get(i)
        if row is None:
            row = _blank(r.name, r.url, r.label)
            row["error"] = f"not answering (no complete answer within {deadline:.0f}s)"
        rows.append(row)
    return rows
