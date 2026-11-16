"""What a physical iPhone needs before quern can drive it without Xcode (#706).

Developer Mode, and the developer disk image: screenshots and the CoreDevice
app service need the image mounted. Xcode mounts it when it first meets a
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

from dataclasses import dataclass

IMAGE_SOURCE = (
    "pymobiledevice3 downloads Apple's developer disk image from its "
    "DeveloperDiskImage repository on GitHub, and Apple's signing server "
    "personalizes it for the phone")


@dataclass
class Readiness:
    """None for anything that could not be asked, with why in `errors`."""

    udid: str
    name: str
    os_version: str
    developer_mode: bool | None = None
    image_mounted: bool | None = None
    errors: list[str] | None = None


async def phones() -> list[tuple[str, str, str]]:
    """(udid, name, iOS version) of each iPhone on USB, every version."""
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
    wait on, or create, a Trust prompt. An unpaired phone raises, and is
    reported as such (CodeRabbit on #710)."""
    from pymobiledevice3.lockdown import create_using_usbmux

    return await create_using_usbmux(serial=udid, autopair=False)


def _major(version: str) -> int:
    try:
        return int(version.split(".")[0])
    except (ValueError, IndexError):
        return 0


async def readiness(udid: str) -> Readiness:
    """Developer Mode and the mounted image, each asked on its own, so one
    that cannot be read does not hide the other."""
    from pymobiledevice3.services.mobile_image_mounter import MobileImageMounterService

    errors: list[str] = []
    try:
        lockdown = await _lockdown(udid)
    except Exception as e:  # noqa: BLE001 -- said, not raised
        return Readiness(udid, "", "", errors=[
            f"could not connect ({e}); if the phone has not trusted this Mac, unlock it "
            "and tap Trust when it asks"])
    state = Readiness(udid, lockdown.all_values.get("DeviceName", ""),
                      lockdown.product_version, errors=errors)
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


async def mount_image(udid: str, os_version: str) -> None:
    """Mount the developer disk image with pymobiledevice3's auto-mount
    (`IMAGE_SOURCE`): over the phone's tunnel on iOS 17 and later, which
    need it for the image, and over USB lockdown before that, where there is
    no tunnel. Raises DeviceError when it cannot."""
    from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
    from pymobiledevice3.services.mobile_image_mounter import auto_mount

    from server.device.performance import _device_address
    from server.models import DeviceError

    rsd = None
    try:
        if _major(os_version) >= 17:
            rsd = RemoteServiceDiscoveryService(await _device_address(udid))
            await rsd.connect()
            await auto_mount(rsd)
        else:
            await auto_mount(await _lockdown(udid))
    except DeviceError:
        raise
    except Exception as e:  # noqa: BLE001 -- pymobiledevice3 raises its own kinds
        raise DeviceError(f"mounting the developer disk image on {udid[:8]} failed: {e}",
                          tool="pymobiledevice3") from e
    finally:
        if rsd is not None:
            try:
                await rsd.close()
            except Exception:  # noqa: BLE001
                pass


async def reveal_developer_mode(udid: str) -> None:
    """Show the Developer Mode switch in the phone's Settings, which iOS
    hides until a developer tool asks."""
    from pymobiledevice3.services.amfi import AmfiService

    await AmfiService(await _lockdown(udid)).reveal_developer_mode_option_in_ui()
