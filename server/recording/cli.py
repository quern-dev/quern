"""`quern record start|stop|list|download`: recordings from a shell or a CI step (#364).

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
import os
import re
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from server.session import session_from_env

USAGE = """\
Usage: quern record start --udid UDID [--out DIR] [--kinds actions,flows,logs]
                          [--host H]... [--exclude-host H]... [--include-unattributed]
                          [--requested-by NAME] [--video [--viewer]] [--allow-passthrough]
                          [--keyframes actions,requests]
                          [--bodies all|errors|none] [--max-body-bytes N]
                          [--exclude-content-type TYPE]...
       quern record stop RECORDING_ID [--require-complete]
       quern record keyframe RECORDING_ID [--label TEXT]
       quern record list [--requested-by NAME]
       quern record download RECORDING_ID [--dest DIR]

A host is a domain and its subdomains, or a glob: --exclude-host '*.s3.*.amazonaws.com'.
For a small recording of a long run, keep the flows and limit their bodies:
--bodies errors keeps response bodies only for failed or unanswered requests. A
request's own body is kept on the line written when it starts, since whether it
will be answered is not known yet; --max-body-bytes bounds that too.
With --video, seek points come from actions and requests (requests need flows
in --kinds); --keyframes '' asks for none. `keyframe --label` marks a moment.
--viewer (with --video) also prints a LAN viewer link on the second line, for
a CI job to post: it plays the simulator live in a browser on another machine,
and expires 10 minutes after the recording stops. Needs viewer.enabled in
~/.quern/config.json.

Record one device's actions, flows and logs to disk until stopped, for as long
as a run lasts. `start` prints the recording's id on the first line, so a
script can keep it:

    REC=$(quern record start --udid "$SIM" --out "$ARTIFACTS/quern" | head -1)
    ...
    quern record stop "$REC" --require-complete

Then GET /api/v1/trace?recording=<id or DIR> gives the trace over any window.

`download` copies a recording's files -- events.jsonl, manifest.json, the
video -- into DIR (default quern-recordings/ID, here), for when the server is
another machine (QUERN_REMOTE) and its output directory is on its disk. A
client key downloads its own recordings only.
"""


def _target():
    """(url, key, remote): the server to ask -- the paired remote QUERN_REMOTE
    names (#254, phase 3), else this Mac's -- and the key to ask it with."""
    from server.__main__ import _server_base_url
    from server.config import API_KEY_FILE, client_api_key
    from server.remote import client

    try:
        remote = client.from_env()
    except client.RemoteError as e:
        raise ConnectionError(str(e)) from e
    if remote is not None:
        return remote.url, remote.key, remote
    url = _server_base_url()
    if url is None:
        raise ConnectionError("no server answering: start it with `quern start`")
    key = client_api_key()
    if not key:
        raise ConnectionError(f"no API key at {API_KEY_FILE} and no QUERN_CLIENT_KEY: "
                              "run `quern setup`")
    return url, key, None


def _request(url: str, key: str, path: str, method: str = "GET",
             body: dict | None = None) -> urllib.request.Request:
    return urllib.request.Request(
        url + path, method=method, data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 # Who is asking (#254): an agent's Bash command is attributed
                 # to that agent's session, anything else to "cli".
                 "X-Quern-Session": session_from_env(dict(os.environ), fallback="cli")})


def _opener(remote):
    """urllib's open, pinned to the remote's certificate when there is one."""
    from server.remote import client

    return client.opener(remote).open if remote is not None else urllib.request.urlopen


def _call(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    from server.remote import client

    url, key, remote = _target()
    req = _request(url, key, path, method, body)
    try:
        with _opener(remote)(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read() or b"{}").get("detail", "")
        except ValueError:
            detail = ""
        return e.code, {"detail": detail or str(e)}
    except client.RemoteError as e:     # a pin mismatch: one line, not a traceback
        raise ConnectionError(str(e)) from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise ConnectionError(f"the server did not answer: {e}") from e


#: The server's recording ids: the default destination is built from one.
_REC_ID = re.compile(r"rec_[0-9a-f]{12}")

#: The names a recording writes, as the server serves them.
_FILE = re.compile(r"(events\.jsonl|manifest\.json|video-[0-9]{1,4}\.(mp4|log))")


def _download(recording_id: str, dest: str | None) -> int:
    """Copy a recording's files into `dest` (#482). Each lands through a
    .part file, so a download cut off leaves no half file under its name."""
    from server.remote import client

    if not _REC_ID.fullmatch(recording_id):
        print(f"quern record download: {recording_id!r} is not a recording id (rec_ and 12 "
              "hex digits, as `start` and `list` print)", file=sys.stderr)
        return 2
    status, listing = _call("GET", f"/api/v1/recordings/{recording_id}/files")
    if status >= 400:
        print(f"quern record download: {_detail(listing.get('detail'))}", file=sys.stderr)
        return 2
    target = Path(dest or Path("quern-recordings") / recording_id).expanduser()
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"quern record download: cannot write to {target}: {e}", file=sys.stderr)
        return 1
    url, key, remote = _target()
    got = []
    for entry in listing.get("files") or []:
        name = str(entry.get("name", ""))
        if not _FILE.fullmatch(name):
            continue        # nothing but the names a recording writes lands here
        part = target / f"{name}.part"
        req = _request(url, key, f"/api/v1/recordings/{recording_id}/files/{name}")
        try:
            with _opener(remote)(req, timeout=60) as resp, open(part, "wb") as out:
                expected = resp.headers.get("Content-Length")
                shutil.copyfileobj(resp, out, 1024 * 1024)
                written = out.tell()
            # A connection closed early reads as the end of the file: the
            # length it promised is what says the file is whole (review).
            if expected is not None and expected.isdigit() and written != int(expected):
                raise OSError(f"the server sent {written} of {expected} bytes")
            part.replace(target / name)
        except urllib.error.HTTPError as e:
            part.unlink(missing_ok=True)
            print(f"quern record download: {name}: HTTP {e.code}", file=sys.stderr)
            return 2
        except (client.RemoteError, urllib.error.URLError, OSError) as e:
            part.unlink(missing_ok=True)
            print(f"quern record download: {name}: {e}", file=sys.stderr)
            return 1
        got.append(f"{name} ({(target / name).stat().st_size} bytes)")
    print(str(target))
    for line in got:
        print(f"  {line}", file=sys.stderr)
    if listing.get("live"):
        print("note: still recording -- these are the files as written so far", file=sys.stderr)
    return 0


def _remote_note(recording_id: str) -> None:
    """Under QUERN_REMOTE, a recording's paths are on the server's disk."""
    if os.environ.get("QUERN_REMOTE", "").strip():
        print(f"(those paths are on the server: `quern record download {recording_id}` "
              "copies the files here)", file=sys.stderr)


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
    start.add_argument("--viewer", action="store_true")
    start.add_argument("--allow-passthrough", action="store_true")
    start.add_argument("--requested-by")
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
    listing = sub.add_parser("list", add_help=False)
    listing.add_argument("--requested-by")
    download = sub.add_parser("download", add_help=False)
    download.add_argument("recording_id")
    download.add_argument("--dest")
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        print(USAGE, file=sys.stderr)
        return 2
    if args.what == "download":
        try:
            return _download(args.recording_id, args.dest)
        except ConnectionError as e:
            print(f"quern record: {e}", file=sys.stderr)
            return 1
    try:
        if args.what == "start":
            kinds = [k.strip() for k in args.kinds.split(",") if k.strip()] if args.kinds \
                else None
            body = {"udid": args.udid, "output_dir": args.out, "kinds": kinds,
                    "hosts": args.host, "exclude_hosts": args.exclude_host,
                    "include_unattributed": args.include_unattributed, "video": args.video,
                    "viewer": args.viewer,
                    "allow_passthrough": args.allow_passthrough,
                    "requested_by": args.requested_by,
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
            query = (f"?{urllib.parse.urlencode({'requested_by': args.requested_by})}"
                     if args.requested_by else "")
            status, answer = _call("GET", f"/api/v1/recordings{query}")
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
        # The viewer link on stdout's second line (#449), so a CI script keeps
        # `head -1` for the id and takes `sed -n 2p` for the link to post.
        if answer.get("viewer_url"):
            print(answer["viewer_url"])
        print(f"recording {answer['udid']} into {answer['output_dir']}", file=sys.stderr)
        _remote_note(answer["id"])
        for w in answer.get("warnings") or []:
            print(f"warning: {w}", file=sys.stderr)
        return 0
    if args.what == "stop":
        print(json.dumps(answer, indent=2))
        _remote_note(answer.get("id", args.recording_id))
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
