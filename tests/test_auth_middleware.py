"""`APIKeyMiddleware`: what it lets through, what it refuses, and disconnects.

Written against the `BaseHTTPMiddleware` version first, so the rewrite to pure
ASGI (#208) had to reproduce it exactly rather than be described by tests
written afterwards. The one deliberate difference is the empty-key guard, which
is marked where it appears.

The disconnect tests are the point of the rewrite: through
`BaseHTTPMiddleware`, `request.is_disconnected()` never reports a disconnect,
so `_run_until_client_leaves` in `server/api/device_ui.py` could not work in
production however correct it was.
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from server.auth import APIKeyMiddleware

KEY = "test-key-2f8a"
PUBLIC_PATHS = (
    "/", "/health", "/api/v1/health", "/tools", "/docs", "/redoc",
    "/openapi.json", "/api/v1/proxy/cert", "/video-test",
)


def _app(api_key: str = KEY) -> FastAPI:
    app = FastAPI()
    app.add_middleware(APIKeyMiddleware, api_key=api_key)

    @app.get("/api/v1/device/list")
    async def protected():
        return {"status": "ok"}

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    return app


@pytest.fixture
def client() -> TestClient:
    return TestClient(_app())


# -- what gets through -------------------------------------------------------


@pytest.mark.parametrize("path", PUBLIC_PATHS)
def test_public_paths_need_no_key(path):
    """A 404 is fine here -- these tests are about the 401 that must not
    happen. Several of the public paths have no route in this app."""
    with TestClient(_app()) as client:
        assert client.get(path).status_code != 401


@pytest.mark.parametrize("header", [
    {"Authorization": f"Bearer {KEY}"},
    {"X-API-Key": KEY},
    {"authorization": f"Bearer {KEY}"},      # ASGI header names are lowercase
    {"x-api-key": KEY},
    {"AUTHORIZATION": f"Bearer {KEY}"},
    {"X-Api-Key": KEY},
])
def test_a_valid_key_is_accepted_in_either_header_in_any_case(client, header):
    assert client.get("/api/v1/device/list", headers=header).status_code == 200


def test_a_valid_key_wins_even_when_another_header_is_wrong(client):
    response = client.get(
        "/api/v1/device/list",
        headers={"Authorization": "Bearer nope", "X-API-Key": KEY},
    )
    assert response.status_code == 200


# -- what is refused ---------------------------------------------------------


@pytest.mark.parametrize("headers", [
    {},
    {"Authorization": "Bearer wrong-key"},
    {"Authorization": f"Bearer {KEY} "},          # trailing space is not the key
    {"Authorization": KEY},                       # no scheme
    {"Authorization": f"Basic {KEY}"},            # wrong scheme
    {"Authorization": f"bearer {KEY}"},           # scheme is matched case-sensitively
    {"X-API-Key": "wrong-key"},
    {"X-API-Key": ""},
    {"X-API-Key": f"{KEY}x"},
])
def test_a_missing_or_wrong_key_is_refused(client, headers):
    assert client.get("/api/v1/device/list", headers=headers).status_code == 401


def test_the_refusal_body_is_unchanged(client):
    """Clients match on this; the rewrite hand-builds the response that
    `JSONResponse` used to send."""
    response = client.get("/api/v1/device/list")

    assert response.status_code == 401
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {"detail": "Invalid or missing API key"}
    assert response.headers["content-length"] == str(len(response.content))
    assert response.content == json.dumps(
        {"detail": "Invalid or missing API key"}, separators=(",", ":"),
    ).encode()


def test_a_public_path_is_matched_exactly(client):
    """`/health` is public; `/health/secrets` is not."""
    assert client.get("/health/secrets").status_code == 401


def test_an_empty_configured_key_authorises_nobody():
    """The deliberate change from the BaseHTTPMiddleware version.

    It compared `X-API-Key` against the configured key using `""` as the
    default for a missing header, so an empty configured key accepted every
    request that sent no header at all. `ServerConfig` always generates a key,
    so this is a guard against a state that should not arise rather than a
    reachable bug -- but "should not arise" is what the old code assumed.
    """
    with TestClient(_app(api_key="")) as client:
        assert client.get("/api/v1/device/list").status_code == 401
        assert client.get(
            "/api/v1/device/list", headers={"X-API-Key": ""},
        ).status_code == 401


@pytest.mark.parametrize("sent", ["prefix", "suffix"])
def test_a_key_that_merely_starts_or_extends_the_real_one_is_refused(client, sent):
    """A truncated key must not authenticate: comparing with `startswith`
    rather than an equality would accept every prefix of the real key, and
    `Bearer q` would be enough."""
    token = KEY[:-1] if sent == "prefix" else KEY + "x"

    assert client.get(
        "/api/v1/device/list", headers={"Authorization": f"Bearer {token}"},
    ).status_code == 401
    assert client.get(
        "/api/v1/device/list", headers={"X-API-Key": token},
    ).status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("name", [b"authorization", b"x-api-key"])
@pytest.mark.parametrize("order,expected", [
    (("good", "bad"), 200),
    (("bad", "good"), 401),
])
async def test_a_repeated_header_is_read_the_way_starlette_reads_it(
    name, order, expected,
):
    """First occurrence wins, as `Headers.get` does. Taking the last instead
    would let a second header override the first, which is the shape of a
    request-smuggling trick."""
    values = {
        b"authorization": {"good": f"Bearer {KEY}".encode(), "bad": b"Bearer wrong"},
        b"x-api-key": {"good": KEY.encode(), "bad": b"wrong"},
    }[name]

    status = []

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def send(message):
        if message["type"] == "http.response.start":
            status.append(message["status"])

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/v1/device/list",
        "headers": [(name, values[which]) for which in order],
    }
    await APIKeyMiddleware(app, api_key=KEY)(scope, receive, send)

    assert status == [expected]


async def _status(scope, api_key=KEY):
    """Drive the middleware directly, for requests a test client cannot send."""
    codes = []

    async def app(_scope, _receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def send(message):
        if message["type"] == "http.response.start":
            codes.append(message["status"])

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    await APIKeyMiddleware(app, api_key=api_key)(scope, receive, send)
    return codes[0]


def _get(path="/api/v1/device/list", headers=(), method="GET"):
    return {"type": "http", "method": method, "path": path, "headers": list(headers)}


@pytest.mark.asyncio
async def test_a_key_with_characters_outside_latin_1_is_compared_whole():
    """The bypass a review of #208 found in the first version of this.

    It encoded the configured key as latin-1 with errors="ignore", so every
    character that would not fit was *dropped* before the comparison: a key of
    `abc<emoji>def` was satisfied by `abcdef`, and a key made only of emoji
    encoded to nothing, which an empty bearer token matched. ~/.quern/api-key
    is a file a user can edit, so a pasted smart quote is enough to reach it.
    """
    key = "abc\U0001F511def"

    assert await _status(_get(headers=[(b"x-api-key", b"abcdef")]), key) == 401
    assert await _status(_get(headers=[(b"x-api-key", key.encode())]), key) == 200
    assert await _status(
        _get(headers=[(b"authorization", b"Bearer " + key.encode())]), key,
    ) == 200


@pytest.mark.asyncio
async def test_a_key_that_encodes_to_nothing_authorises_nobody():
    key = "\U0001F511\U0001F511"

    assert await _status(_get(headers=[(b"authorization", b"Bearer ")]), key) == 401
    assert await _status(_get(headers=[(b"x-api-key", b"")]), key) == 401
    assert await _status(_get(headers=[(b"x-api-key", key.encode())]), key) == 200


@pytest.mark.asyncio
async def test_the_bearer_scheme_needs_its_space():
    """`Bearer` without the separator is not the scheme: matching on it would
    read the key from one byte into the value."""
    assert await _status(
        _get(headers=[(b"authorization", b"BearerX" + KEY.encode())]),
    ) == 401


@pytest.mark.asyncio
async def test_a_scope_without_headers_is_refused_not_crashed():
    """Not every scope carries a header list -- a malformed or synthetic one
    may omit it, and an unauthenticated crash is still a failure."""
    assert await _status({"type": "http", "method": "GET", "path": "/api/v1/x"}) == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["OPTIONS", "HEAD", "DELETE", "POST"])
async def test_no_method_is_exempt(method):
    """CORS answers preflights above auth in production, so an OPTIONS
    exemption here would be invisible rather than harmless."""
    assert await _status(_get(method=method)) == 401


# -- scopes and disconnects --------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("scope_type", ["lifespan", "websocket"])
async def test_non_http_scopes_pass_straight_through(scope_type):
    seen = []

    async def app(scope, receive, send):
        seen.append(scope["type"])

    middleware = APIKeyMiddleware(app, api_key=KEY)
    await middleware({"type": scope_type}, None, None)

    assert seen == [scope_type], "a non-http scope was inspected for a key"


@pytest.mark.asyncio
async def test_a_disconnect_reaches_the_handler():
    """The whole point of #208.

    `BaseHTTPMiddleware` replaces the receive channel with one that never
    delivers `http.disconnect` to the endpoint, so `request.is_disconnected()`
    always answers False and a handler cannot tell that its caller has gone.
    """
    disconnected = []

    async def app(scope, receive, send):
        request = Request(scope, receive)
        disconnected.append(await request.is_disconnected())
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive():
        return {"type": "http.disconnect"}

    sent = []

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/v1/device/list",
        "headers": [(b"x-api-key", KEY.encode())],
    }
    await APIKeyMiddleware(app, api_key=KEY)(scope, receive, send)

    assert disconnected == [True], (
        "the handler could not see the client had gone, so no endpoint can "
        "stop work it is doing on a caller's behalf"
    )
