"""`quern clients`: issue, list and revoke client keys (#254, phase 2).

Works on ~/.quern/clients.json directly, so it needs no running server; a
running one notices the change within a request (and its sweep, within a
second, ends the sessions of a revoked key).
"""

from __future__ import annotations

import sys

USAGE = """\
Usage: quern clients add LABEL      Issue a client key; it is printed once
       quern clients list           Issued keys: label, created, last used, revoked
       quern clients revoke LABEL   Revoke a key; its sessions end and devices are freed

A client key is a caller other than the owner: another person's agent, a CI
job, a second agent kept in its lane. It can watch every device, reserve
devices, and change only the ones its own session holds -- nothing Mac-wide
(the proxy, capture, updates). Give it to a client as QUERN_API_KEY, which the
quern MCP, `quern session run` and `quern record` read.

LABEL is 1-16 of a-z, 0-9 and '-'. It prefixes the client's session ids.
"""


def main(argv: list[str]) -> int:
    from server.clients import ClientKeyError, ClientKeys

    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE, end="")
        return 0 if argv else 2
    command, rest = argv[0], argv[1:]
    keys = ClientKeys()
    try:
        if command == "add" and len(rest) == 1:
            key = keys.add(rest[0])
            print(f"Client key for {rest[0]} (shown once; quern keeps only its hash):")
            print()
            print(f"  {key}")
            print()
            print("Give it to the client as QUERN_API_KEY.")
            return 0
        if command == "revoke" and len(rest) == 1:
            record = keys.revoke(rest[0])
            print(f"Revoked {record.label}'s key. A running server ends its sessions "
                  "within a second and refuses its next request.")
            return 0
        if command == "list" and not rest:
            return _list(keys)
    except ClientKeyError as e:
        print(f"quern clients: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"quern clients: could not update {keys.path}: {e}", file=sys.stderr)
        return 1
    print(USAGE, end="", file=sys.stderr)
    return 2


def _list(keys) -> int:
    records = keys.records()
    if not records:
        print("No client keys. Issue one with `quern clients add LABEL`.")
        return 0
    seen = keys.last_used()
    print(f"{'LABEL':<17} {'CREATED':<26} {'LAST USED':<26} STATUS")
    for r in records:
        status = f"revoked {r.revoked_at}" if r.revoked_at else "active"
        print(f"{r.label:<17} {r.created_at:<26} {seen.get(r.label, 'never'):<26} {status}")
    return 0
