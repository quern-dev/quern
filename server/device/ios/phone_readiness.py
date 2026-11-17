"""What a physical iPhone needs before quern can drive it without Xcode (#706).

Developer Mode, and the developer disk image: input (taps, typing, buttons)
and the CoreDevice app service need the image mounted. Screen reads,
screenshots and crash reports do not (measured on an iPhone 11 with it
removed). Xcode mounts it when it first meets a
phone, and on iOS 27 it stays: measured on an iPhone 11, it came back mounted
after a restart with Xcode hidden, from the cryptex iOS keeps
(`com.apple.MobileAsset.DDI`). A phone that has never met Xcode has none, and
on a Mac without Xcode nothing mounts one unless quern does.

Quern mounts it with pymobiledevice3's auto-mount, which downloads Apple's
image from pymobiledevice3's DeveloperDiskImage repository on GitHub and has
it personalized for the phone by Apple's signing server. That is said
wherever it happens (`IMAGE_SOURCE`).
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from server.logging_ext import current_action

IMAGE_SOURCE = (
    "pymobiledevice3 downloads Apple's developer disk image from its "
    "DeveloperDiskImage repository on GitHub, and Apple's signing server "
    "personalizes it for the phone")


#: What to do about a missing image, said where its absence shows.
NO_IMAGE_FIX = (
    "The phone has no developer disk image mounted, which its HID and app services "
    "need, and quern could not mount it just now: `quern doctor --fix` tries again "
    "and says why")


def missing_image(error: BaseException) -> bool:
    """Whether `error`, or what caused it, is the phone lacking its developer
    disk image: pymobiledevice3 says so in the error it raises when a service
    the image provides is asked for. Measured on an iPhone 11 (iOS 27.2.1)
    with the image removed: the HID and app services both failed this way,
    while the screen read, screenshots and crash reports still worked."""
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if "DeveloperDiskImage" in type(error).__name__ or "DeveloperDiskImage" in str(error):
            return True
        error = error.__cause__ or error.__context__
    return False


@dataclass
class Readiness:
    """None for anything that could not be asked, with why in `errors`."""

    udid: str
    name: str
    os_version: str
    paired: bool | None = None
    developer_mode: bool | None = None
    image_mounted: bool | None = None
    errors: list[str] | None = None


#: How long the read-only checks may take: a phone that stops answering a
#: lockdown request would otherwise hold setup and doctor for ever, since
#: pymobiledevice3 waits on its reply with no deadline (CodeRabbit on #711).
LIST_TIMEOUT_S = 15.0
READ_TIMEOUT_S = 20.0
#: The mount downloads the image the first time, so it is given longer.
MOUNT_TIMEOUT_S = 300.0


async def phones() -> list[tuple[str, str, str]]:
    """(udid, name, iOS version) of each iPhone on USB, every version.
    Raises TimeoutError past `LIST_TIMEOUT_S`."""
    async with asyncio.timeout(LIST_TIMEOUT_S):
        return await _phones()


async def _phones() -> list[tuple[str, str, str]]:
    from pymobiledevice3.usbmux import list_devices

    found = []
    for device in await list_devices():
        if getattr(device, "connection_type", "USB") != "USB":
            continue
        try:
            lockdown = await _lockdown(device.serial)
        except Exception:  # noqa: BLE001 -- unpaired or locked: listed, unread
            found.append((device.serial, "", ""))
            continue
        found.append((device.serial, lockdown.all_values.get("DeviceName", ""),
                      lockdown.product_version))
    return found


async def _lockdown(udid: str):
    """A lockdown client over USB that never pairs: a doctor check must not
    wait on, or create, a Trust prompt (CodeRabbit on #710). An unpaired
    phone does not raise: the client comes back with `paired` False."""
    from pymobiledevice3.lockdown import create_using_usbmux

    return await create_using_usbmux(serial=udid, autopair=False)


def _major(version: str) -> int:
    try:
        return int(version.split(".")[0])
    except (ValueError, IndexError):
        return 0


TRUST = ("unlock it and tap Trust when it asks whether to trust this computer "
         "(unplug and plug it in again if it does not ask)")


async def readiness(udid: str) -> Readiness:
    """Whether it trusts this Mac, Developer Mode and the mounted image,
    each asked on its own, so one that cannot be read does not hide the
    other. A read that does not finish in `READ_TIMEOUT_S` is said as such."""
    try:
        async with asyncio.timeout(READ_TIMEOUT_S):
            return await _readiness(udid)
    except TimeoutError:
        return Readiness(udid, "", "", errors=[
            f"did not answer within {READ_TIMEOUT_S:.0f} s; unplug it, plug it in again "
            "and unlock it"])


async def _readiness(udid: str) -> Readiness:
    from pymobiledevice3.services.mobile_image_mounter import MobileImageMounterService

    errors: list[str] = []
    try:
        lockdown = await _lockdown(udid)
    except Exception as e:  # noqa: BLE001 -- said, not raised
        return Readiness(udid, "", "", errors=[
            f"could not connect ({e}); if the phone has not trusted this Mac, {TRUST}"])
    state = Readiness(udid, lockdown.all_values.get("DeviceName", ""),
                      lockdown.product_version, paired=bool(lockdown.paired), errors=errors)
    if not state.paired:
        # Nothing else can be asked of a phone that has not trusted this Mac.
        errors.append(f"it does not trust this Mac yet: {TRUST}")
        return state
    major = _major(state.os_version)
    # Developer Mode came with iOS 16: before it there is nothing to turn on,
    # which is neither on nor off.
    if major >= 16:
        try:
            state.developer_mode = await lockdown.get_developer_mode_status()
        except Exception as e:  # noqa: BLE001
            errors.append(f"Developer Mode could not be read: {e}")
    # LookupImage, which every version answers (CopyDevices is not on old ones),
    # for the kind of image this version takes.
    try:
        async with MobileImageMounterService(lockdown=lockdown) as mounter:
            state.image_mounted = await mounter.is_image_mounted(
                "Personalized" if major >= 17 else "Developer")
    except Exception as e:  # noqa: BLE001
        errors.append(f"the developer disk image could not be checked: {e}")
    return state


#: The mount running on its worker thread, per phone: one that ran past its
#: deadline is still running, and no second one starts beside it.
_workers: dict[str, asyncio.Future] = {}


async def mount_image(udid: str, os_version: str) -> bool:
    """Mount the developer disk image with pymobiledevice3's auto-mount
    (`IMAGE_SOURCE`): over the phone's tunnel on iOS 17 and later, which
    need it for the image, and over USB lockdown before that, where there is
    no tunnel. Raises DeviceError when it cannot.

    On a worker thread with its own event loop: auto-mount downloads and
    writes the image synchronously, which on the server's loop would hold
    every other request, and which no deadline could interrupt (CodeRabbit
    on #711). The deadline is kept here; a mount past it is left to finish
    on its thread, and the caller is told it did not; until it ends, no
    other mount of that phone starts.

    Returns whether this call mounted it: False when one was there already.
    """
    from server.models import DeviceError

    running = _workers.get(udid)
    if running is not None and not running.done():
        raise DeviceError(f"a developer disk image mount on {udid[:8]} that ran past its "
                          f"{MOUNT_TIMEOUT_S:.0f} s is still going", tool="pymobiledevice3")
    worker = asyncio.ensure_future(asyncio.to_thread(asyncio.run, _mount(udid, os_version)))
    _workers[udid] = worker
    try:
        # Shielded: the deadline stops the wait, never the worker it tracks.
        await asyncio.wait_for(asyncio.shield(worker), MOUNT_TIMEOUT_S)
    except TimeoutError as e:
        raise DeviceError(f"mounting the developer disk image on {udid[:8]} took over "
                          f"{MOUNT_TIMEOUT_S:.0f} s", tool="pymobiledevice3") from e
    except Exception as e:  # noqa: BLE001 -- pymobiledevice3 raises its own kinds
        if already_mounted(e):
            return False
        if isinstance(e, DeviceError):
            raise
        raise DeviceError(f"mounting the developer disk image on {udid[:8]} failed: {e}",
                          tool="pymobiledevice3") from e
    return True


async def _mount(udid: str, os_version: str) -> None:
    """The mount itself, on the worker's own loop: every connection it
    makes is made there."""
    from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
    from pymobiledevice3.services.mobile_image_mounter import auto_mount

    from server.device.performance import _device_address

    if _major(os_version) < 17:
        await auto_mount(await _lockdown(udid))
        return
    rsd = RemoteServiceDiscoveryService(await _device_address(udid))
    try:
        await rsd.connect()
        await auto_mount(rsd)
    finally:
        try:
            await rsd.close()
        except Exception:  # noqa: BLE001
            pass


def already_mounted(error: BaseException) -> bool:
    """pymobiledevice3's AlreadyMountedError, here or among its causes: a
    mount another call made first, which is the phone being ready."""
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if type(current).__name__ == "AlreadyMountedError":
            return True
        current = current.__cause__ or current.__context__
    return False


async def reveal_developer_mode(udid: str) -> None:
    """Show the Developer Mode switch in the phone's Settings, which iOS
    hides until a developer tool asks."""
    from pymobiledevice3.services.amfi import AmfiService

    await AmfiService(await _lockdown(udid)).reveal_developer_mode_option_in_ui()


# -- mounting on demand -------------------------------------------------------

logger = logging.getLogger(__name__)

#: Said on the response of the request that mounted the image. Kept on the
#: request's action scope (`ActionScope.developer_image`): its child tasks
#: share it, as `tap_element`'s work runs in one, and no other request can
#: take it (CodeRabbit on #711).
#: The holder `noted` sets for its route before the handler runs: a route
#: that opens its action scope inside the handler has closed it by the time
#: `noted` reads. Mutable and set first, so child tasks write to it too.
_HOLDER: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "developer_image_holder", default=None)
MOUNTED_NOTE = (f"The phone had no developer disk image, which this needs, so quern "
                f"mounted it: {IMAGE_SOURCE}.")
#: A phone whose mount failed is not asked again for this long, so every
#: call does not wait on a mount that cannot work.
RETRY_AFTER_S = 60.0
_failed: dict[str, float] = {}
_locks: dict[str, asyncio.Lock] = {}


async def ensure_image(udid: str) -> bool:
    """Mount the developer disk image a call found missing: whether it is
    mounted now. Measured on an iPhone 11 (iOS 27.2.1): 1 s with the image
    cached, 2 s downloading it, so the call that needs it waits for it rather
    than failing. One mount per phone at a time; a failed one is not tried
    again for `RETRY_AFTER_S`. Said on the call's response (`MOUNTED`)."""
    lock = _locks.setdefault(udid, asyncio.Lock())
    async with lock:
        if time.monotonic() - _failed.get(udid, float("-inf")) < RETRY_AFTER_S:
            return False
        try:
            async with asyncio.timeout(READ_TIMEOUT_S):
                version = (await _lockdown(udid)).product_version
            if await _image_is_mounted(udid, version):
                return True                 # another call mounted it meanwhile
            mounted_now = await mount_image(udid, version)
        except Exception as e:  # noqa: BLE001 -- the call's own error stands
            _failed[udid] = time.monotonic()
            logger.warning("[coredevice] could not mount the developer disk image on %s: %s",
                           udid[:8], e)
            return False
        _failed.pop(udid, None)
        if mounted_now:
            logger.info("[coredevice] mounted the developer disk image on %s", udid[:8])
            scope = current_action()
            if hasattr(scope, "developer_image"):
                scope.developer_image = MOUNTED_NOTE
            if (holder := _HOLDER.get()) is not None:
                holder["note"] = MOUNTED_NOTE
        return True


def take_note() -> str | None:
    """`MOUNTED_NOTE` when this request mounted the image, once."""
    scope = current_action()
    note = getattr(scope, "developer_image", None)
    if note:
        scope.developer_image = None
    return note


async def _image_is_mounted(udid: str, version: str) -> bool:
    """Asked again under the lock: a call that waited for another's mount
    finds it done, rather than mounting over it."""
    state = await readiness(udid)
    return bool(state.image_mounted)


def noted(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Put `take_note` on a route's dict response as `developer_image`."""
    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        holder: dict = {}
        token = _HOLDER.set(holder)
        try:
            result = await fn(*args, **kwargs)
        finally:
            _HOLDER.reset(token)
        if isinstance(result, dict) and (note := holder.get("note")):
            result.setdefault("developer_image", note)
        return result
    return wrapper
