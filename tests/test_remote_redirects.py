"""The pinned opener follows a redirect only within the remote's origin.

urllib follows any redirect, to plain http too, and carries the Authorization
header with it, so a 302 to an http:// address sent the client key in clear
to a host no certificate pin covers (review of the remote status work).
"""

from __future__ import annotations

import urllib.request

import pytest

from server.remote import client

ORIGIN = "https://studio.lan:9130"


def _follow(newurl: str):
    req = urllib.request.Request(ORIGIN + "/api/v1/landmarks",
                                 headers={"Authorization": "Bearer qc_key"})
    return client._SameOriginRedirects().redirect_request(req, None, 307, "redirect", {},
                                                          newurl)


@pytest.mark.parametrize("newurl", [
    "http://studio.lan:9130/api/v1/landmarks/",      # same host, but in clear
    "https://elsewhere.lan:9130/x",                  # another host
    "https://studio.lan:9999/x",                     # another port
    "http://127.0.0.1:8080/health",
])
def test_a_redirect_off_the_origin_is_refused(newurl):
    with pytest.raises(client.RemoteError, match="not followed"):
        _follow(newurl)


@pytest.mark.parametrize("newurl", ["/api/v1/landmarks/", ORIGIN + "/api/v1/landmarks/"])
def test_a_redirect_within_the_origin_is_followed(newurl):
    """FastAPI's trailing-slash redirect, relative or absolute."""
    followed = _follow(newurl)
    assert followed.full_url == ORIGIN + "/api/v1/landmarks/"


def test_the_opener_uses_it():
    opener = client.opener(client.Remote(name="s", url=ORIGIN, fingerprint="SHA256:" + "ab" * 32,
                                         key="k"))
    assert any(isinstance(h, client._SameOriginRedirects) for h in opener.handlers)
