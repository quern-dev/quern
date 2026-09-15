"""A thin, honest HTTP client for a *running* Quern server.

Deliberately not a wrapper that hides status codes. The point of this suite is
to assert on what the API actually returns -- including the refusals -- so the
client returns the response and lets the test decide what is correct. Only
transport failures raise.

Why this does not import anything from ``server.``
--------------------------------------------------
The suite under test is the one the user installed and runs, reached over the
wire. Importing ``server.config`` would read ``QUERN_STATE_DIR``, which the root
``tests/conftest.py`` points at a throwaway temp directory before any test
module loads -- so we would read an api-key that no server has ever seen. Every
path here is resolved from the real home directory on purpose.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

#: Where a real install keeps its state, independent of QUERN_STATE_DIR.
REAL_QUERN_DIR = Path.home() / ".quern"

DEFAULT_URL = "http://127.0.0.1:9100"

#: Endpoints documented as public. Used by the auth tests, and by the client to
#: know when omitting the bearer token is not a bug.
PUBLIC_PATHS = frozenset({
    "/",
    "/health",
    "/api/v1/health",
    "/tools",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/video-test",
    "/api/v1/proxy/cert",
})


class ServerUnreachable(RuntimeError):
    """The suite could not reach a Quern server to test."""


@dataclass(frozen=True)
class ServerTarget:
    """Where the suite points, and how it found out."""

    url: str
    api_key: str | None
    #: Human-readable provenance, reported in the environment summary so a
    #: failing run on someone else's machine says which server it hit.
    source: str

    @property
    def has_key(self) -> bool:
        return bool(self.api_key)


def resolve_target() -> ServerTarget:
    """Find the server to test, preferring explicit configuration.

    Order:

    1. ``QUERN_SERVER_URL`` / ``QUERN_API_KEY`` -- so a run can be pointed at a
       server on another host, or at a second server on a different port.
    2. ``~/.quern/state.json``, which a running server writes with its own URL.
    3. The default loopback URL.

    The key is read separately from the URL: pointing at a remote URL while
    still reading the local key is a legitimate setup when both machines were
    provisioned from the same key, and refusing it would be unhelpful.
    """
    env_url = os.environ.get("QUERN_SERVER_URL")
    env_key = os.environ.get("QUERN_API_KEY")

    url, source = None, ""
    if env_url:
        url, source = env_url.rstrip("/"), "QUERN_SERVER_URL"
    else:
        state = _read_json(REAL_QUERN_DIR / "state.json")
        # A stopped server can leave state.json behind; url may be absent or
        # null. Treat either as "not configured" rather than crashing on None.
        candidate = (state or {}).get("url") or _url_from_port(state)
        if candidate:
            url, source = candidate.rstrip("/"), "~/.quern/state.json"
        else:
            url, source = DEFAULT_URL, "default"

    key = env_key
    if key:
        source += " + QUERN_API_KEY"
    else:
        key_path = REAL_QUERN_DIR / "api-key"
        try:
            key = key_path.read_text().strip() or None
        except OSError:
            key = None
        if key:
            source += " + ~/.quern/api-key"

    return ServerTarget(url=url, api_key=key, source=source)


def _url_from_port(state: dict | None) -> str | None:
    """Reconstruct a URL when state.json records only a port."""
    if not state:
        return None
    port = state.get("port")
    if not port:
        return None
    return f"http://127.0.0.1:{port}"


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        # A half-written state.json is a real condition on a live machine;
        # falling back to the default URL is more useful than a crash here,
        # and the health check will produce the clear error if it matters.
        return None


@dataclass
class Call:
    """One request/response pair, kept for the run report."""

    method: str
    path: str
    status: int
    elapsed_ms: float
    #: Populated only on transport failure.
    error: str | None = None


class QuernClient:
    """Synchronous REST client. One instance per session.

    Timeouts are explicit and generous but finite. A hung device tool must
    surface as a test failure with a duration, not as a suite that never ends --
    Quern proxies several tools that can and do wedge.
    """

    #: Endpoints that legitimately take a long time (booting, building,
    #: installing). Everything else gets the default.
    DEFAULT_TIMEOUT = 30.0

    def __init__(self, target: ServerTarget, *, record: list[Call] | None = None):
        self.target = target
        self.calls: list[Call] = record if record is not None else []
        self._client = httpx.Client(
            base_url=target.url,
            timeout=httpx.Timeout(self.DEFAULT_TIMEOUT),
            # Follow the documented `/` -> `/docs` redirect only when a test
            # asks for it, so the redirect itself stays assertable.
            follow_redirects=False,
        )

    # -- core ------------------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        *,
        timeout: float | None = None,
        authenticated: bool = True,
        **kwargs: Any,
    ) -> httpx.Response:
        """Issue a request and record it. Non-2xx is returned, not raised."""
        headers = dict(kwargs.pop("headers", {}) or {})
        if authenticated and self.target.api_key:
            headers.setdefault("Authorization", f"Bearer {self.target.api_key}")

        import time

        started = time.perf_counter()
        try:
            resp = self._client.request(
                method,
                path,
                headers=headers,
                timeout=timeout if timeout is not None else self.DEFAULT_TIMEOUT,
                **kwargs,
            )
        except httpx.HTTPError as exc:
            elapsed = (time.perf_counter() - started) * 1000
            self.calls.append(
                Call(method, path, status=-1, elapsed_ms=elapsed, error=repr(exc))
            )
            raise
        elapsed = (time.perf_counter() - started) * 1000
        self.calls.append(Call(method, path, resp.status_code, elapsed))
        return resp

    def get(self, path: str, **kw: Any) -> httpx.Response:
        return self.request("GET", path, **kw)

    def post(self, path: str, **kw: Any) -> httpx.Response:
        return self.request("POST", path, **kw)

    def put(self, path: str, **kw: Any) -> httpx.Response:
        return self.request("PUT", path, **kw)

    def patch(self, path: str, **kw: Any) -> httpx.Response:
        return self.request("PATCH", path, **kw)

    def delete(self, path: str, **kw: Any) -> httpx.Response:
        return self.request("DELETE", path, **kw)

    # -- conveniences ----------------------------------------------------

    def json_ok(self, method: str, path: str, **kw: Any) -> Any:
        """Request, require 2xx, return the decoded body.

        For the many calls that are setup for the assertion rather than the
        assertion itself. The error message carries the body, because Quern's
        failure responses explain themselves and swallowing that text turns a
        diagnosable failure into "expected 200, got 428".
        """
        resp = self.request(method, path, **kw)
        if not resp.is_success:
            raise AssertionError(
                f"{method} {path} -> {resp.status_code}\n{_body_text(resp)}"
            )
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError as exc:
            raise AssertionError(
                f"{method} {path} -> {resp.status_code} with non-JSON body: "
                f"{_body_text(resp)}"
            ) from exc

    def close(self) -> None:
        self._client.close()


def _body_text(resp: httpx.Response, limit: int = 2000) -> str:
    try:
        text = resp.text
    except Exception:  # noqa: BLE001 - binary or truncated body
        return f"<{len(resp.content)} bytes, undecodable>"
    return text if len(text) <= limit else text[:limit] + "… (truncated)"


@dataclass
class HealthReport:
    """Result of the pre-flight reachability check."""

    reachable: bool
    version: str | None = None
    detail: str = ""
    authenticated: bool = False
    notes: list[str] = field(default_factory=list)


def check_health(client: QuernClient) -> HealthReport:
    """Confirm a server is there and our key works, with precise diagnosis.

    Separating "nothing is listening" from "something is listening but rejects
    the key" matters: the first wants `quern start`, the second wants a key, and
    a single "could not connect" would send anyone down the wrong path.
    """
    try:
        resp = client.get("/health", authenticated=False, timeout=10.0)
    except httpx.HTTPError as exc:
        return HealthReport(
            reachable=False,
            detail=(
                f"no server answered at {client.target.url} ({exc!r}). "
                f"Target resolved from: {client.target.source}. "
                "Start one with `quern start`, or set QUERN_SERVER_URL."
            ),
        )
    if not resp.is_success:
        return HealthReport(
            reachable=False,
            detail=f"/health returned {resp.status_code}: {_body_text(resp)}",
        )

    body = resp.json() if resp.content else {}
    report = HealthReport(reachable=True, version=body.get("version"))

    if not client.target.has_key:
        report.notes.append(
            "no API key found; only the public endpoints can be exercised"
        )
        return report

    # Cheapest authenticated endpoint that is not a device probe: the update
    # channel is read from a file, so this isolates auth from tool health.
    probe = client.get("/api/v1/system/channel", timeout=10.0)
    if probe.status_code in (401, 403):
        report.notes.append(
            f"API key from {client.target.source} was rejected "
            f"({probe.status_code}); authenticated endpoints will be skipped"
        )
    else:
        report.authenticated = True
    return report
