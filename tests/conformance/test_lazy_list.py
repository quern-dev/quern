"""Scrolling a lazy list: the iOS probe's Lazy tab (#475).

A UICollectionView list builds cells only as they scroll into view, so every
read holds the ~15 rows on screen and none of the other 185. That is the shape
#475 was about: through WDA, `scroll_to_element` judged the list by what one
read showed, found "nothing scrollable" after two swipes, and gave up on a row
the list held all along.

The Scroll tab cannot catch that, because a UITableView of static rows is in
the tree whole. These tests run each case twice on iOS: on the backend the
device already uses, and through WDA. On a simulator the WDA case starts the
driver for itself and stops it afterwards. On a physical iPhone, WDA is the
backend already, so both cases are the same path.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from .client import QuernClient

ROWS = 200


@pytest.fixture(params=["default", "wda"], scope="module")
def lazy(request: pytest.FixtureRequest, quern: QuernClient) -> Iterator:
    probe = request.getfixturevalue("ios_probe")
    started = False
    if request.param == "wda" and not getattr(probe, "physical", False):
        quern.json_ok(
            "POST", "/api/v1/device/wda/start", json={"udid": probe.udid}, timeout=300.0,
        )
        started = True
    try:
        probe.goto("lazy")
        yield probe
    finally:
        if started:
            # Back to sim-bridge, so the rest of the session reads the way it
            # would have without this module.
            quern.post("/api/v1/device/wda/stop", json={"udid": probe.udid}, timeout=120.0)


def _row(index: int) -> str:
    return f"lazy_row_{index}"


def _top(probe) -> None:
    """Back to row 0, by the same sweep, so each case starts from the top."""
    probe.scroll_to(identifier=_row(0), max_swipes=40)


#: The depth rows are read at. Through WDA, a list behind More has its rows
#: from depth 14 (measured on an iOS 26.5 simulator; none at 12, the depth
#: `ProbeDriver.element` reads at), so reading at the driver's depth makes
#: every row look absent -- and would pass the laziness check below for the
#: wrong reason. A lazy list holds ~15 rows, so 25 costs little. Ignored off WDA.
ROW_DEPTH = 25


def _read(probe, quern: QuernClient, index: int) -> tuple[int, dict | None]:
    """(status, element) for one row, read at `ROW_DEPTH`."""
    resp = quern.get(
        "/api/v1/device/ui/element",
        params={"udid": probe.udid, "identifier": _row(index),
                "source_timeout": 30, "snapshot_depth": ROW_DEPTH},
        timeout=90.0,
    )
    if resp.status_code not in (200, 404):
        raise AssertionError(f"reading {_row(index)}: {resp.status_code}: {resp.text[:300]}")
    return resp.status_code, (resp.json().get("element") if resp.status_code == 200 else None)


def _visible(probe, quern: QuernClient, index: int) -> bool:
    """In the tree and not marked off screen (WDA's `on_screen: false`)."""
    status, el = _read(probe, quern, index)
    return status == 200 and bool(el) and el.get("on_screen") is not False


def test_rows_beyond_the_first_screen_are_not_in_the_tree(lazy, quern: QuernClient) -> None:
    """Without this, the tests below could pass on a list that is not lazy.

    Absent means a 404, not `on_screen: false`: through WDA a table reports
    every row, off-screen ones marked, so "not visible" would call a whole
    table lazy. Row 0 is checked first, at the same depth, so a read too
    shallow to see any row fails here instead of passing.
    """
    _top(lazy)
    assert _visible(lazy, quern, 0), "row 0 is not on screen at the top"
    status, _ = _read(lazy, quern, 150)
    assert status == 404, (
        f"lazy_row_150 is in the tree at the top ({status}): the list is not "
        "lazy, so these tests no longer exercise #475"
    )


def test_a_row_far_down_a_lazy_list_is_reached(lazy, quern: QuernClient) -> None:
    """The #475 regression: before the fix, 'nothing scrollable' after 2 swipes."""
    _top(lazy)
    lazy.scroll_to(identifier=_row(150))
    assert _visible(lazy, quern, 150), (
        "scroll_to_element answered ok, but lazy_row_150 is not on screen"
    )


def test_a_row_above_is_reached_from_further_down(lazy, quern: QuernClient) -> None:
    _top(lazy)
    lazy.scroll_to(identifier=_row(150))
    lazy.scroll_to(identifier=_row(3))
    assert _visible(lazy, quern, 3), "lazy_row_3 is not on screen after scrolling back up"


def test_a_row_the_list_does_not_have_is_not_found(lazy, quern: QuernClient) -> None:
    resp = quern.post(
        "/api/v1/device/ui/scroll-to-element",
        json={"udid": lazy.udid, "identifier": _row(ROWS + 50), "max_swipes": 40},
        timeout=300.0,
    )
    assert resp.status_code == 404, f"{resp.status_code}: {resp.text[:300]}"
