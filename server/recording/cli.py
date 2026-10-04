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
                          [--video] [--allow-passthrough] [--keyframes actions,requests]
                          [--bodies all|errors|none] [--max-body-bytes N]
                          [--exclude-content-type TYPE]...
       quern record stop RECORDING_ID [--require-complete]
       quern record keyframe RECORDING_ID [--label TEXT]
       quern record list

A host is a domain and its subdomains, or a glob: --exclude-host '*.s3.*.amazonaws.com'.
For a small recording of a long run, keep the flows and limit their bodies:
--bodies errors keeps response bodies only for failed or unanswered requests. A
request's own body is kept on the line written when it starts, since whether it
will be answered is not known yet; --max-body-bytes bounds that too.
With --video, seek points come from actions and requests (requests need flows
in --kinds); --keyframes '' asks for none. `keyframe --label` marks a moment.

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


def _detail(detail) -> str:
    """A refusal in words. Some are structured -- a message and the ways out
    of it, like a capture whose simulator does not trust the CA (#414) -- and
    a dict printed as Python is the least useful way to say that."""
    if isinstance(detail, dict):
        ways = [r.get("action") for r in detail.get("resolutions") or [] if r.get("action")]
        return detail.get("message", str(detail)) + (
            f" Ways out: {', '.join(ways)}." if ways else "")
    if isinstance(detail, list):
        # A 422: the request's fields, each with what was wrong with it.
        return "; ".join(
            f"{'.'.join(str(p) for p in e.get('loc', [])[1:]) or 'request'}: {e.get('msg')}"
            for e in detail if isinstance(e, dict)) or str(detail)
    return str(detail)


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
    start.add_argument("--allow-passthrough", action="store_true")
    start.add_argument("--keyframes")
    start.add_argument("--bodies", choices=("all", "errors", "none"))
    start.add_argument("--max-body-bytes", type=int)
    start.add_argument("--exclude-content-type", action="append")
    keyframe = sub.add_parser("keyframe", add_help=False)
    keyframe.add_argument("recording_id")
    keyframe.add_argument("--label")
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
                    "include_unattributed": args.include_unattributed, "video": args.video,
                    "allow_passthrough": args.allow_passthrough,
                    "max_body_bytes": args.max_body_bytes,
                    "exclude_content_types": args.exclude_content_type}
            if args.keyframes is not None:
                body["keyframes"] = [k.strip() for k in args.keyframes.split(",") if k.strip()]
            if args.bodies is not None:
                body["bodies"] = args.bodies
            status, answer = _call("POST", "/api/v1/recordings", body)
        elif args.what == "keyframe":
            status, answer = _call("POST", f"/api/v1/recordings/{args.recording_id}/keyframe",
                                   {"label": args.label} if args.label else None)
        elif args.what == "stop":
            status, answer = _call("POST", f"/api/v1/recordings/{args.recording_id}/stop")
        else:
            status, answer = _call("GET", "/api/v1/recordings")
    except ConnectionError as e:
        print(f"quern record: {e}", file=sys.stderr)
        return 1
    if status >= 400:
        print(f"quern record {args.what}: {_detail(answer.get('detail'))}", file=sys.stderr)
        return 2
    if args.what == "keyframe":
        print("requested" if answer.get("requested") else "not requested: the movie did not answer")
        return 0 if answer.get("requested") else 3
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
            # The video's own reasons, when it is part of why: otherwise a
            # run that lost only its movie read "dropped nothing, 0 gaps"
            # and named no cause at all (CodeRabbit).
            video = ""
            if answer.get("video_lost"):
                why = [w for w in answer.get("warnings") or [] if w.startswith("video")]
                video = f"; video lost: {'; '.join(why) or 'no movie was recorded'}"
            print(f"quern record stop: {answer['id']} is not complete (state "
                  f"{answer.get('state')}, dropped {lost or 'nothing'}, "
                  f"{len(answer.get('gaps') or [])} gap(s){video})", file=sys.stderr)
            return 3
        return 0
    print(json.dumps(answer, indent=2))
    return 0
