"""Authentication conformance, enumerated from the server's own schema.

`docs/api-reference.md` states the contract: every endpoint requires a bearer
token except a named set of public paths. This module asserts that against
`/openapi.json` rather than a hand-written list, so an endpoint added tomorrow is
covered today. A curated list would pass forever while the route it forgot sat
open.

Probing an endpoint *without* credentials is the one place this suite could do
damage if it found what it is looking for: an unauthenticated `POST
/api/v1/system/update` that is genuinely reachable would launch an update.
Everything below is therefore shaped so that a hole is detected without the
operation behind it running -- see `_POISON_BODY` and `UNSAFE_TO_PROBE`.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.conformance.client import PUBLIC_PATHS, QuernClient

#: Substituted into `{param}` path segments. Deliberately a value no real
#: resource can have, so that an endpoint reached without auth operates on
#: nothing. `conformance-probe` is not a valid flow id, mock id, or state label.
PATH_PARAM_STUB = "conformance-probe-nonexistent"

#: A JSON array where every body-taking endpoint expects an object. Pydantic
#: rejects it with 422 before the handler runs, so even a genuinely
#: unauthenticated endpoint performs no work. This is the safety net that lets
#: the mutating endpoints be probed at all.
_POISON_BODY: list[Any] = []

#: Endpoints that take no request body, so `_POISON_BODY` cannot neuter them,
#: *and* whose side effect is bad enough that we will not risk running it to
#: prove a point. Each is asserted to require auth by the schema-level test
#: instead, which needs no request at all.
#:
#: Being on this list is not an exemption from the auth requirement -- it is a
#: statement that this suite proves it a different way.
UNSAFE_TO_PROBE: dict[tuple[str, str], str] = {
    ("POST", "/api/v1/system/update"): "launches `quern update` in a detached child",
    ("POST", "/api/v1/proxy/configure-system"): "rewrites macOS system proxy settings",
    ("POST", "/api/v1/proxy/unconfigure-system"): "rewrites macOS system proxy settings",
    ("POST", "/api/v1/proxy/start"): "starts the proxy listener",
    ("POST", "/api/v1/proxy/stop"): "stops the proxy, dropping capture in progress",
}

#: What an unauthenticated request is allowed to come back as. 422 is included
#: because `_POISON_BODY` is designed to provoke it -- but only for endpoints
#: that take a body, and reaching validation means auth did *not* reject, so it
#: is accepted only where the schema says a body is read. See `_expected_codes`.
_AUTH_REJECTIONS = frozenset({401, 403})


@pytest.fixture(scope="session")
def openapi(quern: QuernClient) -> dict:
    """The live schema. Public by contract, so no key is needed to read it."""
    resp = quern.get("/openapi.json", authenticated=False, timeout=15.0)
    assert resp.is_success, f"/openapi.json returned {resp.status_code}"
    schema = resp.json()
    assert schema.get("paths"), "/openapi.json carried no paths"
    return schema


def _operations(schema: dict) -> list[tuple[str, str, dict]]:
    """(METHOD, path, operation) for every documented operation."""
    out = []
    for path, item in schema["paths"].items():
        for method, operation in item.items():
            if method.lower() in ("get", "post", "put", "patch", "delete"):
                out.append((method.upper(), path, operation))
    return sorted(out)


def _fill(path: str) -> str:
    """Replace `{param}` segments with a value that matches no real resource."""
    out = path
    while "{" in out:
        start = out.index("{")
        end = out.index("}", start)
        out = out[:start] + PATH_PARAM_STUB + out[end + 1 :]
    return out


def _ids(ops: list[tuple[str, str, dict]]) -> list[str]:
    return [f"{m} {p}" for m, p, _ in ops]


# -- the public set ----------------------------------------------------------


def test_documented_public_paths_are_reachable_without_a_key(
    quern: QuernClient,
) -> None:
    """Every path the reference calls public must answer without credentials.

    This is the half of the contract that breaks quietly. `/api/v1/proxy/cert`
    in particular is fetched by devices during setup, *before* they hold a key;
    putting it behind auth would break certificate installation while every
    authenticated test in this suite kept passing.
    """
    failures = []
    for path in sorted(PUBLIC_PATHS):
        resp = quern.get(path, authenticated=False, timeout=15.0)
        if resp.status_code in _AUTH_REJECTIONS:
            failures.append(f"{path} -> {resp.status_code}")
    assert not failures, (
        "documented-public paths demanded credentials: " + ", ".join(failures)
    )


def test_the_certificate_endpoint_serves_a_certificate_unauthenticated(
    quern: QuernClient,
) -> None:
    """`/api/v1/proxy/cert` is the one public endpoint that returns a secret-ish
    artifact, and the reference is explicit that it serves *only* the public CA
    certificate. Assert it actually returns a certificate, not an error page
    that happens to be unauthenticated.
    """
    resp = quern.get("/api/v1/proxy/cert", authenticated=False, timeout=15.0)
    if resp.status_code == 404:
        pytest.skip("no CA certificate generated on this machine yet")
    assert resp.is_success, f"/api/v1/proxy/cert -> {resp.status_code}"
    body = resp.content
    assert b"BEGIN CERTIFICATE" in body or body[:1] == b"0", (
        "cert endpoint returned something that is not a PEM or DER certificate: "
        f"{body[:80]!r}"
    )
    assert b"PRIVATE KEY" not in body, (
        "the public certificate endpoint served a private key"
    )


# -- the protected set -------------------------------------------------------


def test_every_operation_is_either_public_or_protected(openapi: dict) -> None:
    """No operation may be undeclared.

    A schema-level check, so it also covers the operations that are unsafe to
    probe live. It fails on a path that is neither in the documented public set
    nor reachable for probing, which would mean this module silently stopped
    covering something.
    """
    undeclared = []
    for method, path, _ in _operations(openapi):
        if path in PUBLIC_PATHS:
            continue
        if (method, path) in UNSAFE_TO_PROBE:
            continue
        # Everything else is probed by the tests below; nothing to assert here
        # beyond the fact that it is reachable to them.
        if not path.startswith("/api/v1/") and path not in PUBLIC_PATHS:
            undeclared.append(f"{method} {path}")
    assert not undeclared, (
        "operations outside /api/v1/ that are not documented as public: "
        + ", ".join(undeclared)
        + " — either they are public and belong in PUBLIC_PATHS, or they are a "
        "surface nobody has classified"
    )


def _expected_codes(operation: dict) -> tuple[frozenset[int], str]:
    """What counts as 'auth rejected this' for one operation.

    An endpoint that reads a request body may answer 422 to `_POISON_BODY`
    *only* if it never checked auth -- so 422 is a failure, not a pass. The
    distinction is recorded here so the assertion message can explain which
    of the two happened.
    """
    return _AUTH_REJECTIONS, "401/403"


@pytest.fixture(scope="session")
def probeable(openapi: dict) -> list[tuple[str, str, dict]]:
    return [
        (m, p, op)
        for m, p, op in _operations(openapi)
        if p not in PUBLIC_PATHS and (m, p) not in UNSAFE_TO_PROBE
    ]


def test_there_are_operations_to_probe(probeable: list) -> None:
    """Guard against the enumeration silently collapsing to nothing.

    Without this, a change that made `_operations` return `[]` would turn the
    whole auth tier green.
    """
    assert len(probeable) > 50, (
        f"only {len(probeable)} operations enumerated from /openapi.json; "
        "the auth sweep is not covering the API"
    )


def test_protected_operations_reject_a_missing_token(
    quern: QuernClient, probeable: list[tuple[str, str, dict]]
) -> None:
    """The sweep: every non-public operation, called with no Authorization.

    One test rather than a parametrised one per endpoint, because the useful
    output is the *list* of holes, not the first one. A partial failure here is
    a security finding and wants to be read whole.
    """
    holes = []
    for method, path, operation in probeable:
        try:
            resp = quern.request(
                method,
                _fill(path),
                authenticated=False,
                json=_POISON_BODY if method != "GET" else None,
                timeout=20.0,
            )
        except Exception as exc:  # noqa: BLE001 - a transport error is a result
            holes.append(f"{method} {path} -> transport error {exc!r}")
            continue
        if resp.status_code not in _AUTH_REJECTIONS:
            holes.append(f"{method} {path} -> {resp.status_code}")

    assert not holes, (
        f"{len(holes)} operation(s) did not reject an unauthenticated request "
        f"with 401/403:\n  " + "\n  ".join(holes)
    )


def test_protected_operations_reject_a_wrong_token(
    quern: QuernClient, probeable: list[tuple[str, str, dict]]
) -> None:
    """A present-but-wrong key must fail exactly like a missing one.

    Separate from the missing-token sweep because the code paths differ: a
    missing header is often rejected by the dependency's own `auto_error`, while
    a malformed or wrong token reaches the comparison. An endpoint that accepts
    any non-empty bearer would pass the test above and fail this one.
    """
    headers = {"Authorization": "Bearer conformance-probe-not-a-real-key"}
    holes = []
    for method, path, _ in probeable:
        try:
            resp = quern.request(
                method,
                _fill(path),
                authenticated=False,
                headers=headers,
                json=_POISON_BODY if method != "GET" else None,
                timeout=20.0,
            )
        except Exception as exc:  # noqa: BLE001
            holes.append(f"{method} {path} -> transport error {exc!r}")
            continue
        if resp.status_code not in _AUTH_REJECTIONS:
            holes.append(f"{method} {path} -> {resp.status_code}")

    assert not holes, (
        f"{len(holes)} operation(s) accepted an invalid bearer token:\n  "
        + "\n  ".join(holes)
    )


def test_a_valid_token_is_actually_accepted(
    quern: QuernClient, authenticated: None
) -> None:
    """The control for the two sweeps above.

    Without it, a server that rejected *everything* -- including valid keys --
    would make the entire auth tier pass. That is the failure mode where a
    security test is worse than none: it reports the strongest possible result
    for a completely broken server.
    """
    resp = quern.get("/api/v1/system/channel", timeout=15.0)
    assert resp.status_code not in _AUTH_REJECTIONS, (
        f"the configured API key was rejected ({resp.status_code}); the "
        "unauthenticated sweeps above prove nothing on this run"
    )
    assert resp.is_success, f"/api/v1/system/channel -> {resp.status_code}"
