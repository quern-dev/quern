"""API route for building an Xcode project and installing the app on devices."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from server.models import BuildResult, DeviceError, DeviceType

router = APIRouter(prefix="/api/v1/device", tags=["device"])
logger = logging.getLogger("quern-debug-server.api")

BUILD_TIMEOUT = 600  # seconds


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class BuildAndInstallRequest(BaseModel):
    project_path: str
    scheme: str | None = None
    configuration: str = "Debug"
    # Single device (backward compat) or multiple devices.
    # Both may be supplied; they are merged into one list internally.
    udid: str | None = None
    udids: list[str] | None = None


class DeviceInstallResult(BaseModel):
    udid: str
    installed: bool
    app_path: str | None = None
    error: str | None = None


class BuildAndInstallResponse(BaseModel):
    # Per-architecture build results (None = that arch wasn't needed / failed early)
    build_iphoneos: BuildResult | None = None
    build_iphonesimulator: BuildResult | None = None
    # Per-device results
    devices: list[DeviceInstallResult]
    all_installed: bool


# ---------------------------------------------------------------------------
# Project / scheme helpers
# ---------------------------------------------------------------------------


def _resolve_project_path(path_str: str) -> tuple[str, str]:
    """Return (xcodebuild flag, resolved path). Prefers .xcworkspace."""
    p = Path(path_str).expanduser().resolve()
    if p.suffix == ".xcworkspace":
        return "-workspace", str(p)
    if p.suffix == ".xcodeproj":
        return "-project", str(p)
    for ws in sorted(p.glob("*.xcworkspace")):
        return "-workspace", str(ws)
    for proj in sorted(p.glob("*.xcodeproj")):
        return "-project", str(proj)
    raise ValueError(f"No .xcworkspace or .xcodeproj found in {path_str}")


async def _list_schemes(proj_flag: str, proj_path: str) -> list[str]:
    """Run `xcodebuild -list -json` and return the scheme list."""
    proc = await asyncio.create_subprocess_exec(
        "xcodebuild", proj_flag, proj_path, "-list", "-json",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
    try:
        import json
        data = json.loads(stdout.decode(errors="replace"))
        root = data.get("workspace") or data.get("project") or {}
        return root.get("schemes", [])
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Build helper
# ---------------------------------------------------------------------------


async def _build(
    proj_flag: str,
    proj_path: str,
    scheme: str,
    configuration: str,
    destination: str,
    derived_data: Path,
    build_adapter,
) -> BuildResult:
    """Run xcodebuild for one destination and return the parsed BuildResult."""
    cmd = [
        "xcodebuild",
        proj_flag, proj_path,
        "-scheme", scheme,
        "-configuration", configuration,
        "-destination", destination,
        "-derivedDataPath", str(derived_data),
        "build",
    ]
    logger.info("Building %s (scheme=%s, destination=%s)", proj_path, scheme, destination)
    logger.debug("xcodebuild command: %s", " ".join(cmd))

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=BUILD_TIMEOUT)
    except asyncio.TimeoutError:
        raise RuntimeError(f"Build timed out after {BUILD_TIMEOUT}s ({destination})")
    except FileNotFoundError:
        raise RuntimeError("xcodebuild not found — is Xcode installed?")

    return await build_adapter.parse_build_output(stdout_bytes.decode(errors="replace"))


# ---------------------------------------------------------------------------
# App discovery
# ---------------------------------------------------------------------------


def _find_app(derived_data: Path, config: str, is_physical: bool) -> Path | None:
    """Locate the built .app bundle in the DerivedData products directory."""
    suffix = "iphoneos" if is_physical else "iphonesimulator"
    products = derived_data / "Build" / "Products" / f"{config}-{suffix}"
    apps = list(products.glob("*.app"))
    return apps[0] if apps else None


# ---------------------------------------------------------------------------
# OS version check
# ---------------------------------------------------------------------------


def _read_minimum_os_version(app_path: Path) -> str | None:
    """Read MinimumOSVersion from the app bundle's Info.plist, or None if unavailable."""
    import plistlib

    info_plist = app_path / "Info.plist"
    if not info_plist.exists():
        return None
    try:
        with open(info_plist, "rb") as f:
            plist = plistlib.load(f)
        return plist.get("MinimumOSVersion") or plist.get("LSMinimumSystemVersion")
    except Exception:
        return None


def _parse_version_tuple(version_str: str) -> tuple[int, ...]:
    """Convert '17.0' or 'iOS 17.0' into (17, 0)."""
    import re
    parts = re.findall(r"\d+", version_str)
    return tuple(int(p) for p in parts) if parts else (0,)


def _check_minimum_os(app_path: Path, device_os_version: str) -> str | None:
    """Return an error string if the device OS is below the app's minimum, else None."""
    min_os = _read_minimum_os_version(app_path)
    if not min_os or not device_os_version:
        return None
    if _parse_version_tuple(device_os_version) < _parse_version_tuple(min_os):
        return (
            f"Device OS {device_os_version} is below the app's minimum deployment "
            f"target {min_os} — install skipped"
        )
    return None


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.post("/build-and-install", response_model=BuildAndInstallResponse)
async def build_and_install(request: Request, body: BuildAndInstallRequest):
    """Build an Xcode scheme and install the app on one or more devices/simulators.

    Builds once per required architecture:
    - Physical devices  → ``generic/platform=iOS``        (one build, N installs)
    - Simulators        → ``generic/platform=iOS Simulator`` (one build, N installs)

    Both architectures are built concurrently when the target list mixes device types.

    If ``scheme`` is omitted, returns HTTP 400 listing all available schemes.
    UDID format translation for physical iOS 17+ devices is handled internally.
    """
    controller = request.app.state.device_controller
    if controller is None:
        raise HTTPException(status_code=503, detail="Device controller not initialized")

    build_adapter = request.app.state.build_adapter
    if build_adapter is None:
        raise HTTPException(status_code=503, detail="Build adapter not initialized")

    # 1. Resolve project path
    try:
        proj_flag, proj_path = _resolve_project_path(body.project_path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # 2. Scheme discovery
    if not body.scheme:
        schemes = await _list_schemes(proj_flag, proj_path)
        detail = "scheme is required"
        if schemes:
            detail = f"scheme is required. Available schemes: {schemes}"
        raise HTTPException(status_code=400, detail=detail)

    # 3. Collect and resolve UDIDs
    raw_udids: list[str] = []
    if body.udid:
        raw_udids.append(body.udid)
    if body.udids:
        for u in body.udids:
            if u not in raw_udids:
                raw_udids.append(u)

    if not raw_udids:
        # No UDIDs specified — auto-resolve single active device
        try:
            raw_udids = [await controller.resolve_udid(None)]
        except DeviceError as e:
            raise HTTPException(status_code=400, detail=str(e))

    resolved_udids: list[str] = []
    for u in raw_udids:
        try:
            resolved_udids.append(await controller.resolve_udid(u))
        except DeviceError as e:
            raise HTTPException(status_code=400, detail=f"Cannot resolve {u}: {e}")

    # 4. Partition by architecture
    physical_udids = [u for u in resolved_udids if controller._device_type(u) == DeviceType.DEVICE]
    simulator_udids = [u for u in resolved_udids if controller._device_type(u) == DeviceType.SIMULATOR]

    derived = Path.home() / ".quern" / "builds" / body.scheme
    derived.mkdir(parents=True, exist_ok=True)

    # 5. Build each needed architecture concurrently
    build_tasks: dict[str, asyncio.Task] = {}

    if physical_udids:
        build_tasks["iphoneos"] = asyncio.create_task(
            _build(
                proj_flag, proj_path, body.scheme, body.configuration,
                "generic/platform=iOS",
                derived, build_adapter,
            )
        )
    if simulator_udids:
        build_tasks["iphonesimulator"] = asyncio.create_task(
            _build(
                proj_flag, proj_path, body.scheme, body.configuration,
                "generic/platform=iOS Simulator",
                derived, build_adapter,
            )
        )

    build_results: dict[str, BuildResult] = {}
    build_errors: dict[str, str] = {}
    for arch, task in build_tasks.items():
        try:
            build_results[arch] = await task
        except RuntimeError as e:
            build_errors[arch] = str(e)

    result_iphoneos = build_results.get("iphoneos")
    result_iphonesimulator = build_results.get("iphonesimulator")

    # 6. Install on each device (parallel), skipping if its build failed
    async def _install_one(udid: str, is_physical: bool) -> DeviceInstallResult:
        arch = "iphoneos" if is_physical else "iphonesimulator"

        if arch in build_errors:
            return DeviceInstallResult(udid=udid, installed=False, error=build_errors[arch])

        build_result = build_results.get(arch)
        if build_result is None or not build_result.succeeded:
            return DeviceInstallResult(udid=udid, installed=False, error="Build did not succeed")

        app_path = _find_app(derived, body.configuration, is_physical)
        if app_path is None:
            return DeviceInstallResult(
                udid=udid, installed=False,
                error=f".app not found in DerivedData for {arch}",
            )

        # OS version check (physical devices only — simulators always match)
        if is_physical:
            os_version = controller.wda_client._device_os_versions.get(udid, "")
            os_error = _check_minimum_os(app_path, os_version)
            if os_error:
                return DeviceInstallResult(udid=udid, installed=False, error=os_error)

        # Auto-boot shutdown simulators (simctl install requires a booted sim)
        if not is_physical:
            try:
                logger.info("Ensuring simulator %s is booted before install", udid[:8])
                await controller.simctl.boot(udid)
            except DeviceError as e:
                err_str = str(e)
                # "Unable to boot device in current state: Booted" is not an error
                if "current state: Booted" not in err_str:
                    return DeviceInstallResult(udid=udid, installed=False, error=f"Boot failed: {err_str}")

        try:
            await controller.install_app(str(app_path), udid)
        except DeviceError as e:
            return DeviceInstallResult(udid=udid, installed=False, error=str(e))

        return DeviceInstallResult(udid=udid, installed=True, app_path=str(app_path))

    install_tasks = (
        [_install_one(u, True) for u in physical_udids]
        + [_install_one(u, False) for u in simulator_udids]
    )
    device_results: list[DeviceInstallResult] = await asyncio.gather(*install_tasks)

    return BuildAndInstallResponse(
        build_iphoneos=result_iphoneos,
        build_iphonesimulator=result_iphonesimulator,
        devices=device_results,
        all_installed=all(r.installed for r in device_results),
    )
