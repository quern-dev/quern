"""Tests for the xcodebuild output parser adapter."""

from __future__ import annotations

from pathlib import Path

import pytest

from server.models import LogLevel, LogSource
from server.sources.build import BuildAdapter


FIXTURES = Path(__file__).parent / "fixtures"


def _collect_entries(adapter: BuildAdapter):
    """Replace the adapter's on_entry with a collector."""
    entries = []

    async def collect(entry):
        entries.append(entry)

    adapter.on_entry = collect
    return entries


@pytest.mark.asyncio
async def test_parse_fixture_output():
    """Parse the xcodebuild fixture and verify diagnostics + tests."""
    adapter = BuildAdapter()
    entries = _collect_entries(adapter)
    await adapter.start()

    content = (FIXTURES / "xcodebuild_output.txt").read_text()
    result = await adapter.parse_build_output(content)

    # Build should have failed
    assert result.succeeded is False

    # 2 errors
    assert len(result.errors) == 2
    assert result.errors[0].file.endswith("ViewController.swift")
    assert result.errors[0].line == 42
    assert result.errors[0].severity == "error"
    assert "undeclared identifier" in result.errors[0].message

    assert result.errors[1].file.endswith("NetworkManager.swift")
    assert result.errors[1].line == 123

    # 3 warnings
    assert len(result.warnings) == 3
    assert result.warnings[0].line == 85
    assert result.warnings[1].file.endswith("Utils.swift")

    # Test results
    assert result.tests is not None
    assert result.tests.total == 5
    assert result.tests.passed == 3
    assert result.tests.failed == 2
    assert len(result.tests.failures) == 2
    assert result.tests.failures[0].method == "testFetchData"
    assert result.tests.failures[1].method == "testRefreshToken"

    # Log entries emitted: 2 errors + 3 warnings = 5
    assert len(entries) == 5
    error_entries = [e for e in entries if e.level == LogLevel.ERROR]
    warn_entries = [e for e in entries if e.level == LogLevel.WARNING]
    assert len(error_entries) == 2
    assert len(warn_entries) == 3

    for e in entries:
        assert e.source == LogSource.BUILD
        assert e.process == "xcodebuild"

    # latest_result stored
    assert adapter.latest_result is result

    await adapter.stop()


@pytest.mark.asyncio
async def test_parse_success_build():
    """A build with no errors and BUILD SUCCEEDED."""
    adapter = BuildAdapter()
    _collect_entries(adapter)
    await adapter.start()

    output = """\
Compiling Swift source files
Linking MyApp
** BUILD SUCCEEDED **
"""
    result = await adapter.parse_build_output(output)
    assert result.succeeded is True
    assert len(result.errors) == 0
    assert len(result.warnings) == 0
    assert result.tests is None

    await adapter.stop()


@pytest.mark.asyncio
async def test_parse_empty_output():
    adapter = BuildAdapter()
    _collect_entries(adapter)
    await adapter.start()

    result = await adapter.parse_build_output("")
    assert result.succeeded is True
    assert len(result.errors) == 0
    assert result.tests is None

    await adapter.stop()


@pytest.mark.asyncio
async def test_status_ready():
    adapter = BuildAdapter()
    await adapter.start()
    status = adapter.status()
    assert status.status == "ready"
    assert status.type == "xcodebuild"
    await adapter.stop()
