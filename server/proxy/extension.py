"""Health of the mitmproxy macOS system extension used by local capture.

Local capture routes a chosen process's traffic through mitmproxy without
touching the system proxy, and it does that with a network system extension --
`org.mitmproxy.macos-redirector.network-extension`, shipped as a tarred app
inside the `mitmproxy_macos` wheel and activated by macOS on first use.

The failure this exists to catch is quiet. The extension is approved once, by a
human, in System Settings; the wheel that ships it is then upgraded underneath
that approval by any ordinary dependency update. `mitmproxy-rs` moved to 0.12.11
during the upgrade that prompted this module. When the shipped bundle version
moves past the activated one, macOS keeps running the old extension and local
capture goes on reporting itself as enabled while capturing nothing.

Nothing else in quern looks at this: `proxy_status` reports the mitmdump process
and the configured process list, both of which stay perfectly healthy while the
extension underneath them is stale.
"""

from __future__ import annotations

import plistlib
import re
import subprocess
import sys
import tarfile
from dataclasses import dataclass
from pathlib import Path

BUNDLE_ID = "org.mitmproxy.macos-redirector.network-extension"
APP_TAR_NAME = "Mitmproxy Redirector.app.tar"
_EXTENSION_PLIST = (
    "Mitmproxy Redirector.app/Contents/Library/SystemExtensions/"
    f"{BUNDLE_ID}.systemextension/Contents/Info.plist"
)

# One row of `systemextensionsctl list`, which is tab-separated but pads
# irregularly. Anchored on the bundle id so the surrounding columns can move.
_ROW = re.compile(
    r"(?P<bundle>[\w.\-]+)\s+\((?P<short>[^/)]+)/(?P<build>[^)]+)\)"
    r".*?\[(?P<state>[^\]]+)\]"
)


@dataclass
class ExtensionHealth:
    """What state the redirector extension is in."""

    status: str
    """healthy | stale | not_activated | not_shipped | unsupported | unknown."""

    detail: str
    shipped: str | None = None
    activated: str | None = None
    activated_state: str | None = None
    remedy: str | None = None

    @property
    def fixable(self) -> bool:
        return self.status in ("stale", "not_activated")


def _tar_path() -> Path | None:
    """Where the shipped app tar lives, or None if mitmproxy_macos is absent."""
    try:
        import mitmproxy_macos
    except Exception:
        return None
    location = getattr(mitmproxy_macos, "__file__", None)
    if not location:
        return None
    candidate = Path(location).parent / APP_TAR_NAME
    return candidate if candidate.is_file() else None


def shipped_version() -> str | None:
    """The extension version inside the installed wheel, as `short/build`.

    Read straight out of the tar rather than from an extracted copy: extracting
    is what activation does, and a health check must not have side effects.
    """
    tar_path = _tar_path()
    if tar_path is None:
        return None
    try:
        with tarfile.open(tar_path) as archive:
            member = archive.extractfile(_EXTENSION_PLIST)
            if member is None:
                return None
            info = plistlib.loads(member.read())
    except (OSError, tarfile.TarError, plistlib.InvalidFileException, KeyError):
        return None
    short = info.get("CFBundleShortVersionString")
    build = info.get("CFBundleVersion")
    if not short or not build:
        return None
    return f"{short}/{build}"


def activated_extension() -> tuple[str, str] | None:
    """The activated `(version, state)` for the redirector, or None if absent.

    `systemextensionsctl list` needs no privileges, which is what lets this run
    inside read-only diagnostics without a sudo prompt.
    """
    try:
        result = subprocess.run(
            ["systemextensionsctl", "list"],
            capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        if BUNDLE_ID not in line:
            continue
        match = _ROW.search(line)
        if match and match.group("bundle") == BUNDLE_ID:
            return f"{match.group('short')}/{match.group('build')}", match.group("state")
    return None


def extension_health() -> ExtensionHealth:
    """Whether the activated extension matches the one currently shipped."""
    if sys.platform != "darwin":
        return ExtensionHealth(
            status="unsupported",
            detail="local capture's system extension is macOS-only",
        )

    shipped = shipped_version()
    if shipped is None:
        return ExtensionHealth(
            status="not_shipped",
            detail="mitmproxy_macos is not installed, so local capture cannot run",
            remedy="reinstall quern's dependencies: quern doctor --fix",
        )

    found = activated_extension()
    if found is None:
        return ExtensionHealth(
            status="not_activated", shipped=shipped,
            detail="not registered with macOS; local capture has never been approved",
            remedy="approve it in System Settings > General > Login Items & Extensions",
        )

    activated, state = found
    if activated != shipped:
        return ExtensionHealth(
            status="stale", shipped=shipped, activated=activated, activated_state=state,
            detail=(
                f"macOS is running {activated} but the installed wheel ships "
                f"{shipped}; local capture reports enabled while capturing nothing"
            ),
            remedy="quern doctor --fix re-runs the shipped app to activate it",
        )

    if "activated" not in state or "enabled" not in state:
        return ExtensionHealth(
            status="not_activated", shipped=shipped, activated=activated,
            activated_state=state,
            detail=f"registered but not active (state: {state})",
            remedy="enable it in System Settings > General > Login Items & Extensions",
        )

    return ExtensionHealth(
        status="healthy", shipped=shipped, activated=activated, activated_state=state,
        detail=f"{activated} activated and enabled",
    )


# Where the redirector app is unpacked. A stable location rather than a temp
# directory: macOS keeps referring to the app bundle that registered an
# extension, and unpacking to somewhere that gets cleaned up leaves the
# activated extension pointing at nothing.
INSTALL_DIR = Path.home() / ".quern" / "mitmproxy-redirector"


def reinstall() -> tuple[bool, str]:
    """Unpack the shipped redirector and run it so macOS activates it.

    There is no Python API for this. `mitmproxy_macos` is a data-only package --
    its `__init__.py` is empty -- and activation happens inside
    `mitmproxy_rs.local.LocalRedirector` when capture starts. Running the app
    directly is what mitmproxy itself relies on.

    This cannot complete unattended: macOS shows an approval prompt in System
    Settings and waits for a human. So the honest return is "handed off", not
    "installed", and the caller must say so rather than reporting success.
    """
    tar_path = _tar_path()
    if tar_path is None:
        return False, "mitmproxy_macos is not installed; nothing to unpack"

    try:
        INSTALL_DIR.mkdir(parents=True, exist_ok=True)
        with tarfile.open(tar_path) as archive:
            # filter="data" refuses absolute paths, traversal and device nodes.
            # The archive is trusted, but this runs against whatever version of
            # the wheel happens to be installed, which is not a thing to assume.
            archive.extractall(INSTALL_DIR, filter="data")
    except (OSError, tarfile.TarError, ValueError) as exc:
        return False, f"could not unpack the redirector: {exc}"

    app = INSTALL_DIR / "Mitmproxy Redirector.app"
    if not app.is_dir():
        return False, f"unpacked, but {app.name} is not where it was expected"

    try:
        result = subprocess.run(
            ["open", "-a", str(app)], capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"could not launch the redirector: {exc}"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        return False, f"launching the redirector failed: {detail}"

    return True, (
        f"launched {app.name}. macOS will ask you to approve the extension in "
        "System Settings > General > Login Items & Extensions — it is not active "
        "until you do. Re-run `quern doctor` afterwards to confirm."
    )
