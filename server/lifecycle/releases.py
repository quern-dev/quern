"""Where release metadata and assets come from (#219).

Everything that fetches a release -- the updater, setup's menu-bar app fetch,
`quern menubar install`, the install script -- talks to one GitHub API base.
That is right in production and makes a release candidate untestable: until it
is published there is nothing to point at, so the paths that actually bite
(updating *from* the previous release, a first install) could only ever be
exercised after the release was already out. That is how 0.18.3 shipped an
update that crashed for everyone (#212).

`QUERN_RELEASES_URL` moves that base, so a rehearsal can serve a candidate
locally. Nothing sets it in normal use, and it is deliberately one variable
rather than one per caller: two sources of truth is how a check ends up
verifying something other than what runs.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

GITHUB_REPO = "quern-dev/quern"
DEFAULT_API = f"https://api.github.com/repos/{GITHUB_REPO}"

#: Overrides the API base. A rehearsal points this at a local server.
ENV_VAR = "QUERN_RELEASES_URL"


def api_base(env: dict[str, str] | None = None) -> str:
    """The release API base: GitHub's, or whatever `QUERN_RELEASES_URL` says."""
    env = os.environ if env is None else env
    return (env.get(ENV_VAR) or "").strip().rstrip("/") or DEFAULT_API


def is_overridden(env: dict[str, str] | None = None) -> bool:
    return api_base(env) != DEFAULT_API


def asset_url_is_trusted(url: str, env: dict[str, str] | None = None) -> bool:
    """Whether an asset URL from a release response may be followed.

    Release assets are served from github.com; anything else means the response
    is not what we think it is, and following it would fetch code from
    somewhere nobody chose. The exception is an explicitly overridden base: the
    operator pointed us at that server on purpose, and its assets live there.
    Even then the download is verified before it is installed -- the signature
    check is what makes the app safe to run, not the hostname.
    """
    if is_overridden(env):
        base = urlparse(api_base(env))
        here = urlparse(url)
        return (here.scheme, here.netloc) == (base.scheme, base.netloc)
    return url.startswith("https://github.com/")


def download_url(version: str, env: dict[str, str] | None = None) -> str:
    """Where `quern-<version>.tar.gz` lives, for callers that build the URL
    themselves rather than reading it out of a release response."""
    asset = f"quern-{version}.tar.gz"
    if is_overridden(env):
        return f"{api_base(env)}/releases/download/v{version}/{asset}"
    return f"https://github.com/{GITHUB_REPO}/releases/download/v{version}/{asset}"
