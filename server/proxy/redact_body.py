"""Secret fields in captured bodies, edited in place (#749).

Part of #742's redaction (see `server.proxy.redact`), for request and
response bodies: an OAuth token response's `access_token`, a login's
`password`, a form post's `client_secret`.

A JSON body is not parsed and re-serialized. A small scanner walks its tokens
and replaces only the value of each secret-named field, so everything else
stays exactly as captured -- whitespace, key order, escapes -- and a body that
was cut at the capture limit is scanned up to the cut. Re-serializing would
reformat every body an agent diffs, and cannot read a truncated one at all.

Pure: the caller passes the function that makes a placeholder, so this module
holds no secrets and no state.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable

#: Field names whose value is a credential, in JSON and form bodies, matched
#: case-insensitively at any depth. Not the query-string list: a bare `key` or
#: `sig` in a body is as often `{"key": "theme"}` as a secret.
FIELDS = frozenset({
    "access_token", "refresh_token", "id_token", "token", "password", "passwd",
    "client_secret", "api_key", "apikey", "secret", "private_key",
})

#: Media types with no field names to find: their bodies are not worth naming
#: as "not redacted" in an answer.
_OPAQUE_MEDIA = ("image/", "video/", "audio/", "font/")

#: A JSON literal: a number, true, false or null.
_LITERAL = re.compile(r"-?[0-9][0-9.eE+-]*|-|true|false|null|[A-Za-z]+")

Hide = Callable[[str], str]


def kind_of(body: str, content_type: str, encoding: str) -> str | None:
    """"json", "form", or None for a body this cannot scan."""
    if encoding != "utf-8":
        return None
    ct = content_type.split(";", 1)[0].strip().lower()
    if ct == "application/x-www-form-urlencoded":
        return "form"
    if ct.endswith("/json") or ct.endswith("+json"):
        return "json"
    # Apps mislabel: a JSON body sent as text/plain, or with no type at all.
    if ct in ("", "text/plain") and body.lstrip()[:1] in ("{", "["):
        return "json"
    return None


def worth_naming(content_type: str, encoding: str) -> bool:
    """Whether a body left unscanned should be named in the answer: not an
    image, video, audio or font, which hold no named fields."""
    ct = content_type.split(";", 1)[0].strip().lower()
    return not ct.startswith(_OPAQUE_MEDIA)


def redact_json(text: str, fields: frozenset[str], shown: frozenset[str],
                hide: Hide, hide_literal: Hide, names: set[str]) -> str:
    """`text` with the value of each secret-named field replaced, in place.

    A string value becomes a placeholder string. A literal (`"pin": 1234`)
    becomes a placeholder string too, through `hide_literal`, so the way back
    can tell it was not a string. An array under a secret name has each of
    its strings and literals replaced; an object under one is walked by name
    like any other, since GraphQL `variables` and the like nest. A string cut
    off by truncation is replaced up to the cut.
    """
    out: list[str] = []
    # Each frame: ["o", expecting_key, secret_name | None] for an object,
    # ["a", secret_name | None] for an array.
    stack: list[list] = []
    i, n = 0, len(text)

    def secret_value() -> str | None:
        """The secret name the next value is under, if it is."""
        if not stack:
            return None
        top = stack[-1]
        return top[2] if top[0] == "o" else top[1]

    def value_done() -> None:
        if stack and stack[-1][0] == "o":
            stack[-1][2] = None

    while i < n:
        c = text[i]
        if c == '"':
            j, closed = _string_end(text, i)
            raw = text[i + 1:j - 1] if closed else text[i + 1:]
            top = stack[-1] if stack else None
            if top is not None and top[0] == "o" and top[1]:
                name = _decoded(raw).lower()
                top[1] = False
                top[2] = name if name in fields and name not in shown else None
                out.append(text[i:j])
            else:
                name = secret_value()
                if name is not None and raw:
                    out.append(f'"{hide(raw)}' + ('"' if closed else ""))
                    names.add(name)
                else:
                    out.append(text[i:j])
                value_done()
            i = j
            continue
        if c in "{[":
            name = secret_value()
            value_done()
            stack.append(["o", True, None] if c == "{" else ["a", name])
            out.append(c)
            i += 1
            continue
        if c in "}]":
            if stack:
                stack.pop()
            out.append(c)
            i += 1
            continue
        if c == ",":
            if stack and stack[-1][0] == "o":
                stack[-1][1], stack[-1][2] = True, None
            out.append(c)
            i += 1
            continue
        m = _LITERAL.match(text, i) if c not in " \t\r\n:" else None
        if m:
            name = secret_value()
            if name is not None:
                out.append(f'"{hide_literal(m.group(0))}"')
                names.add(name)
            else:
                out.append(m.group(0))
            value_done()
            i = m.end()
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _string_end(text: str, i: int) -> tuple[int, bool]:
    """The index just past the string starting at `text[i]` (a quote), and
    whether it closed before the text ended."""
    j, n = i + 1, len(text)
    while j < n:
        c = text[j]
        if c == "\\":
            j += 2
        elif c == '"':
            return j + 1, True
        else:
            j += 1
    return n, False


def _decoded(raw: str) -> str:
    """A JSON string's content with its escapes read: `"pass\\u0077ord"` is
    the name password."""
    try:
        return json.loads(f'"{raw}"')
    except ValueError:
        return raw
