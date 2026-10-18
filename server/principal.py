"""Who is calling, by credential: the owner, a client, or nobody (#254, phase 2).

The credential decides, never the network address (decided 2026-10-18): the
owner's API key is the owner wherever it comes from, and a client key issued
with `quern clients add` is that client even on 127.0.0.1. So two MCPs on the
owner's own Mac, each with a client key, exercise exactly what two remote
machines would.

Set by `APIKeyMiddleware` for every request and WebSocket; read through
`visibility.current_caller()`. Work outside a request -- a sweep, a stream
restart -- is the server's own, so the default is the owner.
"""

from __future__ import annotations

import contextvars

from server.session_registry import OWNER

#: A request with no valid credential, which only public paths admit.
ANONYMOUS = "anonymous"
_CLIENT = "client:"

_PRINCIPAL: contextvars.ContextVar[str] = contextvars.ContextVar(
    "quern_principal", default=OWNER)


def current_principal() -> str:
    return _PRINCIPAL.get()


def set_principal(principal: str) -> contextvars.Token:
    return _PRINCIPAL.set(principal)


def reset_principal(token: contextvars.Token) -> None:
    _PRINCIPAL.reset(token)


def client(label: str) -> str:
    """The principal a client key's holder acts as."""
    return f"{_CLIENT}{label}"


def label_of(principal: str) -> str | None:
    """A client principal's label; None for the owner or anyone else."""
    return principal[len(_CLIENT):] if principal.startswith(_CLIENT) else None


def qualify(raw: str, principal: str | None = None) -> str:
    """A session id as the server knows it: a client's ids carry its label.

    A client names its sessions as it likes -- the MCP sends one id as the
    header, the open request's name and the socket's path -- but cannot be
    trusted to stay out of another client's names. So the server prefixes
    every id a client gives with `<label>:`, and its sessions, reservations
    and active-device slot live in its own namespace. Idempotent: an id the
    server handed back already carries the prefix. The owner's ids are its
    own, as in phase 1 ("name, don't gate")."""
    label = label_of(principal if principal is not None else current_principal())
    if label is None or not raw:
        return raw
    prefix = f"{label}:"
    return raw if raw.startswith(prefix) else prefix + raw
