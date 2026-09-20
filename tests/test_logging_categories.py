"""Categories on quern's own log entries.

`LogEntry.category` has existed and been filterable since before this, and the
server never set it -- every server-side entry arrived with `category=""`, so
"show me device actions" was not a question anyone could ask. See
docs/proposals/logging-spec.md.

These pin the path end to end: a call through `server.logging_ext` must arrive
at the ring buffer with its category intact, and a plain `logger.info` must
still work and still arrive uncategorised.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from server import logging_ext
from server.models import LogLevel
from server.sources.server_log import ServerLogAdapter, _map_level


class TestTheCategoryReachesTheBuffer:
    async def _entries_for(self, emit) -> list:
        """Run `emit` with the adapter installed, return what it captured."""
        captured: list = []

        async def on_entry(entry):
            captured.append(entry)

        adapter = ServerLogAdapter(on_entry=on_entry)
        await adapter.start()
        try:
            emit()
            # The handler hops through call_soon_threadsafe, so the entry is
            # not in the list until the loop has turned.
            for _ in range(10):
                await asyncio.sleep(0)
                if captured:
                    break
            await asyncio.sleep(0.05)
        finally:
            await adapter.stop()
        return captured

    async def test_a_categorised_call_arrives_categorised(self):
        logger = logging.getLogger("quern-test.categories")
        logger.setLevel(logging.DEBUG)

        entries = await self._entries_for(
            lambda: logging_ext.info(
                logger, "restored input on %s", "ABCD1234",
                category="device.lifecycle", udid="ABCD1234-FULL",
            )
        )

        assert entries, "nothing reached the buffer"
        entry = entries[-1]
        assert entry.category == "device.lifecycle", (
            f"category was {entry.category!r}; the filter this exists for "
            "cannot work without it"
        )
        assert "ABCD1234" in entry.message

    async def test_a_plain_logger_call_still_works(self):
        """Most of the tree is not converted, and must keep logging."""
        logger = logging.getLogger("quern-test.plain")
        logger.setLevel(logging.DEBUG)

        entries = await self._entries_for(lambda: logger.info("no category here"))

        assert entries, "an unconverted logger call stopped reaching the buffer"
        assert entries[-1].category == ""

    async def test_the_level_policy_survives_the_trip(self):
        """A WARNING is how "did what you asked, result is suspect" travels."""
        logger = logging.getLogger("quern-test.levels")
        logger.setLevel(logging.DEBUG)

        entries = await self._entries_for(
            lambda: logging_ext.warning(
                logger, "Device Hub holds the input services", category="device.lifecycle",
            )
        )

        assert entries[-1].level == LogLevel.WARNING


class TestTheVocabularyIsClosed:
    def test_an_unknown_category_is_reported_not_silently_kept(self, caplog):
        """A typo'd category is unfilterable and invisible. Say so."""
        logger = logging.getLogger("quern-test.unknown")
        logger.setLevel(logging.DEBUG)

        with caplog.at_level(logging.WARNING, logger="quern-test.unknown"):
            logging_ext.info(logger, "x", category="devcie.action")

        assert any("Unknown log category" in r.message for r in caplog.records)

    def test_an_unknown_category_still_logs_the_entry(self):
        """Raising would turn a logging mistake into an outage, on paths that
        are often already reporting a failure."""
        logger = logging.getLogger("quern-test.unknown2")
        logger.setLevel(logging.DEBUG)
        logging_ext.info(logger, "the message still matters", category="nope")

    @pytest.mark.parametrize("category", logging_ext.CATEGORIES)
    def test_every_published_category_is_accepted(self, category, caplog):
        logger = logging.getLogger("quern-test.each")
        logger.setLevel(logging.DEBUG)

        with caplog.at_level(logging.WARNING, logger="quern-test.each"):
            logging_ext.info(logger, "x", category=category)

        assert not any("Unknown log category" in r.message for r in caplog.records)

    def test_the_categories_are_the_ones_the_spec_publishes(self):
        """The list is the contract. Changing it is a review conversation,
        which a test failure is the prompt for."""
        assert set(logging_ext.CATEGORIES) == {
            "device.action", "device.read", "device.lifecycle",
            "proxy", "logs", "media", "build", "knowledge", "server.lifecycle",
        }


class TestNoticeIsUnreachableFromPython:
    """The enum has a NOTICE and Python logging cannot produce one. Pinned so
    that someone adding a level 25 has to notice this test and the docstring
    that explains why it was left alone."""

    @pytest.mark.parametrize(
        "levelno",
        [logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR, logging.CRITICAL],
    )
    def test_no_python_level_maps_to_notice(self, levelno):
        assert _map_level(levelno) != LogLevel.NOTICE


class TestTheQueryPathCanActuallyFilterOnIt:
    """Populating `category` is useless if the query endpoint ignores it.

    It did. `/logs/query` had no `category` parameter, so FastAPI dropped the
    unknown query string silently and returned *everything* -- a filter that
    looks like it works and does not is worse than one that is absent. Caught
    live, not by a test, which is why these exist.
    """

    @staticmethod
    def _entry(category: str, message: str):
        import uuid
        from datetime import UTC, datetime

        from server.models import LogEntry, LogSource

        return LogEntry(
            id=uuid.uuid4().hex,
            timestamp=datetime.now(UTC),
            device_id="server",
            process="quern-debug-server.test",
            category=category,
            level=LogLevel.INFO,
            message=message,
            source=LogSource.SERVER,
        )

    async def _buffer(self):
        from server.storage.ring_buffer import RingBuffer

        buf = RingBuffer(max_size=100)
        await buf.append(self._entry("device.lifecycle", "restored input"))
        await buf.append(self._entry("device.action", "tapped something"))
        await buf.append(self._entry("", "an uncategorised legacy line"))
        return buf

    async def test_a_category_selects_only_its_own_entries(self):
        from server.models import LogQueryParams

        buf = await self._buffer()
        got = await buf.filter_entries(LogQueryParams(category="device.lifecycle"))

        assert [e.message for e in got] == ["restored input"], (
            "the category filter let other categories through"
        )

    async def test_a_category_with_no_entries_returns_none(self):
        """The half that proves the filter runs at all: before the fix every
        category returned the whole buffer."""
        from server.models import LogQueryParams

        buf = await self._buffer()
        got = await buf.filter_entries(LogQueryParams(category="build"))

        assert got == []

    async def test_no_category_still_returns_everything(self):
        from server.models import LogQueryParams

        buf = await self._buffer()
        got = await buf.filter_entries(LogQueryParams())

        assert len(got) == 3

    def test_the_query_endpoint_accepts_a_category_parameter(self):
        """FastAPI ignores unknown query params, so a missing parameter is a
        silent no-op rather than a 422. Assert the signature directly."""
        import inspect

        from server.api.logs import query_logs

        assert "category" in inspect.signature(query_logs).parameters
