"""`quern record start|stop|list`: recordings from a shell or a CI step (#364).

A CI job should not have to read ~/.quern for a port and a key and hand-roll
curl. This finds the running server the way `quern url` does and calls the
recordings API. Standard library only, like the rest of the early dispatch.

Exit codes are the point for a CI step: 0 done, 1 could not ask (no server,
no key), 2 the server refused, 3 a recording that failed, or with
`stop --require-complete` one that lost something.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

USAGE = """\
Usage: quern record start --udid UDID [--out DIR] [--kinds actions,flows,logs]
                          [--host H]... [--exclude-host H]... [--include-unattributed]
                          [--video]
       quern record stop RECORDING_ID [--require-complete]
       quern record list

Record one device's actions, flows and logs to disk until stopped, for as long
as a run lasts. `start` prints the recording's id on the first line, so a
script can keep it:

    REC=$(quern record start --udid "$SIM" --out "$ARTIFACTS/quern" | head -1)
    ...
    quern record stop "$REC" --require-complete

Then GET /api/v1/trace?recording=<id or DIR> gives the trace over any window.
"""


def _call(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    from server.__main__ import _server_base_url
    from server.config import API_KEY_FILE

    url = _server_base_url()
    if url is None:
        raise ConnectionError("no server answering: start it with `quern start`")
    try:
        key = API_KEY_FILE.read_text().strip()
    except OSError:
        key = ""
    if not key:
        raise ConnectionError(f"no API key at {API_KEY_FILE}: run `quern setup`")
    req = urllib.request.Request(
        url + path, method=method, data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read() or b"{}").get("detail", "")
        except ValueError:
            detail = ""
        return e.code, {"detail": detail or str(e)}
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise ConnectionError(f"the server did not answer: {e}") from e


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0 if argv else 2
    parser = argparse.ArgumentParser(prog="quern record", add_help=False)
    sub = parser.add_subparsers(dest="what")
    start = sub.add_parser("start", add_help=False)
    start.add_argument("--udid", required=True)
    start.add_argument("--out")
    start.add_argument("--host", action="append")
    start.add_argument("--exclude-host", action="append")
    start.add_argument("--kinds")
    start.add_argument("--include-unattributed", action="store_true")
    start.add_argument("--video", action="store_true")
    stop = sub.add_parser("stop", add_help=False)
    stop.add_argument("recording_id")
    stop.add_argument("--require-complete", action="store_true")
    sub.add_parser("list", add_help=False)
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        print(USAGE, file=sys.stderr)
        return 2
    try:
        if args.what == "start":
            kinds = [k.strip() for k in args.kinds.split(",") if k.strip()] if args.kinds \
                else None
            body = {"udid": args.udid, "output_dir": args.out, "kinds": kinds,
                    "hosts": args.host, "exclude_hosts": args.exclude_host,
                    "include_unattributed": args.include_unattributed, "video": args.video}
            status, answer = _call("POST", "/api/v1/recordings", body)
        elif args.what == "stop":
            status, answer = _call("POST", f"/api/v1/recordings/{args.recording_id}/stop")
        else:
            status, answer = _call("GET", "/api/v1/recordings")
    except ConnectionError as e:
        print(f"quern record: {e}", file=sys.stderr)
        return 1
    if status >= 400:
        print(f"quern record {args.what}: {answer.get('detail')}", file=sys.stderr)
        return 2
    if args.what == "start":
        print(answer["id"])
        print(f"recording {answer['udid']} into {answer['output_dir']}", file=sys.stderr)
        for w in answer.get("warnings") or []:
            print(f"warning: {w}", file=sys.stderr)
        return 0
    if args.what == "stop":
        print(json.dumps(answer, indent=2))
        if answer.get("state") not in ("stopped", None):
            # A failed recording is not a success, flag or no flag.
            print(f"quern record stop: {answer.get('id')} {answer.get('state')}: "
                  f"{answer.get('error')}", file=sys.stderr)
            return 3
        if args.require_complete and answer.get("complete") is not True:
            lost = {k: v for k, v in (answer.get("dropped") or {}).items() if v}
            print(f"quern record stop: {answer['id']} is not complete (state "
                  f"{answer.get('state')}, dropped {lost or 'nothing'}, "
                  f"{len(answer.get('gaps') or [])} gap(s))", file=sys.stderr)
            return 3
        return 0
    print(json.dumps(answer, indent=2))
    return 0
