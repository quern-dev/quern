"""The conformance fixture's contract, checked without a device.

`tests/conformance/` runs against a live server and is kept out of CI, so a
contract mistake there is found only when someone runs it -- which is how the
Android contract went without `screen_ready` for every screen: a comment said
its pages were all in the tree at once, and `goto` therefore never confirmed
it had arrived. Only the current page is in the tree.
"""

from __future__ import annotations

import pytest

from tests.conformance import probe


@pytest.mark.parametrize("contract", [probe.IOS, probe.ANDROID], ids=lambda c: c.platform)
def test_goto_can_confirm_every_screen(contract):
    """Each screen needs an identifier only its content carries, or `goto`
    returns before the page is there."""
    missing = sorted(set(contract.screens) - set(contract.screen_ready))
    stray = sorted(set(contract.screen_ready) - set(contract.screens))
    assert not missing, f"{contract.platform}: no screen_ready for {missing}"
    assert not stray, f"{contract.platform}: screen_ready for screens it does not have: {stray}"
