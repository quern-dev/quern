"""A datetime query parameter without an offset must not 500 (#267).

`?since=2026-09-21T12:00:00` is valid ISO 8601 and FastAPI parses it into a
*naive* datetime. Everything it is compared against is UTC-aware, so the
comparison raised `TypeError: can't compare offset-naive and offset-aware
datetimes` and a well-formed request came back as HTTP 500.

**These tests pin a non-UTC timezone, and that is the whole point.** The
correct fix is `replace(tzinfo=UTC)` -- read the wall clock the caller sent as
UTC. The plausible wrong one is `astimezone(UTC)` -- read it as the *server's*
local time and convert, which silently shifts the window and returns the wrong
rows instead of raising. Under `TZ=UTC` the two are identical, and CI runs in
UTC, so a test that does not pin a timezone passes against either.

The offset's *direction* matters too. With a negative offset (Los Angeles),
`astimezone` produces a **later** `since`, which wrongly excludes entries that
should match -- so asserting an entry comes back distinguishes them. With a
positive offset (Tokyo) the wrong implementation produces an earlier `since`
and the entry is still returned, so the same assertion would pass against the
bug. They are not interchangeable.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from server.models import LogQueryParams, _as_utc


@pytest.fixture
def app_for_introspection():
    """A real app with every datetime-comparing store holding one record.

    The stores matter: with an empty one nothing is compared, the endpoint
    returns before it can raise, and the test is green against the bug. That
    is exactly how #267 shipped, and how two of these very tests were inert.
    """
    import asyncio
    from types import SimpleNamespace

    from server.config import ServerConfig
    from server.main import create_app
    from server.models import LogEntry

    config = ServerConfig(api_key="test-key-12345")
    app = create_app(
        config=config, enable_oslog=False, enable_crash=False, enable_proxy=False,
    )
    app.state.proxy_adapter = None

    at = datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC)

    async def _seed():
        await app.state.ring_buffer.append(
            LogEntry(
                id="e1", timestamp=at, source="simulator",
                level="error", message="boom",
            ),
        )

    asyncio.run(_seed())

    # A crash adapter holding one report, so `/crashes/latest` reaches the
    # `r.timestamp >= since` comparison at crashes.py:53 instead of returning
    # early on `crash_adapter is None`. Matching the real attribute name
    # (`crash_reports`) is the point: a stub shaped wrongly makes the test
    # fail loudly, which is better than one shaped conveniently making it
    # pass without ever reaching the code under test.
    from server.models import CrashReport

    report = CrashReport(crash_id="c1", timestamp=at, process="MyApp")
    app.state.crash_adapter = SimpleNamespace(crash_reports=[report])

    # A real FlowStore holding one flow, so `/proxy/flows` and `/trace`
    # compare rather than short-circuiting, and so `size`/`max_size` exist.
    from server.models import FlowRecord, FlowRequest
    from server.proxy.flow_store import FlowStore

    store = FlowStore(max_size=10)

    async def _seed_flow():
        await store.add(
            FlowRecord(
                id="f1", timestamp=at,
                request=FlowRequest(
                    method="GET", url="https://example.com/",
                    host="example.com", path="/",
                ),
            ),
        )

    asyncio.run(_seed_flow())
    app.state.flow_store = store
    return app


class TestReadingANaiveDatetime:
    def test_a_naive_value_is_read_as_utc_not_as_local(self, pinned_timezone):
        """The distinguishing case. In Los Angeles the two candidate fixes
        disagree by seven or eight hours."""
        pinned_timezone("America/Los_Angeles")

        got = _as_utc(datetime(2026, 9, 21, 12, 0, 0))

        assert got == datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC), (
            "a naive value was converted from local time rather than read as UTC"
        )
        assert got.hour == 12, "the wall clock the caller sent was shifted"

    def test_the_same_holds_with_a_positive_offset(self, pinned_timezone):
        """Tokyo, so the test does not depend on the sign of one zone's
        offset. `astimezone` would give 03:00Z here and 19:00Z above; only
        `replace` gives 12:00Z in both."""
        pinned_timezone("Asia/Tokyo")

        got = _as_utc(datetime(2026, 9, 21, 12, 0, 0))

        assert got == datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC)

    def test_an_aware_value_is_left_alone(self, pinned_timezone):
        """The control. A caller that sent an offset meant it."""
        pinned_timezone("America/Los_Angeles")
        aware = datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC)

        assert _as_utc(aware) is aware

    def test_an_offset_that_is_not_utc_survives(self, pinned_timezone):
        """+05:30 is not UTC and not the server's zone; it must not be
        rewritten to either."""
        pinned_timezone("America/Los_Angeles")
        from datetime import timezone

        kolkata = timezone(timedelta(hours=5, minutes=30))
        aware = datetime(2026, 9, 21, 12, 0, 0, tzinfo=kolkata)

        assert _as_utc(aware).utcoffset() == timedelta(hours=5, minutes=30)

    def test_none_stays_none(self):
        assert _as_utc(None) is None


class TestTheModelsCoerce:
    """The type is what makes this survive the eighth endpoint. Per-handler
    `if x.tzinfo is None` is how six endpoints stayed broken while three were
    fixed one at a time."""

    def test_log_query_params_coerces_a_naive_since(self, pinned_timezone):
        pinned_timezone("America/Los_Angeles")

        params = LogQueryParams(since="2026-09-21T12:00:00")

        assert params.since.tzinfo is not None, "a naive since reached the model"
        assert params.since == datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC)

    def test_log_query_params_coerces_a_naive_until(self, pinned_timezone):
        pinned_timezone("America/Los_Angeles")

        params = LogQueryParams(until="2026-09-21T12:00:00")

        assert params.until == datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC)

    def test_an_offset_is_preserved_through_the_model(self, pinned_timezone):
        pinned_timezone("America/Los_Angeles")

        params = LogQueryParams(since="2026-09-21T12:00:00+05:30")

        assert params.since.utcoffset() == timedelta(hours=5, minutes=30)


@pytest.mark.asyncio
class TestTheEndpointsDoNotFiveHundred:
    """The bug only fires once there is data — with an empty buffer nothing is
    compared and the call succeeds. That is why it went unnoticed, so these
    put an entry in first."""

    async def _ring_with_one_entry(self):
        from server.models import LogEntry
        from server.storage.ring_buffer import RingBuffer

        ring = RingBuffer(max_size=10)
        await ring.append(
            LogEntry(
                id="e1",
                timestamp=datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC),
                source="simulator",
                level="info",
                message="hello",
            ),
        )
        return ring

    async def test_a_naive_since_returns_the_entry_rather_than_raising(
        self, pinned_timezone,
    ):
        """End to end through the query path. Under `astimezone` the window
        would start at 18:00Z in this timezone (PDT, UTC-7) and the 12:00Z entry would be
        missing — a silently wrong answer rather than an error."""
        pinned_timezone("America/Los_Angeles")
        ring = await self._ring_with_one_entry()

        params = LogQueryParams(since="2026-09-21T11:00:00")
        got, total = await ring.query(params)

        assert total == 1, (
            "the entry was filtered out — the naive since was read as local time"
        )

    async def test_a_naive_until_still_excludes_what_it_should(
        self, pinned_timezone,
    ):
        """The control for the test above: coercion must not make the filter
        match everything."""
        pinned_timezone("America/Los_Angeles")
        ring = await self._ring_with_one_entry()

        params = LogQueryParams(until="2026-09-21T11:00:00")
        got, total = await ring.query(params)

        assert total == 0, "until stopped excluding anything"


class TestOverHttp:
    """The issue as a caller meets it: a well-formed request returning 500.

    These go through the real app, because that is where `UtcDatetime` does
    its work — FastAPI validates the query parameter and the coercion happens
    there. **A test that calls a handler function directly bypasses all of
    it** and proves nothing about this fix, which is worth knowing before
    writing the next one.

    An entry goes into the buffer first. With an empty buffer nothing is
    compared, the call succeeds either way, and the test is green against the
    bug — which is exactly why this shipped unnoticed.
    """

    @pytest.fixture
    def app_with_an_entry(self, app_for_introspection):
        """An app whose every datetime-comparing store holds one record.

        **Every store, not just the log buffer.** Two of these cases used to
        be inert: `/crashes/latest` returned early because
        `crash_adapter is None`, so it never reached the comparison it was
        meant to exercise -- an HTTP test that could not fail, naming one of
        the endpoints the commit claimed to fix.

        `asyncio.run` rather than a hand-made loop: the append and the handler
        would otherwise run on different loops while sharing
        `RingBuffer._lock`, which works only because `asyncio.Lock` binds its
        loop on the *contended* path. Any contention turns that into a
        RuntimeError surfacing as a 500 -- which would read as this fix
        breaking.
        """
        return app_for_introspection

    # `/logs/summary` is deliberately absent: it takes `since_cursor: str`,
    # never a datetime, so `?since=` on it is an ignored query string and the
    # case returned 200 whatever the code did. It was in this list and could
    # not fail -- the shape this file exists to catch, inside the file itself.
    @pytest.mark.parametrize(
        "path",
        [
            "/api/v1/logs/query?since=2026-09-21T11:00:00",
            "/api/v1/logs/query?until=2026-09-21T13:00:00",
            "/api/v1/logs/errors?since=2026-09-21T11:00:00",
            "/api/v1/proxy/flows?since=2026-09-21T11:00:00",
            "/api/v1/proxy/flows?until=2026-09-21T13:00:00",
            "/api/v1/crashes/latest?since=2026-09-21T11:00:00",
            "/api/v1/trace?since=2026-09-21T11:00:00",
        ],
    )
    def test_a_naive_offset_does_not_five_hundred(
        self, app_with_an_entry, pinned_timezone, path,
    ):
        from fastapi.testclient import TestClient

        pinned_timezone("America/Los_Angeles")
        client = TestClient(app_with_an_entry)

        resp = client.get(path, headers={"Authorization": "Bearer test-key-12345"})

        assert resp.status_code != 500, (
            f"{path} returned 500 for a well-formed ISO 8601 timestamp: "
            f"{resp.text[:200]}"
        )
        assert resp.status_code < 400, f"{path} -> {resp.status_code}"

    def test_an_offset_is_still_accepted(self, app_with_an_entry):
        """The control: coercing naive values must not break the normal case."""
        from fastapi.testclient import TestClient

        client = TestClient(app_with_an_entry)
        resp = client.get(
            "/api/v1/logs/query?since=2026-09-21T11:00:00%2B00:00",
            headers={"Authorization": "Bearer test-key-12345"},
        )

        assert resp.status_code < 400, resp.text[:200]


class TestEveryDatetimeInputCarriesTheCoercion:
    """The check that survives the next endpoint.

    Five HTTP tests defend two endpoints. A review reverted `UtcDatetime` to
    `datetime` at four of the six changed sites and the **entire suite stayed
    green** while five inputs went back to returning 500 -- so per-endpoint
    tests are not what makes this stick. This walks the app instead and fails
    on any datetime input that is not annotated, including one added tomorrow.

    Two traps, both hit while writing it, both worth keeping:

    1. FastAPI keeps included routers as lazy `_IncludedRouter` wrappers, so a
       plain walk of `app.routes` sees **8 endpoints rather than 132** and
       reports clean on an app it never read. Recurse through
       `original_router`.
    2. That failure is invisible -- a clean answer from an empty walk looks
       exactly like a clean answer from a full one. Hence the count assertion
       below: the check has to prove it looked before its silence means
       anything.
    """

    @staticmethod
    def _endpoints(app):
        from fastapi.routing import APIRoute

        found, seen = [], set()

        def walk(router, depth=0):
            if depth > 10 or id(router) in seen:
                return
            seen.add(id(router))
            for route in getattr(router, "routes", []):
                if isinstance(route, APIRoute):
                    found.append(route)
                # `or` is wrong here and was: an `APIRouter` can be falsy,
                # and `_IncludedRouter.app` is a *string*, so the fallback
                # silently produced something with no `.routes` and the walk
                # found nothing. That is the can't-fail shape again, inside
                # the check written to catch it -- which is what the count
                # assertion in the sibling test exists to surface.
                for attr in ("original_router", "router", "app"):
                    inner = getattr(route, attr, None)
                    if inner is not None and hasattr(inner, "routes"):
                        walk(inner, depth + 1)

        walk(app)
        return found

    @staticmethod
    def _carries_coercion(annotation) -> bool:
        """True if `_as_utc` is somewhere in this annotation's metadata."""
        import typing

        from server.models import _as_utc

        stack, seen = [annotation], set()
        while stack:
            item = stack.pop()
            if id(item) in seen:
                continue
            seen.add(id(item))
            for meta in getattr(item, "__metadata__", ()):
                if getattr(meta, "func", None) is _as_utc:
                    return True
            stack.extend(a for a in typing.get_args(item) if a is not None)
        return False

    @staticmethod
    def _mentions_datetime(annotation) -> bool:
        import typing
        from datetime import datetime

        stack, seen = [annotation], set()
        while stack:
            item = stack.pop()
            if id(item) in seen:
                continue
            seen.add(id(item))
            if item is datetime:
                return True
            stack.extend(a for a in typing.get_args(item) if a is not None)
            stack.extend(getattr(item, "__metadata__", ()))
        return False

    def test_the_walk_actually_reaches_the_routes(self, app_for_introspection):
        """The control. Without this, a walk that found nothing would report
        every datetime input as correctly annotated."""
        routes = self._endpoints(app_for_introspection)
        documented = len(app_for_introspection.openapi()["paths"])

        assert len(routes) >= documented, (
            f"walked {len(routes)} routes but OpenAPI documents {documented} "
            "paths — the walk is not seeing the included routers, so its "
            "silence means nothing"
        )

    def test_no_datetime_parameter_is_left_naive(self, app_for_introspection):
        """`get_type_hints`, not `inspect.signature`.

        Every module here has `from __future__ import annotations`, so
        signature annotations are **strings** -- `"datetime | None"`, not the
        type. Comparing those against the `datetime` class matches nothing, so
        the first version of this test reported zero offenders on an app with
        four of them. A check that cannot produce its negative, inside the
        check written to catch exactly that. `include_extras=True` is required
        or `Annotated` metadata is stripped and every parameter looks naive.
        """
        import typing

        offenders = []
        for route in self._endpoints(app_for_introspection):
            try:
                hints = typing.get_type_hints(route.endpoint, include_extras=True)
            except Exception:  # unresolvable forward ref — not this test's job
                continue
            for name, ann in hints.items():
                if name == "return":
                    continue
                if self._mentions_datetime(ann) and not self._carries_coercion(ann):
                    offenders.append(f"{route.path} :: {name}")

        assert not offenders, (
            "datetime parameters without UtcDatetime — a naive ISO 8601 value "
            f"will 500 these: {offenders}"
        )

    def test_no_request_model_field_is_left_naive(self):
        """Body models, which the route walk does not cover. This is what
        caught `WaitForFlowRequest.since`."""
        import pydantic

        import server.models as models

        offenders = []
        for name in dir(models):
            obj = getattr(models, name)
            if not (isinstance(obj, type) and issubclass(obj, pydantic.BaseModel)):
                continue
            for field_name, field in obj.model_fields.items():
                ann = field.annotation
                if not self._mentions_datetime(ann):
                    continue
                # Response/record models hold datetimes quern produced, which
                # are already aware. Only *inputs* need coercing, and those
                # are the ones a caller can send naive.
                if not field_name.startswith(("since", "until")):
                    continue
                if not self._carries_coercion(ann):
                    offenders.append(f"{name}.{field_name}")

        assert not offenders, (
            f"since/until fields without UtcDatetime: {offenders}"
        )
