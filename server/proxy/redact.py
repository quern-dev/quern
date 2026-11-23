"""Secrets in captured traffic, kept out of what quern shows (#742).

Flow output carried every request and response header as captured, so an
agent reading a flow put the app's bearer tokens, cookies and API keys into
its transcript -- measured on a real session, which had to read flows over
REST and strip a token by hand. Here a secret header's value, and a secret
query-string parameter's, is replaced in what is *shown* by a placeholder:

    Authorization: Bearer ‹redacted, 1,184 chars, id 3f2a9c1e04bd›

The scheme and length stay, so an agent can still tell an authenticated
request from one that isn't. The id is an HMAC of the value under a key kept
in this server's state directory: equal values give equal ids on this server
-- "same token as before?", "did it rotate?" -- and the id is useless
anywhere else. Not a plain hash, which would make a low-entropy value such as
a Basic-auth password guessable.

The real values stay inside the server: the FlowStore keeps them, and replay,
mocks and intercepts use them. A placeholder that comes back as an input -- an
agent copying the headers it was shown into `release_flow`'s modifications --
is put back to its real value (`restore`), since sending the placeholder
would fail auth in a way that looks like the app's own bug.

Bodies (#749): secret-named fields in JSON and form bodies are redacted the
same way, edited in place by `server.proxy.redact_body` so the rest of a body
stays exactly as captured. Other bodies -- multipart, binary, HTML -- are shown
as captured, and the answer names them, so nobody assumes they were redacted.
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import hmac
import logging
import os
import re
import secrets
import threading
from collections import OrderedDict
from collections.abc import Iterator
from typing import Any
from urllib.parse import unquote_plus

from server.proxy import redact_body as body_mod

logger = logging.getLogger(__name__)

#: Headers whose value is a credential or session, matched case-insensitively.
HEADERS = frozenset({
    "authorization", "proxy-authorization", "cookie", "set-cookie",
    "x-api-key", "x-auth-token",
})

#: Query-string parameters whose value is a credential, matched
#: case-insensitively on the decoded name.
PARAMS = frozenset({
    "access_token", "refresh_token", "id_token", "token", "api_key", "apikey",
    "key", "sig", "signature", "client_secret", "password", "secret",
})

#: What a flow answer says when it redacted anything.
NOTE = ("Secret headers, query-string parameters and JSON or form body fields are "
        "redacted: pass reveal (names, or true) to see them.")


def note(names: list[str] | None, unscanned: list[str] | None) -> str | None:
    """An answer's redaction_note: said when anything was redacted, and when a
    body that could hold secrets was shown as captured -- multipart, binary,
    HTML -- since only JSON and form bodies are scanned (#749)."""
    parts = [NOTE] if names else []
    if unscanned:
        parts.append(f"Not redacted, shown as captured: {', '.join(unscanned)} -- only "
                     "JSON and form bodies are scanned.")
    return " ".join(parts) or None

#: How many redacted values are remembered for `restore`. Bounded: each is a
#: secret held in memory, though the FlowStore holds the same ones anyway.
SEEN_MAX = 20_000

_PLACEHOLDER = re.compile(r"‹redacted, [\d,]+ chars, id ([0-9a-f]{12})›")
_SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9-]{0,19})(\s+)(\S.*)$", re.S)
#: A cookie's `name=value` at the start of a Set-Cookie, or after the comma
#: that joins several. An attribute (`Path=/`) follows a `;`, and an Expires
#: date's comma is followed by a day number, not `name=`, so neither matches.
_SET_COOKIE = re.compile(r"(^|,\s*)([^=;,\s]+)=([^;,]*)")

_lock = threading.Lock()
_seen: OrderedDict[str, str] = OrderedDict()
_key_cache: bytes | None = None
_settings_cache: tuple[tuple, tuple[bool, frozenset[str], frozenset[str], frozenset[str]]
                       ] | None = None
#: The ids of placeholders that stand for a JSON literal (`1234`, `true`), not
#: a string: a body's way back puts them back without quotes.
_literal_ids: set[str] = set()
#: Under blind sharing only (#254): the flows each placeholder was made from,
#: as (device, time), so a placeholder is put back only for a caller whose
#: view still shows one of them (#756 review). Without it a client key could
#: pass a placeholder from a device it never held, or no longer holds, and
#: read the real value back from `restored_response` -- or send it on.
#: Per placeholder, its sources and the latest time each was seen: a device's
#: udid, or ("principal", P) for a held request P's intercept holds. Only the
#: latest time per source matters -- `View.allows` compares it with a
#: reservation's start -- so a cookie on every request stays one entry.
_sources: dict[str, dict[Any, Any]] = {}
#: Where the value being redacted comes from, as (source, time), while a flow
#: or held request is being redacted under blind sharing.
_source: contextvars.ContextVar[tuple[Any, Any] | None] = contextvars.ContextVar(
    "quern_redact_source", default=None)


class UnknownPlaceholder(ValueError):
    """A placeholder whose real value this server does not know: from another
    server, or remembered so long ago it has been let go."""


# -- settings -----------------------------------------------------------------


def settings() -> tuple[bool, frozenset[str], frozenset[str], frozenset[str]]:
    """(on, headers, params, body fields) from config.json's `proxy` object:

        {"proxy": {"redact": false, "redact_headers": ["X-Shazam-Token"],
                   "redact_params": ["session"], "redact_fields": ["pin"]}}

    `redact_headers`, `redact_params` and `redact_fields` *add* to the defaults rather than
    replacing them: a list that replaced them, written to add one header,
    would quietly stop redacting Authorization. `redact: false` -- a literal
    false, nothing else -- is the one way to turn it off on this Mac.
    Anything malformed reads as on with the defaults: a typo must never mean
    "show every token"."""
    from server import config

    # Cached on the file's identity: this runs for every captured flow's log
    # line, and config.json changes rarely. A changed file is read again.
    path = config.USER_CONFIG_FILE
    try:
        st = path.stat()
        stamp = (str(path), st.st_mtime_ns, st.st_size)
    except OSError:
        stamp = (str(path), None, None)
    global _settings_cache
    if _settings_cache is not None and _settings_cache[0] == stamp:
        return _settings_cache[1]
    raw = config.read_user_config().get("proxy")
    raw = raw if isinstance(raw, dict) else {}

    def extra(key: str) -> frozenset[str]:
        names = raw.get(key)
        if not isinstance(names, list):
            return frozenset()
        return frozenset(n.strip().lower() for n in names if isinstance(n, str) and n.strip())

    result = (raw.get("redact") is not False, HEADERS | extra("redact_headers"),
              PARAMS | extra("redact_params"), body_mod.FIELDS | extra("redact_fields"))
    _settings_cache = (stamp, result)
    return result


def _key() -> bytes:
    """This server's fingerprint key, in its state directory (0600), made on
    first use. Unreadable or unwritable, a key for this process alone: the
    ids then change at a restart, which costs comparisons across one, never
    a secret."""
    global _key_cache
    if _key_cache is not None:
        return _key_cache
    from server import config

    path = config.CONFIG_DIR / "redaction-key"
    try:
        if path.is_file():
            key = bytes.fromhex(path.read_text().strip())
            if len(key) >= 16:
                _key_cache = key
                return key
        path.parent.mkdir(parents=True, exist_ok=True)
        key = secrets.token_bytes(32)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(key.hex())
        _key_cache = key
    except (OSError, ValueError) as e:
        logger.warning("[redact] could not keep a fingerprint key at %s (%s): "
                       "using one for this process", path, e)
        _key_cache = secrets.token_bytes(32)
    return _key_cache


# -- placeholders ---------------------------------------------------------------


def fingerprint(value: str) -> str:
    """The value's id on this server: 12 hex characters (48 bits). The round
    trip puts a value back by its id, so two values sharing one would send
    the wrong secret; at 48 bits that does not happen at any size quern
    keeps."""
    return hmac.new(_key(), value.encode("utf-8", "surrogateescape"),
                    hashlib.sha256).hexdigest()[:12]


def placeholder(value: str, *, literal: bool = False) -> str:
    """`value` as shown, remembered so `restore` can put it back. A `literal`
    -- a JSON number or true/false/null -- is fingerprinted apart from the
    string with the same text, so `1234` and `"1234"` never share an id and
    the way back never changes a body's types."""
    vid = fingerprint(f"\x00literal:{value}" if literal else value)
    with _lock:
        _seen[vid] = value
        _seen.move_to_end(vid)
        if literal:
            _literal_ids.add(vid)
        if (source := _source.get()) is not None:
            key, when = source
            seen = _sources.setdefault(vid, {})
            if key not in seen or (when is not None and (seen[key] is None or when > seen[key])):
                seen[key] = when
        while len(_seen) > SEEN_MAX:
            gone, _ = _seen.popitem(last=False)
            _literal_ids.discard(gone)
            _sources.pop(gone, None)
    return f"‹redacted, {len(value):,} chars, id {vid}›"


#: The headers whose value starts with an auth scheme ("Bearer ...").
_SCHEMED = frozenset({"authorization", "proxy-authorization"})


def _hide(value: str) -> str:
    """`value` as a placeholder -- unless it already is exactly one. A trace
    built from a redacted recording reads placeholders back: redacted again
    they would show the placeholder's own length and a new id, and restore to
    the placeholder rather than the real value (#750 review). Text *beside* a
    placeholder is still redacted: only a whole one is left."""
    return value if _PLACEHOLDER.fullmatch(value) else placeholder(value)


def _header_value(name: str, value: str) -> str:
    # A captured value that happens to contain placeholder text is redacted
    # like any other: it is the app's, so it is untrusted, and a guard that
    # skipped it would show the real secret beside it (review).
    if not value:
        return value
    if name == "cookie":
        # Each cookie's name stays: which cookies a request carried is often
        # the question, and the names are not the secret.
        return "; ".join(
            f"{k}={_hide(v)}" if v else part
            for part in value.split(";")
            for k, _, v in [part.strip().partition("=")]
            if part.strip())
    if name == "set-cookie":
        return _SET_COOKIE.sub(
            lambda m: f"{m.group(1)}{m.group(2)}={_hide(m.group(3))}"
            if m.group(3) else m.group(0), value)
    # A scheme is kept only where the header has one: in X-Api-Key, the
    # first word of "live-secret suffix" is part of the key (review).
    scheme = _SCHEME.match(value) if name in _SCHEMED else None
    if scheme:
        return f"{scheme.group(1)}{scheme.group(2)}{_hide(scheme.group(3))}"
    return _hide(value)


def _query(text: str, params: frozenset[str], reveal: frozenset[str], *,
           fragment_ok: bool = True) -> tuple[str, set[str]]:
    """`text` -- a URL or a path -- with secret query parameters' values
    redacted, and the names redacted. Only the values change, character for
    character elsewhere: the URL is shown, not re-encoded. `fragment_ok`
    false for a form body, where a `#` is data: read as a fragment, it hid
    every field after it (#753 review)."""
    before, sep, rest = text.partition("?")
    if not sep:
        return text, set()
    query, hashsep, fragment = rest.partition("#") if fragment_ok else (rest, "", "")
    names: set[str] = set()
    out = []
    for part in query.split("&"):
        key, eq, val = part.partition("=")
        name = unquote_plus(key).lower()
        if eq and val and name in params and name not in reveal:
            out.append(f"{key}={_hide(val)}")
            names.add(name)
        else:
            out.append(part)
    return before + sep + "&".join(out) + hashsep + fragment, names


def reveal_set(reveal: Any) -> frozenset[str] | None:
    """`reveal` as asked: None (or anything unreadable) redacts everything;
    `True` or "true" redacts nothing, returned as the sentinel `ALL`; names,
    as a list or a comma-separated string, leave those names alone."""
    if reveal is True or (isinstance(reveal, str) and reveal.strip().lower() == "true"):
        return ALL
    if isinstance(reveal, str):
        reveal = reveal.split(",")
    if isinstance(reveal, (list, tuple, set, frozenset)):
        return frozenset(n.strip().lower() for n in reveal if isinstance(n, str) and n.strip())
    return None


#: `reveal_set`'s "redact nothing".
ALL: frozenset[str] = frozenset({"*"})


def redact_text(text: str) -> str:
    """A free-text line -- a log message's `METHOD path` -- with secret query
    parameters redacted. Always applied: a log line has no reveal, and the
    flow it came from keeps the real values."""
    on, _, params, _ = settings()
    if not on or not text or "?" not in text:
        return text
    return _query(text, params, frozenset())[0]


def redact_flow(flow: Any, reveal: Any = None, *, force: bool = False
                ) -> tuple[Any, list[str]]:
    """A copy of `flow` (a FlowRecord) as shown, and the names redacted in it.
    The stored record is never changed: replay and mocks need the real
    values. `force` redacts even with `proxy.redact: false`: a recording
    that started redacted says so in its manifest, and stays so."""
    flows, names = redact_flows([flow], reveal, force=force)
    return flows[0], names


def redact_flows(flows: list[Any], reveal: Any = None, *, force: bool = False
                 ) -> tuple[list[Any], list[str]]:
    """Copies of `flows` as shown, and every name redacted in any of them.
    The settings are read once for the lot, not once a flow."""
    out, names, _ = shown_flows(flows, reveal, force=force)
    return out, names


def shown_flows(flows: list[Any], reveal: Any = None, *, force: bool = False
                ) -> tuple[list[Any], list[str], list[str]]:
    """`redact_flows`, and the bodies shown as captured that could hold a
    secret -- named for the answer's note (`note`)."""
    on, headers, params, fields = settings()
    shown = reveal_set(reveal)
    if (not on and not force) or shown is ALL:
        return list(flows), [], []
    shown = shown or frozenset()
    names: set[str] = set()
    unscanned: set[str] = set()
    out = [_redact_one(f, (headers, params, fields), shown, names, unscanned) for f in flows]
    return out, sorted(names), sorted(unscanned)


Lists = tuple[frozenset[str], frozenset[str], frozenset[str]]


def _head(h: dict[str, str], headers: frozenset[str], shown: frozenset[str],
          names: set[str]) -> dict[str, str]:
    out = {}
    for k, v in h.items():
        low = k.lower()
        if low in headers and low not in shown and isinstance(v, str) and v:
            out[k] = _header_value(low, v)
            names.add(low)
        else:
            out[k] = v
    return out


def _content_type(h: dict[str, str]) -> str:
    for k, v in (h or {}).items():
        if k.lower() == "content-type" and isinstance(v, str):
            return v
    return ""


def _body_update(part: Any, side: str, fields: frozenset[str], shown: frozenset[str],
                 names: set[str], unscanned: set[str]) -> dict[str, Any]:
    """The `body` update for a request or response, or nothing (#749). The
    body is edited in place: JSON by a scanner, form bodies as a query
    string, each up to a truncation cut. A body neither JSON nor form is left
    as captured and named in `unscanned`, unless it is an image, video, audio
    or font. `body_size` stays the real size."""
    body = getattr(part, "body", None)
    if not isinstance(body, str) or not body:
        return {}
    ctype = _content_type(part.headers)
    encoding = getattr(part, "body_encoding", "utf-8") or "utf-8"
    kind = body_mod.kind_of(body, ctype, encoding)
    if kind is None:
        if body_mod.worth_naming(ctype, encoding):
            label = ctype.split(";", 1)[0].strip().lower() or "untyped"
            unscanned.add(f"{side} body ({'binary' if encoding != 'utf-8' else label})")
        return {}
    if kind == "form":
        text, found = _query("?" + body, fields, shown, fragment_ok=False)
        names |= found
        return {"body": text[1:]} if found else {}
    found: set[str] = set()
    text = body_mod.redact_json(body, fields, shown, _hide,
                                lambda tok: placeholder(tok, literal=True), found)
    names |= found
    return {"body": text} if found else {}


def _redact_request(request: Any, lists: Lists, shown: frozenset[str],
                    names: set[str], unscanned: set[str]) -> Any:
    headers, params, fields = lists
    url, in_url = _query(request.url, params, shown)
    path, in_path = _query(request.path, params, shown)
    names |= in_url | in_path
    return request.model_copy(update={
        "url": url, "path": path, "headers": _head(request.headers, headers, shown, names),
        **_body_update(request, "request", fields, shown, names, unscanned)})


def _redact_one(flow: Any, lists: Lists, shown: frozenset[str],
                names: set[str], unscanned: set[str]) -> Any:
    token = _source.set(_flow_source(flow))
    try:
        return _redact_one_in(flow, lists, shown, names, unscanned)
    finally:
        _source.reset(token)


def _flow_source(flow: Any) -> tuple[str, Any] | None:
    """The device and time a flow is attributed to, as flow visibility does --
    recorded only under blind sharing, where it decides who may restore."""
    from server import visibility

    if visibility.MODE != "blind":
        return None
    from server.trace import device_of

    try:
        from server.api.trace import read_ip_map

        ip_map = read_ip_map()
    except Exception:  # noqa: BLE001 -- unreadable: no physical flow is attributable
        ip_map = {}
    udid, firm = device_of(flow, ip_map)
    return (udid, getattr(flow, "timestamp", None)) if firm and udid else None


def _may_restore(vid: str) -> bool:
    """Whether the current caller may have placeholder `vid` put back: always
    under shared visibility, and for the owner. A blind client only while its
    view shows a flow the placeholder came from -- what reveal would show it."""
    from server import visibility

    if visibility.MODE != "blind":
        return True
    caller = visibility.current_caller()
    if caller.is_owner:
        return True
    view = visibility.data_view(caller)
    with _lock:
        sources = list(_sources.get(vid, {}).items())
    for key, when in sources:
        if isinstance(key, tuple):                # ("principal", P): a held request
            if key == ("principal", caller.principal):
                return True
        elif view.allows(key, when):
            return True
    return False


def _redact_one_in(flow: Any, lists: Lists, shown: frozenset[str],
                   names: set[str], unscanned: set[str]) -> Any:
    headers, _, fields = lists
    update: dict[str, Any] = {
        "request": _redact_request(flow.request, lists, shown, names, unscanned)}
    if flow.response is not None:
        update["response"] = flow.response.model_copy(update={
            "headers": _head(flow.response.headers, headers, shown, names),
            **_body_update(flow.response, "response", fields, shown, names, unscanned)})
    return flow.model_copy(update=update)


def redact_requests(requests: list[Any], reveal: Any = None) -> tuple[list[Any], list[str]]:
    """Copies of FlowRequests -- a held flow's -- as shown, and the names
    redacted in any of them."""
    out, names, _ = shown_requests(requests, reveal)
    return out, names


def shown_requests(requests: list[Any], reveal: Any = None
                   ) -> tuple[list[Any], list[str], list[str]]:
    """`redact_requests`, and the bodies shown as captured (see `shown_flows`).

    Held requests: under blind sharing a caller is shown only those its own
    intercepts hold (`list_held_flows`), and may release only those, so their
    placeholders are the caller's principal's to put back (#756 review) --
    a held request has no stored flow to attribute it to a device by."""
    with shown_to_caller():
        return _shown_requests_in(requests, reveal)


@contextlib.contextmanager
def shown_to_caller() -> Iterator[None]:
    """Placeholders made inside are the current caller's principal's to put
    back, under blind sharing: for what quern shows a caller of its own --
    the requests its intercepts hold, the mocks it set -- where there is no
    stored flow to attribute a value to a device by (#756 review)."""
    from server import visibility

    # (source, time): the source is the principal itself, and no time.
    token = _source.set((("principal", visibility.current_caller().principal), None)
                        if visibility.MODE == "blind" else None)
    try:
        yield
    finally:
        _source.reset(token)


def _shown_requests_in(requests: list[Any], reveal: Any = None
                       ) -> tuple[list[Any], list[str], list[str]]:
    on, headers, params, fields = settings()
    shown = reveal_set(reveal)
    if not on or shown is ALL:
        return list(requests), [], []
    shown = shown or frozenset()
    names: set[str] = set()
    unscanned: set[str] = set()
    out = [_redact_request(r, (headers, params, fields), shown, names, unscanned)
           for r in requests]
    return out, sorted(names), sorted(unscanned)


def redact_headers(h: dict[str, str]) -> dict[str, str]:
    """Header values as shown, for headers quern holds rather than captured
    -- a mock's. A mock set from placeholders holds the real values (see
    `restore`), so listing it must not show them either."""
    on, headers, _, _ = settings()
    if not on or not h:
        return h
    return _head(h, headers, frozenset(), set())


def redact_body_text(body: str | None, headers: dict[str, str] | None) -> str | None:
    """A body quern holds rather than captured -- a mock's -- as shown, its
    secret fields redacted (#749). A mock set from placeholders holds the
    real values, as its headers do (`redact_headers`)."""
    on, _, _, fields = settings()
    if not on or not body:
        return body
    from types import SimpleNamespace

    part = SimpleNamespace(body=body, headers=headers or {}, body_encoding="utf-8")
    return _body_update(part, "mock", fields, frozenset(), set(), set()).get("body", body)


def redact_urls(item: Any, reveal: Any = None) -> Any:
    """A copy of anything with `url` and `path` -- a FlowEvent on the stream
    -- with secret query parameters redacted, as `redact_flow` shows them."""
    on, _, params, _ = settings()
    shown = reveal_set(reveal)
    if not on or shown is ALL:
        return item
    shown = shown or frozenset()
    return item.model_copy(update={"url": _query(item.url, params, shown)[0],
                                   "path": _query(item.path, params, shown)[0]})


def redact_summaries(items: list[Any], reveal: Any = None) -> tuple[list[Any], list[str]]:
    """Copies of flow summary rows (`url`, `path`) as shown, and the names
    redacted in them: a capture stopped with detail "summary" returns rows,
    not flows (review)."""
    on, _, params, _ = settings()
    shown = reveal_set(reveal)
    if not on or shown is ALL:
        return list(items), []
    shown = shown or frozenset()
    names: set[str] = set()
    out = []
    for item in items:
        url, a = _query(item.url, params, shown)
        path, b = _query(item.path, params, shown)
        names |= a | b
        out.append(item.model_copy(update={"url": url, "path": path}))
    return out, sorted(names)


# -- the way back ---------------------------------------------------------------


def restore(text: str) -> tuple[str, int]:
    """`text` with each placeholder put back to the real value it stands for,
    and how many were. UnknownPlaceholder for one this server does not know:
    sending it on would fail in a way that looks like the app's own bug."""
    count = 0

    def back(m: re.Match) -> str:
        nonlocal count
        with _lock:
            value = _seen.get(m.group(1))
        # Refused alike when the caller may not have it: the answer must not
        # say a placeholder exists that this caller was never shown.
        if value is None or not _may_restore(m.group(1)):
            raise UnknownPlaceholder(
                f"the redacted value {m.group(0)} is not known to this server -- it may "
                "come from another server, or from long enough ago to have been let go. "
                "Pass the real value, or read the flow again with reveal to get it")
        count += 1
        return value

    return _PLACEHOLDER.sub(back, text), count


_QUOTED_PLACEHOLDER = re.compile(r'"(‹redacted, [\d,]+ chars, id ([0-9a-f]{12})›)"')


_ESCAPED_PLACEHOLDER = re.compile(
    r"\\u2039(redacted, [\d,]+ chars, id [0-9a-f]{12})\\u203[aA]")


def restore_body(text: str | None) -> tuple[str | None, int]:
    """A body sent back -- a replay's `modify_body`, a release's or a mock's
    -- with each placeholder put back (#749), and how many were. A quoted
    placeholder that stands for a JSON literal loses its quotes again, so
    `"pin": 1234`, redacted to a string, goes back as the number it was.
    UnknownPlaceholder as `restore`."""
    if not text:
        return text, 0
    # A JSON body re-encoded with ASCII escapes -- json.dumps' default --
    # carries `\u2039...\u203a` for the delimiters: read as a placeholder
    # too, or a known one would be sent as text and an unknown one would
    # skip its refusal (#753 review).
    text = _ESCAPED_PLACEHOLDER.sub(r"‹\1›", text)
    if "‹redacted" not in text:
        return text, 0
    count = 0

    def literal(m: re.Match) -> str:
        nonlocal count
        with _lock:
            value = _seen.get(m.group(2)) if m.group(2) in _literal_ids else None
        if value is None or not _may_restore(m.group(2)):
            return m.group(0)                     # a string's, or refused: `restore` below
        count += 1
        return value

    text = _QUOTED_PLACEHOLDER.sub(literal, text)
    text, more = restore(text)
    return text, count + more


def restore_headers(headers: dict[str, str] | None) -> tuple[dict[str, str] | None, list[str]]:
    """Headers with placeholders put back, and the names that had one."""
    if not headers:
        return headers, []
    out, names = {}, []
    for k, v in headers.items():
        if isinstance(v, str) and _PLACEHOLDER.search(v):
            out[k], _ = restore(v)
            names.append(k)
        else:
            out[k] = v
    return out, names


def restored_note(names: list[str]) -> str | None:
    """What an answer says when it put redacted values back."""
    if not names:
        return None
    return (f"{', '.join(names)} carried a redacted placeholder; quern sent the real "
            "value it stands for (#742, #749).")


def _forget_for_tests() -> None:
    global _key_cache, _settings_cache
    with _lock:
        _seen.clear()
        _literal_ids.clear()
        _sources.clear()
    _key_cache = None
    _settings_cache = None
