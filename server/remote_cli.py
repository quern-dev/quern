"""`quern pair`, `quern connect` and `quern remote` (#254, phase 3).

    quern pair LABEL                    on the server: a one-time code for a client
    quern connect URL --code CODE       on the client: pair, and remember the server
    quern connect URL --key K --fingerprint F
                                        on the client: remember one paired by hand
    quern remote status                 on the server: the listener and its certificate
    quern remote rotate-cert            on the server: a new certificate; clients re-pair
    quern remote list [--json] [--no-check] [--devices]
                                        on the client: the servers it is paired with,
                                        each asked now: up or not, version, free devices;
                                        --devices lists each device: OS, family, state
    quern remote forget NAME            on the client: forget one
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

USAGE = __doc__.split("\n\n", 1)[1]


def _owner_call(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    """The local server, with the owner's key: pairing codes are made on the
    server's own Mac, never remotely."""
    from server.__main__ import _server_base_url
    from server.config import API_KEY_FILE

    url = _server_base_url()
    if url is None:
        raise ConnectionError("no server answering here: start it with `quern start`")
    try:
        key = API_KEY_FILE.read_text().strip()
    except OSError as e:
        raise ConnectionError(f"no API key at {API_KEY_FILE}: run `quern setup`") from e
    req = urllib.request.Request(
        url + path, method=method, data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "X-Quern-Session": "cli"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {"detail": str(e)}
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise ConnectionError(f"the server did not answer: {e}") from e


def _pair(args: list[str]) -> int:
    if len(args) != 1:
        print("Usage: quern pair LABEL", file=sys.stderr)
        return 2
    status, body = _owner_call("POST", "/api/v1/remote/pairing-codes", {"label": args[0]})
    if status != 200:
        print(f"quern pair: {body.get('detail', status)}", file=sys.stderr)
        return 1
    minutes = body["expires_in"] // 60
    print(f"Pairing code for {body['label']} (one use, {minutes} minutes):\n")
    print(f"  {body['code']}\n")
    print("On the client machine:\n")
    print(f"  quern connect {body['url']} --code {body['code']}\n")
    print(f"This server's certificate: {body['fingerprint']}")
    return 0


def _connect(args: list[str]) -> int:
    from server.remote import cert as cert_mod
    from server.remote import client
    from server.remote.pairing import PairingError

    parser = argparse.ArgumentParser(prog="quern connect", add_help=False)
    parser.add_argument("url")
    parser.add_argument("--code")
    parser.add_argument("--key")
    parser.add_argument("--fingerprint")
    parser.add_argument("--name")
    try:
        opts = parser.parse_args(args)
    except SystemExit:
        print(USAGE, end="", file=sys.stderr)
        return 2
    name = opts.name or _default_name(opts.url)
    try:
        if opts.code:
            if opts.key or opts.fingerprint:
                raise client.RemoteError("give --code, or --key and --fingerprint, not both")
            remote = client.pair(opts.url, opts.code, name)
        elif opts.key and opts.fingerprint:
            if not opts.url.lower().startswith("https://"):
                raise client.RemoteError(f"{opts.url!r} is not an https:// address: the "
                                         "remote listener speaks only TLS")
            remote = client.Remote(name=name, url=client.with_port(opts.url),
                                   fingerprint=cert_mod.normalise(opts.fingerprint),
                                   key=opts.key)
            # Checked before it is kept: a typo in either is found now, not
            # by the first agent that uses it.
            client.health(remote)
        else:
            raise client.RemoteError("give --code (from `quern pair` on the server), or "
                                     "--key and --fingerprint")
        client.save(remote)
    except (client.RemoteError, PairingError, ValueError, OSError) as e:
        print(f"quern connect: {e}", file=sys.stderr)
        return 1
    print(f"Paired with {remote.url} as {remote.name!r} "
          f"(certificate {cert_mod.show(remote.fingerprint)}).")
    print(f"Use it with QUERN_REMOTE={remote.name}, or "
          f"`quern mcp-install --remote {remote.name}` for the MCP.")
    return 0


def _default_name(url: str) -> str:
    import urllib.parse

    host = urllib.parse.urlsplit(url).hostname or "remote"
    return host.split(".")[0] if not host.replace(".", "").isdigit() else host


def _remote(args: list[str]) -> int:
    from server.remote import cert as cert_mod
    from server.remote import client

    if args[:1] == ["status"] and len(args) == 1:
        status, body = _owner_call("GET", "/api/v1/remote/status")
        if status != 200:
            print(f"quern remote status: {body.get('detail', status)}", file=sys.stderr)
            return 1
        if not body.get("enabled"):
            print('The remote listener is off. Turn it on with {"remote": {"enabled": true}} '
                  "in ~/.quern/config.json, then restart quern.")
            return 0
        if not body.get("listening"):
            print(f"The remote listener is on but not listening: {body.get('error')}")
            return 1
        print(f"Listening at {body['url']}\nCertificate {body['fingerprint']}")
        return 0
    if args[:1] == ["rotate-cert"] and len(args) == 1:
        from server.config import remote_settings

        settings = remote_settings()
        new = cert_mod.rotate(cert_mod.names_for(settings["hosts"], []))
        print(f"New certificate {cert_mod.show(new.fingerprint)}.")
        print("Restart quern for the listener to use it. Every client must pair again: "
              "the old certificate's pin no longer matches.")
        return 0
    if args[:1] == ["list"]:
        parser = argparse.ArgumentParser(prog="quern remote list", add_help=False)
        parser.add_argument("--json", action="store_true")
        parser.add_argument("--no-check", action="store_true")
        parser.add_argument("--devices", action="store_true")
        try:
            opts = parser.parse_args(args[1:])
        except SystemExit:
            print(USAGE, end="", file=sys.stderr)
            return 2
        if opts.no_check:
            try:
                remotes = client.list_remotes()
            except client.RemoteError as e:
                print(f"quern remote list: {e}", file=sys.stderr)
                return 1
            if opts.json:
                print(json.dumps([{"name": r.name, "url": r.url, "label": r.label}
                                  for r in remotes], indent=2))
                return 0
            if not remotes:
                print("Not paired with any remote quern. Pair with `quern connect`.")
            for r in remotes:
                print(f"{r.name:<16} {r.url:<32} as {r.label or '?':<16} "
                      f"{cert_mod.show(r.fingerprint)[:23]}...")
            return 0
        return _list_status(as_json=opts.json, devices=opts.devices)
    if args[:1] == ["forget"] and len(args) == 2:
        if client.forget(args[1]):
            print(f"Forgot {args[1]}. Its client key still works until the server's owner "
                  f"revokes it (quern clients revoke).")
            return 0
        print(f"quern remote forget: no remote called {args[1]!r}", file=sys.stderr)
        return 1
    print(USAGE, end="", file=sys.stderr)
    return 2


def _list_status(*, as_json: bool, devices: bool = False) -> int:
    """Every paired remote, asked now (`server.remote.status`). Exits 1 when
    any could not be asked, so a script can tell "all up" from "listed"."""
    from server import get_version
    from server.remote import status

    rows = status.statuses(get_version())
    if as_json:
        print(json.dumps({"local_version": get_version(), "remotes": rows}, indent=2))
    elif not rows:
        print("Not paired with any remote quern. Pair with `quern connect`.")
    else:
        for r in rows:
            print(_status_line(r))
            if devices and r.get("devices"):
                for line in _device_lines(r["devices"]):
                    print(line)
    return 0 if all(r.get("reachable") and r.get("devices") is not None for r in rows) else 1


def _device_lines(devices: list[dict]) -> list[str]:
    """One line per device: free and booted first, which is what a caller
    choosing where to run wants to see; then the rest; held last."""
    def order(d: dict) -> tuple:
        return (not d.get("free"), d.get("state") != "booted",
                d.get("device_family") or "", d.get("os_version") or "", d.get("name") or "")

    lines = []
    for d in sorted(devices, key=order):
        who = ("free" if d.get("free") else
               "yours" if d.get("yours") else f"held by {d.get('held_by') or 'another session'}")
        if d.get("is_available") is False:
            who += ", unavailable"
        name = d.get("name") or "?"
        if d.get("model") and d["model"] != name:
            name += f" ({d['model']})"
        lines.append(f"    {name:<34} {d.get('device_family') or '':<8} "
                     f"{d.get('os_version') or '':<12} {d.get('device_type') or '':<16} "
                     f"{d.get('state') or '':<9} {who}  {d.get('udid') or ''}")
    return lines


def _status_line(r: dict) -> str:
    head = f"{r.get('name') or '?':<16} {r.get('url') or '':<32} as {r.get('label') or '?':<14}"
    if not r.get("reachable"):
        return f"{head} down: {r.get('error')}"
    version = r.get("version") or "?"
    if r.get("version_matches") is False:
        version += " (differs from this Mac's)"
    if r.get("devices") is None:
        return f"{head} up  {version}  {r.get('error')}"
    devices = r["devices"]
    booted = sum(1 for d in devices if d.get("state") == "booted")
    return (f"{head} up  {version}  {len(devices)} devices ({booted} booted), "
            f"{r.get('free')} free")


def main(command: str, args: list[str]) -> int:
    try:
        if command == "pair":
            return _pair(args)
        if command == "connect":
            return _connect(args)
        return _remote(args)
    except ConnectionError as e:
        print(f"quern {command}: {e}", file=sys.stderr)
        return 1
