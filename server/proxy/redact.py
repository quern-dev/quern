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

Bodies are not redacted (#749): answers say so, so nobody assumes they are.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
import threading
from collections import OrderedDict
from typing import Any
from urllib.parse import unquote_plus

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
NOTE = ("Secret headers and query-string parameters are redacted: pass reveal "
        "(names, or true) to see them. Bodies are not redacted (#749).")

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
_settings_cache: tuple[tuple, tuple[bool, frozenset[str], frozenset[str]]] | None = None


class UnknownPlaceholder(ValueError):
    """A placeholder whose real value this server does not know: from another
    server, or remembered so long ago it has been let go."""


# -- settings -----------------------------------------------------------------


def settings() -> tuple[bool, frozenset[str], frozenset[str]]:
    """(on, headers, params) from config.json's `proxy` object:

        {"proxy": {"redact": false, "redact_headers": ["X-Shazam-Token"],
                   "redact_params": ["session"]}}

    `redact_headers` and `redact_params` *add* to the defaults rather than
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
              PARAMS | extra("redact_params"))
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


def placeholder(value: str) -> str:
    """`value` as shown, remembered so `restore` can put it back."""
    vid = fingerprint(value)
    with _lock:
        _seen[vid] = value
        _seen.move_to_end(vid)
        while len(_seen) > SEEN_MAX:
            _seen.popitem(last=False)
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


def _query(text: str, params: frozenset[str], reveal: frozenset[str]) -> tuple[str, set[str]]:
    """`text` -- a URL or a path -- with secret query parameters' values
    redacted, and the names redacted. Only the values change, character for
    character elsewhere: the URL is shown, not re-encoded."""
    before, sep, rest = text.partition("?")
    if not sep:
        return text, set()
    query, hashsep, fragment = rest.partition("#")
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
    on, _, params = settings()
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
    on, headers, params = settings()
    shown = reveal_set(reveal)
    if (not on and not force) or shown is ALL:
        return list(flows), []
    shown = shown or frozenset()
    names: set[str] = set()
    out = [_redact_one(f, headers, params, shown, names) for f in flows]
    return out, sorted(names)


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


def _redact_request(request: Any, headers: frozenset[str], params: frozenset[str],
                    shown: frozenset[str], names: set[str]) -> Any:
    url, in_url = _query(request.url, params, shown)
    path, in_path = _query(request.path, params, shown)
    names |= in_url | in_path
    return request.model_copy(update={
        "url": url, "path": path, "headers": _head(request.headers, headers, shown, names)})


def _redact_one(flow: Any, headers: frozenset[str], params: frozenset[str],
                shown: frozenset[str], names: set[str]) -> Any:
    update: dict[str, Any] = {
        "request": _redact_request(flow.request, headers, params, shown, names)}
    if flow.response is not None:
        update["response"] = flow.response.model_copy(
            update={"headers": _head(flow.response.headers, headers, shown, names)})
    return flow.model_copy(update=update)


def redact_requests(requests: list[Any], reveal: Any = None) -> tuple[list[Any], list[str]]:
    """Copies of FlowRequests -- a held flow's -- as shown, and the names
    redacted in any of them."""
    on, headers, params = settings()
    shown = reveal_set(reveal)
    if not on or shown is ALL:
        return list(requests), []
    shown = shown or frozenset()
    names: set[str] = set()
    out = [_redact_request(r, headers, params, shown, names) for r in requests]
    return out, sorted(names)


def redact_headers(h: dict[str, str]) -> dict[str, str]:
    """Header values as shown, for headers quern holds rather than captured
    -- a mock's. A mock set from placeholders holds the real values (see
    `restore`), so listing it must not show them either."""
    on, headers, _ = settings()
    if not on or not h:
        return h
    return _head(h, headers, frozenset(), set())


def redact_urls(item: Any, reveal: Any = None) -> Any:
    """A copy of anything with `url` and `path` -- a FlowEvent on the stream
    -- with secret query parameters redacted, as `redact_flow` shows them."""
    on, _, params = settings()
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
    on, _, params = settings()
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
        if value is None:
            raise UnknownPlaceholder(
                f"the redacted value {m.group(0)} is not known to this server -- it may "
                "come from another server, or from long enough ago to have been let go. "
                "Pass the real value, or read the flow again with reveal to get it")
        count += 1
        return value

    return _PLACEHOLDER.sub(back, text), count


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
            "value it stands for (#742).")


def _forget_for_tests() -> None:
    global _key_cache, _settings_cache
    with _lock:
        _seen.clear()
    _key_cache = None
    _settings_cache = None
