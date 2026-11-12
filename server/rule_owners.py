"""Who set each mock rule and intercept, and from which session (#650).

The key is recorded as before, in `app.state.mock_owners` and
`intercept_owners` (#254, #646): a blind server lists a client's rules to it
alone, and a client's rules end with its hold on their device. Alongside it,
the held session that set the rule, and which opening of it: an owner-key
agent's rules end when its session does, whoever's they are -- left, one
agent's mock answered the next agent's requests with nothing to say whose it
was. A rule set with no held session (curl, a script) has none, and stays
until someone clears it.
"""

from __future__ import annotations

from typing import Any

from server import principal, visibility

_MAPS = {
    "mock": ("mock_owners", "mock_sessions"),
    "intercept": ("intercept_owners", "intercept_sessions"),
}


def _map(app: Any, name: str) -> dict:
    found = getattr(app.state, name, None)
    if found is None:
        found = {}
        setattr(app.state, name, found)
    return found


def capture(app: Any) -> tuple[str, tuple | None]:
    """The caller's key and held session (its id and opening), read when a
    request to set a rule arrives -- before anything is awaited, so a session
    that ends while the rule is being set is still the one it belongs to
    (CodeRabbit on #655). A session id the registry does not hold -- the CLI
    and scripts send one without opening a session -- is no session."""
    caller = visibility.current_caller()
    registry = getattr(app.state, "sessions", None)
    live = registry.get(caller.session) if registry is not None and caller.session else None
    held = (live.id, live.opened_at) if live is not None \
        and live.principal == caller.principal else None
    return caller.principal, held


def still_open(app: Any, captured: tuple[str, tuple | None]) -> bool:
    """Whether the session a rule was set from is still that opening; true
    for a rule set with no session."""
    held = captured[1]
    if held is None:
        return True
    registry = getattr(app.state, "sessions", None)
    live = registry.get(held[0]) if registry is not None else None
    return live is not None and live.opened_at == held[1]


def record(app: Any, kind: str, rule_id: str,
           captured: tuple[str, tuple | None] | None = None) -> None:
    """Note who set `rule_id`: `captured` as `capture` read it when the
    request arrived, or the caller now."""
    owners, sessions = _MAPS[kind]
    key, held = captured if captured is not None else capture(app)
    _map(app, owners)[rule_id] = key
    if held is not None:
        _map(app, sessions)[rule_id] = held
    else:
        _map(app, sessions).pop(rule_id, None)


def forget(app: Any, kind: str, rule_id: str) -> None:
    for name in _MAPS[kind]:
        _map(app, name).pop(rule_id, None)


def forget_all(app: Any, kind: str) -> None:
    for name in _MAPS[kind]:
        _map(app, name).clear()


def label(app: Any, kind: str, rule_id: str) -> dict:
    """`owner`: the client key's label, or "owner" for the owner key, or None
    when quern does not know (a rule from before it recorded owners);
    `session`: the held session that set it, or None."""
    owners, sessions = _MAPS[kind]
    key = _map(app, owners).get(rule_id)
    held = _map(app, sessions).get(rule_id)
    return {
        "owner": None if key is None else (principal.label_of(key) or "owner"),
        "session": held[0] if held else None,
    }


def set_by(app: Any, kind: str, session_id: str, opened: Any) -> set[str]:
    """The rules this opening of a session set."""
    _, sessions = _MAPS[kind]
    return {rule_id for rule_id, held in _map(app, sessions).items()
            if held == (session_id, opened)}
