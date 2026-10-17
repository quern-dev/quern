"""The conformance client's target discovery, without a server (#446).

The suite itself runs against a live quern and is deselected by default, so
this is the only test that runs its URL logic in a normal run.
"""

from __future__ import annotations

from tests.conformance.client import DEFAULT_URL, _url_from_port


def test_the_url_comes_from_the_port_the_server_writes():
    """`server_port` is the key state.json carries; reading `port`, which is
    never written, always fell back to the default URL (CodeRabbit on #467)."""
    assert _url_from_port({"server_port": 9123}) == "http://127.0.0.1:9123"


def test_an_old_port_key_still_works_and_nothing_is_nothing():
    assert _url_from_port({"port": 9124}) == "http://127.0.0.1:9124"
    assert _url_from_port({}) is None and _url_from_port(None) is None


def test_the_fallback_is_the_new_default():
    from server.lifecycle.ports import DEFAULT_SERVER_PORT

    assert DEFAULT_URL == f"http://127.0.0.1:{DEFAULT_SERVER_PORT}"
