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
from urllib.parse import unquote, urlparse

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


def allowed_prefixes(env: dict[str, str] | None = None) -> tuple[str, ...]:
    """Every URL prefix a release response may point us at.

    Pinned to *this* repository, not merely to github.com: a host check alone
    accepts `https://github.com/someone-else/evil/releases/download/...`, and a
    free account is enough to host a payload on a genuinely trusted host. What
    is downloaded is extracted over the install, so the repository is the part
    that matters.

    Both shapes are here because a release can legitimately be either:
    `releases/download/` is an uploaded asset, and `.../tarball/` is GitHub's
    generated source tarball, which is what a release with no asset resolves to
    -- v0.14.1 and every release cut before the asset existed. Refusing that
    form would have refused their updates outright, with no fallback.
    """
    base = api_base(env)
    if is_overridden(env):
        # One server the operator named. Everything it serves is under its own
        # base; a response from it naming somewhere else is still wrong.
        return (base + "/",)
    return (
        f"https://github.com/{GITHUB_REPO}/releases/download/",
        f"https://github.com/{GITHUB_REPO}/archive/",
        f"{DEFAULT_API}/tarball/",
    )


def asset_url_is_trusted(url: str, env: dict[str, str] | None = None) -> bool:
    """Whether a URL from a release response may be followed.

    The scheme and host are compared case-insensitively, since they are
    case-insensitive in fact; the path is not.

    This is a first-hop check. GitHub's asset URLs redirect to a storage host,
    and urllib follows that redirect, so it establishes *which release* we
    asked for rather than which bytes come back -- see #227 for signing them.
    """
    origin = _origin(url)
    if origin is None:
        return False
    parsed = urlparse(url)
    # A prefix compare reads the path left to right, and `..` is how a path
    # that starts inside the allowlist ends up outside it. api.github.com
    # resolves dot segments server-side and urllib sends the path unchanged,
    # so `.../quern-dev/quern/tarball/../../../someone/else/tarball/master`
    # passed this check and fetched another repository's tree -- which is the
    # exact substitution the repo pin exists to prevent. Refused rather than
    # normalised: a release naming a URL that has to be resolved to be
    # understood is not one to follow.
    if _has_dot_segment(parsed.path):
        return False
    for prefix in allowed_prefixes(env):
        head = _origin(prefix)
        if head is None:
            continue
        # The raw path, not the decoded one: `%2F` reads as a separator here
        # and as one character to the server, and the two readings must not be
        # allowed to disagree about which repository is being named.
        if f"{origin}{parsed.path}".startswith(f"{head}{urlparse(prefix).path}"):
            return True
    return False


def _has_dot_segment(path: str) -> bool:
    """Whether any segment is `.` or `..`, percent-encoding included."""
    return any(seg in (".", "..") for seg in unquote(path).split("/"))


def _origin(url: str) -> str | None:
    """`scheme://host[:port]`, lowercased, or None if it cannot be trusted to
    mean what it reads as.

    Userinfo is refused outright: in `https://github.com@evil.example/x` the
    host a person reads is not the host urllib connects to. A default port is
    dropped so that `https://github.com:443/...` -- which no one emits, but
    which is the same origin -- is not a false refusal. The last false refusal
    in this check would have blocked every v0.14.1 user's update.
    """
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme not in ("https", "http"):
        return None
    if parsed.username or parsed.password:
        return None
    try:
        port = parsed.port
    except ValueError:      # a non-numeric port; urlparse raises on access
        return None
    host = (parsed.hostname or "").lower()
    if not host:
        return None
    if port is None or port == (443 if scheme == "https" else 80):
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


def download_url(version: str, env: dict[str, str] | None = None) -> str:
    """Where `quern-<version>.tar.gz` lives, for callers that build the URL
    themselves rather than reading it out of a release response."""
    asset = f"quern-{version}.tar.gz"
    if is_overridden(env):
        return f"{api_base(env)}/releases/download/v{version}/{asset}"
    return f"https://github.com/{GITHUB_REPO}/releases/download/v{version}/{asset}"
