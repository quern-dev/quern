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
    from server.config import read_user_config

    raw = read_user_config().get("proxy")
    raw = raw if isinstance(raw, dict) else {}

    def extra(key: str) -> frozenset[str]:
        names = raw.get(key)
        if not isinstance(names, list):
            return frozenset()
        return frozenset(n.strip().lower() for n in names if isinstance(n, str) and n.strip())

    return (raw.get("redact") is not False, HEADERS | extra("redact_headers"),
            PARAMS | extra("redact_params"))


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


def _header_value(name: str, value: str) -> str:
    if not value or _PLACEHOLDER.search(value):
        return value                              # empty, or already shown redacted
    if name == "cookie":
        # Each cookie's name stays: which cookies a request carried is often
        # the question, and the names are not the secret.
        return "; ".join(
            f"{k}={placeholder(v)}" if v else part
            for part in value.split(";")
            for k, _, v in [part.strip().partition("=")]
            if part.strip())
    if name == "set-cookie":
        return _SET_COOKIE.sub(
            lambda m: f"{m.group(1)}{m.group(2)}={placeholder(m.group(3))}"
            if m.group(3) else m.group(0), value)
    scheme = _SCHEME.match(value)
    if scheme:
        return f"{scheme.group(1)}{scheme.group(2)}{placeholder(scheme.group(3))}"
    return placeholder(value)


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
        if eq and val and name in params and name not in reveal \
                and not _PLACEHOLDER.search(val):
            out.append(f"{key}={placeholder(val)}")
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


def redact_flow(flow: Any, reveal: Any = None) -> tuple[Any, list[str]]:
    """A copy of `flow` (a FlowRecord) as shown, and the names redacted in it.
    The stored record is never changed: replay and mocks need the real
    values."""
    flows, names = redact_flows([flow], reveal)
    return flows[0], names


def redact_flows(flows: list[Any], reveal: Any = None) -> tuple[list[Any], list[str]]:
    """Copies of `flows` as shown, and every name redacted in any of them.
    The settings are read once for the lot, not once a flow."""
    on, headers, params = settings()
    shown = reveal_set(reveal)
    if not on or shown is ALL:
        return list(flows), []
    shown = shown or frozenset()
    names: set[str] = set()
    out = [_redact_one(f, headers, params, shown, names) for f in flows]
    return out, sorted(names)


def _redact_one(flow: Any, headers: frozenset[str], params: frozenset[str],
                shown: frozenset[str], names: set[str]) -> Any:
    def head(h: dict[str, str]) -> dict[str, str]:
        out = {}
        for k, v in h.items():
            low = k.lower()
            if low in headers and low not in shown and v:
                out[k] = _header_value(low, v)
                names.add(low)
            else:
                out[k] = v
        return out

    request = flow.request
    url, in_url = _query(request.url, params, shown)
    path, in_path = _query(request.path, params, shown)
    names |= in_url | in_path
    update: dict[str, Any] = {"request": request.model_copy(update={
        "url": url, "path": path, "headers": head(request.headers)})}
    if flow.response is not None:
        update["response"] = flow.response.model_copy(
            update={"headers": head(flow.response.headers)})
    return flow.model_copy(update=update)


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
    global _key_cache
    with _lock:
        _seen.clear()
    _key_cache = None
