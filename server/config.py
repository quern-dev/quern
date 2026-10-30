"""Server configuration and API key management."""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import secrets
import shlex
import shutil
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from server.lifecycle.ports import DEFAULT_SERVER_PORT

logger = logging.getLogger(__name__)

# Honours QUERN_STATE_DIR, so redirecting it redirects *everything* under
# ~/.quern -- the api key, config.json, the device pool, crash reports, the
# tool snapshot, the installed-by-setup manifest, all of it.
#
# It used to redirect two files. `server/lifecycle/state.py` read the variable
# itself and applied it to state.json and active-device.json only, while every
# other path was built from `Path.home()` here. The test suite's own comment
# said "QUERN_STATE_DIR redirects ~/.quern", which was the sandbox everyone
# believed in: a test calling `regenerate_api_key()` would have rewritten the
# developer's real key and left every MCP client on the machine authenticating
# with a stale one, silently.
_state_dir = os.environ.get("QUERN_STATE_DIR")
CONFIG_DIR = Path(_state_dir) if _state_dir else Path.home() / ".quern"
API_KEY_FILE = CONFIG_DIR / "api-key"
USER_CONFIG_FILE = CONFIG_DIR / "config.json"


@dataclass
class ServerConfig:
    """Configuration for the Quern debug log server."""

    #: Loopback (#254, phase 2): the API carries the owner's key in clear, so
    #: it is not offered to the LAN unless someone asks with --host. The
    #: proxy and the viewer have their own listeners and keep theirs.
    host: str = "127.0.0.1"
    port: int = DEFAULT_SERVER_PORT
    ring_buffer_size: int = 10_000
    #: What `device_id` a source gets when it does not know which device it
    #: is reading. Empty, not a name: a sentinel that looks like a device
    #: passes every truthiness check and fails every equality one, which is
    #: how app log lines came to be treated as belonging to a device called
    #: "default" and matched nothing anywhere.
    #:
    #: oslog and crash watch the *host*, so for them this is honest rather
    #: than a placeholder -- a macOS crash report genuinely belongs to no
    #: simulator.
    default_device_id: str = ""
    api_key: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        if not self.api_key:
            self.api_key = self._load_or_create_api_key()

    @staticmethod
    def _load_or_create_api_key() -> str:
        """Load existing API key or generate a new one."""
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)

        if API_KEY_FILE.exists():
            key = API_KEY_FILE.read_text().strip()
            if key:
                # `.strip()` takes control characters off the ends but not out
                # of the middle, and `isascii()` calls them ASCII -- so a key
                # with an embedded CR, LF or NUL passes both. No client can
                # send one: httpx builds the header and then refuses at the
                # wire with "Illegal header value", naming the character and
                # not the file. That is the same failure the check below
                # exists to prevent, so it is the same refusal.
                control = next(
                    (c for c in key if ord(c) < 0x20 or ord(c) == 0x7F), None,
                )
                if control is not None:
                    raise ValueError(
                        f"The API key in {API_KEY_FILE} contains a control "
                        f"character (0x{ord(control):02x}), which HTTP headers "
                        "cannot carry, so no client could authenticate. Edit "
                        "the file, or delete it to have a new key generated."
                    )
                if not key.isascii():
                    # No HTTP client can send it: httpx raises
                    # UnicodeEncodeError and node's Headers a TypeError, both
                    # naming a character rather than the file it came from.
                    # The server would start and refuse every request. This is
                    # a hand-edited file, so a pasted smart quote is the likely
                    # cause -- say that here rather than let it present as
                    # "authentication is broken".
                    raise ValueError(
                        f"The API key in {API_KEY_FILE} contains non-ASCII "
                        "characters, which HTTP headers cannot carry, so no "
                        "client could authenticate. Edit the file, or delete "
                        "it to have a new key generated."
                    )
                return key

        key = secrets.token_urlsafe(32)
        API_KEY_FILE.write_text(key)
        API_KEY_FILE.chmod(0o600)
        return key

    @staticmethod
    def regenerate_api_key() -> str:
        """Generate a new API key, replacing the existing one."""
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        key = secrets.token_urlsafe(32)
        API_KEY_FILE.write_text(key)
        API_KEY_FILE.chmod(0o600)
        return key


def client_api_key() -> str:
    """The key a quern client process sends: `QUERN_CLIENT_KEY` when set -- a
    client key from `quern clients add` (#254, phase 2) -- else the owner's key
    file. "" when neither.

    Not `QUERN_API_KEY`, which `quern env` exports with the owner's key for
    scripts: preferring it would pin an MCP started from such a shell to a key
    that `quern regenerate-key` has since replaced (review)."""
    explicit = os.environ.get("QUERN_CLIENT_KEY", "").strip()
    if explicit:
        return explicit
    try:
        return API_KEY_FILE.read_text().strip()
    except OSError:
        return ""


def bind_warning(host: str) -> str | None:
    """What to tell someone who bound the API beyond loopback, or None.

    Honoured, not refused (#254, phase 2): it was the default until then. But
    every key -- the owner's and every client's -- crosses the network in
    clear, and anyone who can reach the port can try them."""
    import ipaddress

    try:
        if host == "localhost" or ipaddress.ip_address(host).is_loopback:
            return None
    except ValueError:
        pass
    return (f"the API is listening on {host}, beyond this Mac: API keys -- the owner's "
            "and every client's -- cross the network unencrypted. Drop --host to keep it "
            "on 127.0.0.1; cross-machine access is planned as a separate TLS listener (#254).")


class ConfigUnreadable(OSError):
    """~/.quern/config.json exists and is not a JSON object: writing it now
    would replace every setting in it with the one being written."""


def read_user_config(*, strict: bool = False) -> dict:
    """Read user config from ~/.quern/config.json. Returns {} if missing or invalid.

    "Invalid" includes a document that parses but is not an object. A config
    holding `[]` or `"on"` or `42` is valid JSON, so it came back as-is and
    every caller's `.get()` raised AttributeError -- which read commands turned
    into a crash and the periodic check swallowed, silently skipping the
    automatic update check. The type is in the signature; honour it.
    """
    if not USER_CONFIG_FILE.exists():
        return {}
    try:
        parsed = json.loads(USER_CONFIG_FILE.read_text())
    except Exception as e:
        if strict:
            raise ConfigUnreadable(f"{USER_CONFIG_FILE} cannot be read ({e}); not "
                                   f"overwriting it -- fix or remove it first") from e
        logger.warning("Failed to read config file %s: %s", USER_CONFIG_FILE, e)
        return {}
    if not isinstance(parsed, dict):
        if strict:
            raise ConfigUnreadable(f"{USER_CONFIG_FILE} holds {type(parsed).__name__}, not an "
                                   f"object; not overwriting it -- fix or remove it first")
        logger.warning(
            "Config file %s holds %s, not an object; ignoring it",
            USER_CONFIG_FILE, type(parsed).__name__,
        )
        return {}
    return parsed


def get_default_device_family() -> str:
    """Return the configured default device family, defaulting to 'iPhone'."""
    return read_user_config().get("default_device_family", "iPhone")


# ---------------------------------------------------------------------------
# Update channel — `stable` (default) or `beta`. Drives which branch
# `quern update` checks against and which GitHub release filter the
# tarball updater uses (#41).
# ---------------------------------------------------------------------------

DEFAULT_CRASH_RETENTION_DAYS = 30


def get_crash_retention_days() -> int:
    """Days to keep pulled crash reports on the Mac; 0 keeps them forever (#322).

    `crash_retention_days` in config.json. Anything but a non-negative whole
    number reads as the default -- a typo should keep the usual behaviour, and
    never turn into "delete everything now". A bool is not a number here,
    though Python says it is.
    """
    raw = read_user_config().get("crash_retention_days")
    if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
        return raw
    return DEFAULT_CRASH_RETENTION_DAYS


VALID_UPDATE_CHANNELS = ("stable", "beta")
DEFAULT_UPDATE_CHANNEL = "stable"


def get_update_channel() -> str:
    """Return the configured update channel, defaulting to ``stable``.

    Unknown values fall back to the default rather than throwing — a
    typo in config.json shouldn't break the daemon.
    """
    raw = read_user_config().get("update_channel")
    if isinstance(raw, str) and raw in VALID_UPDATE_CHANNELS:
        return raw
    return DEFAULT_UPDATE_CHANNEL


def set_update_channel(channel: str) -> None:
    """Persist the update channel preference. Raises ValueError on an
    unknown channel name to prevent typos from silently no-op'ing."""
    if channel not in VALID_UPDATE_CHANNELS:
        raise ValueError(
            f"Unknown update channel {channel!r}. "
            f"Valid: {', '.join(VALID_UPDATE_CHANNELS)}"
        )
    update_user_config(lambda c: c.__setitem__("update_channel", channel))


#: The LAN viewer's defaults (#449). Port 9120 was chosen by the maintainer;
#: quern's own port scan excludes it (see `viewer_settings`).
VIEWER_DEFAULT_HOST = "0.0.0.0"
VIEWER_DEFAULT_PORT = 9120
#: How long a viewer link keeps working after its recording stops.
VIEWER_GRACE_SECONDS = 600


def _host_name(value: str) -> str:
    """'ci.lan:9120' or '[fe80::1]:9120' to its name, lowercased."""
    value = value.strip().lower()
    if value.startswith("["):
        return value[1:value.find("]")] if "]" in value else ""
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


def sessions_settings() -> dict:
    """Held sessions' settings from config.json's `sessions` object (#254).

        {"sessions": {"grace_seconds": 180, "visibility": "shared"}}

    `grace_seconds` is how long an abruptly dropped session keeps its devices,
    clamped to 120-300 (the maintainer's 2-5 minutes); anything that is not a
    plain number is the default, so a typo never means "no grace".

    `visibility` is "shared" or "blind" (#254, phase 2). Anything else is
    shared, and `visibility_note` says so: a typo must not quietly show every
    client everyone's data -- but it must not lock the owner out either, and
    shared is what the server did before the setting existed."""
    from server.session_registry import clamp_grace

    raw = read_user_config().get("sessions")
    raw = raw if isinstance(raw, dict) else {}
    visibility = raw.get("visibility", "shared")
    if isinstance(visibility, str):
        # " Blind" means blind: falling back to shared over a capital letter
        # would show every client everything (review).
        visibility = visibility.strip().lower()
    note = None
    if visibility not in ("shared", "blind"):
        note = (f"sessions.visibility is {visibility!r}, which is neither \"shared\" nor "
                "\"blind\": running shared, so every client sees every device's data")
        visibility = "shared"
    return {"grace_seconds": clamp_grace(raw.get("grace_seconds")),
            "visibility": visibility, "visibility_note": note}


def viewer_settings() -> dict:
    """The LAN viewer's settings from config.json's `viewer` object (#449).

        {"viewer": {"enabled": true, "bind": "0.0.0.0:9120",
                    "hosts": ["ci.lan"], "jmuxer_path": "/path/jmuxer.min.js"}}

`hosts` are extra names the listener answers to, for links someone rewrote
to a DNS name; a port written with one is ignored, since Host is matched by
name. `bind` takes an IPv6 host in brackets, `[::]:9120`.

    Off unless `enabled` is literally true: it is the first quern listener
    meant to be reached from another machine, so a typo must mean off. A
    malformed `bind` falls back to the default rather than raising, and is
    reported in `bind_error`.
    """
    raw = read_user_config().get("viewer")
    raw = raw if isinstance(raw, dict) else {}
    host, port, bind_error = _parse_bind(raw.get("bind"), "viewer.bind",
                                         VIEWER_DEFAULT_HOST, VIEWER_DEFAULT_PORT)
    jmuxer = raw.get("jmuxer_path")
    return {
        "enabled": raw.get("enabled") is True,
        "host": host,
        "port": port,
        "bind_error": bind_error,
        "hosts": _host_names(raw.get("hosts")),
        "jmuxer_path": jmuxer if isinstance(jmuxer, str) and jmuxer else None,
        "grace_seconds": VIEWER_GRACE_SECONDS,
    }


def _parse_bind(bind, key: str, default_host: str, default_port: int
                ) -> tuple[str, int, str | None]:
    """`host:port` from a listener's `bind`, an IPv6 host in brackets; the
    defaults and a reason when it is malformed, never an exception."""
    host, port, bind_error = default_host, default_port, None
    if isinstance(bind, str) and bind:
        h, sep, p = bind.rpartition(":")
        if h.startswith("[") and h.endswith("]"):
            h = h[1:-1]                          # "[::]:9120", "[fe80::1]:9120"
        try:
            port_n = int(p)
            if not sep or not (1 <= port_n <= 65535) or ("[" in h or "]" in h):
                raise ValueError(bind)
            if ":" in h and not bind.startswith("["):
                raise ValueError(bind)           # "::1:9120" is ambiguous
            host, port = (h or default_host), port_n
        except ValueError:
            bind_error = (f"{key} {bind!r} is not host:port (an IPv6 host in "
                          f"brackets); using {host}:{port}")
    return host, port, bind_error


def _host_names(hosts) -> list[str]:
    return ([n for n in (_host_name(h) for h in hosts if isinstance(h, str)) if n]
            if isinstance(hosts, list) else [])


_HOST_ID: str | None = None


def host_id() -> str:
    """This installation's id: random, made once, kept in ~/.quern/host-id
    (#254, phase 3). Not the Mac's hardware id -- nothing about the machine
    is given to whoever asks /health -- just enough for a client to tell
    "same disk as me" from "another machine". Read once per process; an
    unreadable or unwritable file gives a per-process id, which errs towards
    "not the same machine"."""
    global _HOST_ID
    if _HOST_ID is not None:
        return _HOST_ID
    path = CONFIG_DIR / "host-id"
    try:
        value = path.read_text().strip()
    except OSError:
        value = ""
    if not re.fullmatch(r"[0-9a-f]{32}", value):
        value = secrets.token_hex(16)
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            path.write_text(value + "\n")
        except OSError:
            pass
    _HOST_ID = value
    return value


REMOTE_DEFAULT_HOST = "0.0.0.0"
REMOTE_DEFAULT_PORT = 9130


def remote_settings() -> dict:
    """The remote listener's settings from config.json's `remote` object
    (#254, phase 3): quern reached from another machine, over TLS, by client
    keys only.

        {"remote": {"enabled": true, "bind": "0.0.0.0:9130", "hosts": ["ci.lan"]}}

    Off unless `enabled` is literally true, as the viewer is: a typo must
    mean off for a listener other machines can reach. `hosts` are extra names
    the listener answers to, besides this Mac's LAN address."""
    raw = read_user_config().get("remote")
    raw = raw if isinstance(raw, dict) else {}
    host, port, bind_error = _parse_bind(raw.get("bind"), "remote.bind",
                                         REMOTE_DEFAULT_HOST, REMOTE_DEFAULT_PORT)
    return {"enabled": raw.get("enabled") is True, "host": host, "port": port,
            "bind_error": bind_error, "hosts": _host_names(raw.get("hosts")),
            "key_warning": _unused_remote_keys(raw)}


_REMOTE_KEYS = ("enabled", "bind", "hosts", "client_viewer_links")


def client_viewer_links() -> bool:
    """Whether a client key may issue viewer links for its own recordings
    (#559): `remote.client_viewer_links`, on when unset or literally true.

    On by default because it grants nothing new: links exist only where the
    owner has enabled the viewer, and only for a client's own recordings,
    whose files it can already download in full. An owner who wants to be the
    only one handing out links sets it to false.

    Every other value is off, and so is a config.json that cannot be read: an
    owner who wrote "false", or set false and then broke the file elsewhere,
    turned it off and must not find it back on (review)."""
    try:
        config = read_user_config(strict=True)
    except ConfigUnreadable:
        return False
    raw = config.get("remote")
    if not isinstance(raw, dict) or "client_viewer_links" not in raw:
        return raw is None or isinstance(raw, dict)
    return raw["client_viewer_links"] is True


def _unused_remote_keys(raw: dict) -> str | None:
    """Words for keys in `remote` that nothing reads, or None. A `port` set
    here was ignored without a word, and the listener came up on 9130."""
    unused = sorted(k for k in raw if k not in _REMOTE_KEYS)
    if not unused:
        return None
    hint = (' -- the address and port are one key, "bind": "0.0.0.0:9130"'
            if {"port", "host", "address"} & set(unused) else "")
    return (f"config.json's remote has keys quern does not use: {', '.join(unused)}"
            f"{hint}. It reads {', '.join(_REMOTE_KEYS)}.")


#: The default cap on what one key may have uploaded at once (#254, phase 3b).
UPLOAD_DEFAULT_MB = 2048


#: Free space the uploads' volume keeps by default (#481).
UPLOAD_DEFAULT_MIN_FREE_MB = 4096


def upload_settings() -> dict:
    """The upload limits from config.json's `uploads` object:

        {"uploads": {"max_mb": 2048, "min_free_mb": 4096}}

    `max_mb` is what one key may hold at once; `min_free_mb` the free space
    the volume keeps, whatever every key holds together (0 turns it off).
    A value that is not a whole number of megabytes -- or, for `max_mb`,
    not positive -- reads as the default, with a note."""
    raw = read_user_config().get("uploads")
    raw = raw if isinstance(raw, dict) else {}
    notes = []

    def whole(key: str, default: int, least: int) -> int:
        value = raw.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or value < least:
            notes.append(f"config.json's uploads.{key} is {value!r}, not a whole number of "
                         f"megabytes {'above 0' if least else 'of 0 or more'}: "
                         f"using {default}")
            return default
        return value

    max_mb = whole("max_mb", UPLOAD_DEFAULT_MB, 1)
    min_free_mb = whole("min_free_mb", UPLOAD_DEFAULT_MIN_FREE_MB, 0)
    return {"max_bytes": max_mb * 1024 * 1024, "min_free_bytes": min_free_mb * 1024 * 1024,
            "note": "; ".join(notes) or None}


def get_auto_install_cert() -> bool:
    """Whether to install the mitmproxy CA automatically when capture needs it.

    Defaults to False, and deliberately: installing a MITM root CA is a larger
    and longer-lived commitment than the proxy toggle that prompts it. It
    persists across sessions, outlives the capture window, and the user has to
    know it happened in order to undo it. So the first encounter costs one
    round of asking, and this makes the second onwards free.

    Anything other than a real boolean is treated as unset -- a typo should
    read as "ask me", never as consent.
    """
    return read_user_config().get("auto_install_cert") is True


def auto_install_cert_choice() -> bool | None:
    """The same setting, but able to say "never answered".

    `get_auto_install_cert` folds unset into False, which is right for its
    callers: they are asking "may I install this without asking", and silence
    is not permission. A caller that is deciding *whether to ask* needs the
    third answer, because re-asking a question the user has already answered
    -- either way -- is how a considered "no" gets overturned by a tired "yes",
    and how a considered "yes" costs them the prompt they turned off.

    True or False only for a literal boolean. A typo reads as unset here for
    the reason it reads as False there: it should mean "ask me", never consent.
    """
    value = read_user_config().get("auto_install_cert")
    return value if isinstance(value, bool) else None


@contextmanager
def _config_lock():
    """Hold an exclusive lock for a whole read-modify-write of the config.

    Each setter reads the file, changes one key and replaces it. Two of them at
    once -- the menu bar writes this file, and so does every `quern set-...`
    command -- both read the same starting point and the second replacement
    drops the first one's key. Nothing in the file says it happened.

    The lock is its own file rather than the config: the config is replaced
    rather than written in place, so a lock held on it would be a lock on an
    inode nobody looks at any more.
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = CONFIG_DIR / "config.lock"
    try:
        # "a", not "w". "w" truncates, which needs write permission on an
        # existing file -- so one `sudo quern set-...` leaves a root-owned lock
        # file and every later write by the user falls through to the unlocked
        # path from then on, permanently, behind a warning nobody reads.
        handle = lock_path.open("a")
    except OSError:
        # Cannot lock. Proceeding unlocked is what happened before there was a
        # lock at all, and refusing to save a setting because a lock file could
        # not be created would be a worse trade than a rare lost key.
        logger.warning("Could not open %s; writing config unlocked", lock_path)
        yield
        return
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except OSError as e:
            # flock is not supported on every filesystem -- SMB, AFP and some
            # NFS mounts answer ENOTSUP or ENOLCK -- and a home directory on
            # one of those is not exotic. Letting this escape would propagate
            # out of a setter, so the setting is lost and the CLI exits with a
            # traceback. Same trade as above: unlocked beats not saved.
            logger.warning("Could not lock %s (%s); writing config unlocked",
                           lock_path, e)
        yield
    finally:
        handle.close()


def update_user_config(change: Callable[[dict], None], *, strict: bool = False) -> None:
    """Apply `change` to the config and persist it, as one atomic step.

    The read and the write are inside one lock deliberately. Reading outside it
    and writing inside would still let two callers start from the same snapshot,
    which is the whole failure.

    `change` must not call this again. The lock is not reentrant -- a nested
    call opens a second descriptor and blocks against the first, in the same
    thread, forever. Reading with `read_user_config()` inside `change` is fine;
    it takes no lock. Nothing nests today, and the callable is the reason to
    say so rather than leave it to be discovered.

    Swapped in rather than written over: the menu-bar app reads this file on a
    poll, and ``write_text`` truncates before it writes, so a read landing in
    that window gets a partial document. ``os.replace`` is atomic within a
    filesystem; the temporary lives in the same directory for that reason, since
    a move across filesystems is a copy and the window comes back.
    """
    with _config_lock():
        # Strict, for a writer that must not replace a file it cannot read:
        # the read that decides is this one, inside the lock -- a check
        # before it can be outrun by an edit (CodeRabbit).
        config = read_user_config(strict=True) if strict else read_user_config()
        change(config)
        tmp = USER_CONFIG_FILE.with_name(f".{USER_CONFIG_FILE.name}.{os.getpid()}.tmp")
        try:
            tmp.write_text(json.dumps(config, indent=2) + "\n")
            os.replace(tmp, USER_CONFIG_FILE)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise


def knowledge_bases_readable() -> bool:
    """False when the config exists and cannot be read, so "nothing
    remembered" is never said on the strength of a file nobody could read."""
    try:
        read_user_config(strict=True)
    except ConfigUnreadable:
        return False
    return True


def get_knowledge_bases() -> dict[str, str]:
    """Remembered knowledge bases, app -> path: loaded at every start.

    An entry that is not a non-empty string under a string key is left out;
    the reload names what it could not use rather than this guessing at it.
    """
    raw = read_user_config().get("knowledge_bases")
    if not isinstance(raw, dict):
        return {}
    return {app: path for app, path in raw.items()
            if isinstance(app, str) and isinstance(path, str) and path}


def remember_knowledge_base(app: str, path: str) -> None:
    """Load `path` for `app` at every start from now on. Raises
    ConfigUnreadable rather than replace a config it cannot read."""
    def change(config: dict) -> None:
        kbs = config.get("knowledge_bases")
        if not isinstance(kbs, dict):
            kbs = {}
        kbs[app] = path
        config["knowledge_bases"] = kbs
    update_user_config(change, strict=True)


def forget_knowledge_base(app: str | None) -> list[str]:
    """Stop loading `app`'s knowledge base at start -- every one, for None.
    Returns the apps forgotten. Raises ConfigUnreadable rather than replace
    a config it cannot read."""
    forgotten: list[str] = []

    def change(config: dict) -> None:
        kbs = config.get("knowledge_bases")
        if not isinstance(kbs, dict):
            return
        for name in ([app] if app is not None else list(kbs)):
            if name in kbs:
                del kbs[name]
                forgotten.append(name)
        config["knowledge_bases"] = kbs
    update_user_config(change, strict=True)
    return forgotten


def set_auto_install_cert(enabled: bool) -> None:
    """Persist the auto-install policy.

    Surfaced in ``proxy_status`` and in the menu-bar app's Settings pane, both
    on purpose: a silent, persistent CA-install policy would be worse than the
    failure it exists to prevent.
    """
    update_user_config(lambda c: c.__setitem__("auto_install_cert", bool(enabled)))


def get_update_check() -> bool:
    """Whether quern checks for updates on its own.

    Defaults to True: this is the "check automatically" box, and it starts
    ticked. Note the asymmetry with ``auto_install_cert``, which requires a
    literal ``true`` -- there, anything unclear must read as "ask me", because
    the cost of guessing wrong is a root CA installed without consent. Here the
    cost of guessing wrong is one HTTPS request a day, so only an explicit
    ``false`` turns it off and a typo leaves checking on.

    Governs the *automatic* check alone. ``quern check-updates`` and the menu
    bar's Check for Updates ignore it, the way every other updater leaves Check
    Now working when the box is unticked.
    """
    return read_user_config().get("update_check") is not False


def set_update_check(enabled: bool) -> None:
    """Persist whether the automatic update check runs."""
    update_user_config(lambda c: c.__setitem__("update_check", bool(enabled)))


def channel_to_release_branch(channel: str) -> str:
    """Map a channel name to its reserved release pointer branch.

    Maintainers fast-forward these branches on each release cut —
    ``release/stable`` to the latest tagged stable release, ``release/
    beta`` to the latest prerelease commit (or main HEAD).
    """
    return f"release/{channel}"


def get_local_capture_processes() -> list[str]:
    """Return the list of process names for local capture mode.

    Returns [] if not configured (disabled).
    Handles legacy bool values: True -> default process list, False -> [].
    """
    value = read_user_config().get("local_capture")
    if value is None or value is False:
        return []
    if value is True:
        # Legacy bool: default to the minimum web-capture set
        return list(CAPTURE_MINIMUM)
    if isinstance(value, list):
        return [str(v) for v in value if v]
    return []


#: Always captured alongside whatever a caller names, unless they ask for
#: exactly their own list.
#:
#: `com.apple.WebKit.Networking` is where a webview's requests actually leave.
#: Naming only the app captures none of its web traffic -- not less of it,
#: none -- and the result is zero flows with no error, which looks exactly
#: like an app that made no requests. That mistake is common enough to design
#: against: it has cost several sessions, including one where it was
#: misdiagnosed as a broken redirector.
#:
#: `MobileSafari` is here for a less obvious reason: an OAuth flow hands off
#: to real Safari. `ASWebAuthenticationSession` and `SFSafariViewController`
#: are Safari, so a login journey leaves the app's own processes entirely.
#: Debugging sign-in without it means watching the interesting half vanish.
CAPTURE_MINIMUM: tuple[str, ...] = ("MobileSafari", "com.apple.WebKit.Networking")


def with_capture_minimum(processes: list[str]) -> tuple[list[str], list[str]]:
    """Add the minimum capture set to `processes`. Returns (list, added).

    Order is preserved and the caller's own entries come first, so the list
    still reads as "what I asked for, plus what makes it work".

    An empty list is returned untouched: that means *disable capture*, and
    quietly turning it into "capture the defaults" would be the opposite of
    what was asked.
    """
    if not processes:
        return [], []
    added = [p for p in CAPTURE_MINIMUM if p not in processes]
    return [*processes, *added], added


def leading_exclusion(processes: list[str]) -> str | None:
    """The first non-blank entry if it is an exclusion (``!...``), else None.

    mitmproxy's local mode takes its starting point from the *first* entry
    (``InterceptConf::new`` in mitmproxy_rs): a list that opens with ``!X``
    intercepts every process on the Mac except X, and later entries can only
    add to that. ``with_capture_minimum`` appends after the caller's entries,
    so it does not narrow it either. ``["!12345"]`` therefore means "the whole
    machine minus one PID" -- and excluding one process is the natural first
    thing to try, so every entry point refuses it unless whole-Mac capture was
    asked for by name.
    """
    for entry in processes:
        entry = entry.strip()
        if entry:
            return entry if entry.startswith("!") else None
    return None


def whole_mac_refusal(entry: str, how_to_ask: str) -> str:
    """Why an exclusion-first list is refused, and the two ways forward."""
    return (
        f"The capture list starts with the exclusion {entry!r}. mitmproxy takes "
        "its starting point from the first entry, so this would capture EVERY "
        "process on the Mac except the ones excluded -- not the processes you "
        "named, minus one. To exclude something from a capture, put the "
        f"exclusion after an include: [\"MyApp\", \"{entry}\"]. To work with one "
        "simulator, don't change the list at all: filter flows by "
        f"simulator_udid. If you do mean every process on the Mac, pass {how_to_ask}."
    )


def set_local_capture_processes(processes: list[str]) -> None:
    """Set the local_capture process list in ~/.quern/config.json.

    An empty list disables local capture.
    """
    update_user_config(lambda c: c.__setitem__("local_capture", processes))


# ---------------------------------------------------------------------------
# Plist watch config
# ---------------------------------------------------------------------------


def get_plist_watch_config() -> dict[str, dict]:
    """Return plist_watch config keyed by bundle_id.

    Each value has a "watches" list of {container, plist_path, ignore_prefixes} dicts.
    """
    return read_user_config().get("plist_watch", {})


def set_plist_watch_config(
    bundle_id: str,
    watches: list[dict],
) -> None:
    """Save plist watch config for a bundle_id.

    watches: list of {container, plist_path, ignore_prefixes} dicts.
    """
    def _set(config: dict) -> None:
        config.setdefault("plist_watch", {})[bundle_id] = {"watches": watches}

    update_user_config(_set)


def clear_plist_watch_config(bundle_id: str) -> bool:
    """Remove plist watch config for a bundle_id. Returns True if it existed."""
    if bundle_id not in read_user_config().get("plist_watch", {}):
        return False

    def _clear(config: dict) -> None:
        # Re-read under the lock, so the decision and the write see the same
        # document. The check above is only to answer False without taking it.
        config.get("plist_watch", {}).pop(bundle_id, None)

    update_user_config(_clear)
    return True


def _is_git_clone(root: Path) -> bool:
    """A clone or worktree: `.git` is a directory in one and a file in the other."""
    return (root / ".git").exists()


def quern_cmd() -> str:
    """How to invoke quern, spelled for where the reader is actually standing.

    Three answers, because there are three situations and only one of them
    tolerates a guess.

    * **`quern`** -- there is a wrapper on PATH, so the bare name works from
      anywhere. This is the normal state after `quern setup`.
    * **`./quern`** -- no wrapper, but the reader's working directory *is* the
      project. A developer in a clone before first setup.
    * **the absolute path** -- no wrapper, and the reader is somewhere else.

    That third case is the one this exists for. An install script drops quern in
    `~/.local/share/quern` and leaves the terminal wherever it was, so a tarball
    user told to run `./quern setup` is being pointed at a file that is not in
    their directory -- and they are the least likely person to work out why. The
    absolute path is longer and it is correct.

    `shutil.which` rather than `WRAPPER_PATH.exists()`: the wrapper can exist
    while `~/.local/bin` is off PATH, which CONTRIBUTING documents as a live
    failure, and what matters is whether typing `quern` works.

    Deliberately not cached. The answer depends on PATH, on whether the wrapper
    exists, and on the current directory -- and all three move: setup installs
    the wrapper mid-run, uninstall removes it, and anything may `chdir`. A cache
    here would need invalidating at each of those, which is three chances to
    miss one. A PATH scan while building an error string is not worth that.
    """
    if shutil.which("quern"):
        return "quern"
    # server/config.py -> server/ -> project root. Derived from this file rather
    # than searched for, so it holds wherever the tree has been moved to.
    wrapper = Path(__file__).resolve().parent.parent / "quern"
    if not wrapper.exists():
        return "quern"  # nothing better to offer; naming a missing file is worse
    # `./quern` only for a developer in a clone. A tarball install's setup is
    # run *by install.sh*, which cds into the install directory first -- so
    # this process stands in the project while the person reading its output
    # does not, and `./quern setup` sent them to a file that isn't there.
    # Measured in a clean VM (#455).
    try:
        if Path.cwd().resolve() == wrapper.parent and _is_git_clone(wrapper.parent):
            return "./quern"
    except OSError:
        pass  # cwd can be deleted out from under a process
    # Quoted, because this is the only branch that can contain a space and it
    # is the one people are meant to paste. `shlex.quote` is a no-op for
    # `quern` and `./quern`, so the common answers are unchanged.
    return shlex.quote(str(wrapper))
