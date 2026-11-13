"""A physical iPhone's accessibility tree, read without WebDriverAgent (#664).

On iOS 27 the device's accessibility service sends the whole on-screen tree to
a host that asks for it: ``clientNeedsAccessibility:`` = YES on the DTX control
channel of ``com.apple.accessibility.axAuditDaemon.remoteAXService``, reached
over the device's RSD tunnel like quern's other in-process pymobiledevice3
paths. The device answers with ``processDataFromRemoteDevice:`` pushes, each
one NSData holding an NSKeyedArchive: an ``InitialDump`` within about a second,
then an ``AdditionalData`` push carrying the same elements with their
identifiers added. AXe 2 published the route (github.com/cameroncooke/AXe,
``docs/device-protocols.md``).

Unlike AXe, nothing here projects the archive through the Mac's private
AccessibilityPlatformTranslation framework. The attributes -- frames in points
included -- are already in the archive, so a plain decode is enough: no
compiled helper, and so nothing that needs Xcode on the Mac.

The attribute numbers are AXPTranslator's private enumeration, read off real
pushes and checked against WDA's tree for the same screen (Settings on an
iPhone 11 on iOS 27.2.1, #664): identifiers matched, frames within a point,
values and types agreed. Only what was confirmed is mapped; an element whose
traits match none of the confirmed bits keeps a generic type rather than a
guessed one.

What the push holds differs from WDA's tree, and on purpose: it is what
VoiceOver can reach. Only on-screen elements; a row's inner texts merged into
the row's own label ("Wi-Fi, MonaLisaOverdrive" where WDA has a button and two
texts); and nothing hidden under a toolbar -- a row WDA calls on screen behind
Settings' search bar is absent. Web content is included, read from the web
process like any other.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import plistlib
import re
import time
from types import SimpleNamespace
from typing import Any

from server.models import DeviceError

logger = logging.getLogger(__name__)

SERVICE = "com.apple.accessibility.axAuditDaemon.remoteAXService"

#: The first iOS version the reader serves. Measured on 27.0.1 and 27.2.1;
#: below that it has not been tried, and #624's touch input refuses iOS 26.
COREDEVICE_MIN_IOS = 27

#: The selector every tree push arrives under.
PUSH_SELECTOR = "processDataFromRemoteDevice:"

#: `associatedRequestType` of the response that carries one element's
#: attributes, keyed by attribute number.
ELEMENT_BUNDLE = 5

#: Attribute numbers, as confirmed against WDA (see the module docstring).
ATTR_ACTIVATION_POINT = 1
ATTR_LABEL = 3
ATTR_CUSTOM_ACTIONS = 13
ATTR_FRAME = 21
ATTR_IDENTIFIER = 25  # only in the AdditionalData push
ATTR_VALUE = 53
ATTR_TRAITS = 77

#: UIAccessibilityTraits bits. The public ones are UIKit's; the two private
#: ones are inferred from where they appeared: `TOGGLE` only on Settings'
#: Airplane Mode switch (WDA's `Switch`), `TEXT_ENTRY` on both text inputs
#: seen, Settings' search field and Safari's address field.
TRAIT_BUTTON = 1 << 0
TRAIT_LINK = 1 << 1
TRAIT_IMAGE = 1 << 2
TRAIT_STATIC_TEXT = 1 << 6
TRAIT_NOT_ENABLED = 1 << 8
TRAIT_SEARCH_FIELD = 1 << 10
TRAIT_ADJUSTABLE = 1 << 12
TRAIT_HEADER = 1 << 16
TRAIT_TEXT_ENTRY = 1 << 18
TRAIT_TOGGLE = 1 << 53

#: How long a read may take. AXe stops at 20 s too, which it measured as about
#: 330 elements; the screens measured here took 1.8-2.5 s.
READ_TIMEOUT_S = 20.0

#: After an AdditionalData push, how long to wait for a newer one before
#: taking it. A screen still loading -- Home's widgets, a page in Safari --
#: sends a second pair a second or so later.
QUIET_S = 0.6

_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


class AxTreeError(ValueError):
    """A push that is not an accessibility tree quern can read."""


def _unarchive(data: bytes) -> Any:
    """Decode an NSKeyedArchive with no class map.

    Collections become lists and dicts, strings and data their Python
    equivalents, NSValue its string form (``{{x, y}, {w, h}}``), and any other
    class a dict of its fields with ``$c`` naming it -- which is all the tree
    needs, and means an unexpected class is data rather than an error.
    """
    try:
        plist = plistlib.loads(data)
        objects = plist["$objects"]
        root = plist["$top"]["root"]
    except (plistlib.InvalidFileException, ValueError, KeyError, TypeError) as e:
        raise AxTreeError(f"not a keyed archive: {e!r}") from e

    memo: dict[int, Any] = {}

    def decode(value: Any) -> Any:
        if isinstance(value, list):
            return [decode(v) for v in value]
        if not isinstance(value, plistlib.UID):
            return value
        index = value.data
        if index == 0:
            return None
        if index in memo:
            return memo[index]
        obj = objects[index]
        if not (isinstance(obj, dict) and "$class" in obj):
            memo[index] = obj
            return obj
        name = objects[obj["$class"].data].get("$classname")
        if "NS.keys" in obj:
            out: Any = {}
            memo[index] = out
            for k, v in zip(obj["NS.keys"], obj["NS.objects"], strict=False):
                out[decode(k)] = decode(v)
            return out
        if "NS.objects" in obj:
            out = []
            memo[index] = out
            out.extend(decode(v) for v in obj["NS.objects"])
            return out
        if name in ("NSString", "NSMutableString"):
            memo[index] = out = decode(obj.get("NS.string"))
            return out
        if name in ("NSData", "NSMutableData"):
            memo[index] = out = obj.get("NS.data")
            return out
        if name == "NSValue":
            for key in ("NS.rectval", "NS.pointval", "NS.sizeval"):
                if key in obj:
                    memo[index] = out = decode(obj[key])
                    return out
        out = {"$c": name}
        memo[index] = out
        for k, v in obj.items():
            if k != "$class":
                out[k] = decode(v)
        return out

    try:
        return decode(root)
    except (IndexError, KeyError, TypeError, AttributeError) as e:
        raise AxTreeError(f"malformed keyed archive: {e!r}") from e


def _text(value: Any) -> str | None:
    """A label or value: a string, or an attributed string's text."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get("NSString"), str):
        return value["NSString"]
    return None


def _rect(value: Any) -> dict[str, float] | None:
    """``{{x, y}, {w, h}}`` in points, or None for anything else."""
    if not isinstance(value, str):
        return None
    numbers = _NUMBER.findall(value)
    if len(numbers) != 4:
        return None
    x, y, w, h = (float(n) for n in numbers)
    return {"x": x, "y": y, "width": w, "height": h}


def _element_type(traits: int, *, is_root: bool) -> str:
    """The accessibility tree's type for an element, as sim-bridge and idb
    name it on a simulator, so landmarks and type filters carry across."""
    if is_root:
        return "Application"
    if traits & TRAIT_HEADER:
        return "Heading"
    if traits & TRAIT_TOGGLE:
        return "CheckBox"  # a UISwitch; WDA's `Switch` (a safe pair)
    if traits & (TRAIT_TEXT_ENTRY | TRAIT_SEARCH_FIELD):
        return "TextField"
    if traits & TRAIT_ADJUSTABLE:
        return "Slider"
    if traits & TRAIT_LINK:
        return "Link"
    if traits & TRAIT_BUTTON:
        return "Button"
    if traits & TRAIT_IMAGE:
        return "Image"
    if traits & TRAIT_STATIC_TEXT:
        return "StaticText"
    return "Group"


def _custom_actions(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [a["AXPCustomActionName"] for a in value
            if isinstance(a, dict) and isinstance(a.get("AXPCustomActionName"), str)]


def parse_push(data: bytes) -> tuple[str, list[dict]]:
    """One push's tree, as ``(dump_type, elements)``.

    `dump_type` is ``"InitialDump"`` or ``"AdditionalData"``; `elements` are
    the dicts `describe_all` returns on every iOS backend (idb's field names),
    the application first. Raises AxTreeError for anything that is not a tree
    dump, including a push of another kind.
    """
    archive = _unarchive(data)
    result = archive.get("resultData") if isinstance(archive, dict) else None
    if not (isinstance(result, dict) and isinstance(result.get("treeDump"), list)):
        raise AxTreeError("push holds no tree dump")
    dump_type = str(result.get("treeDumpType") or "")
    dump_type = dump_type.removeprefix("AXPTreeDumpType")

    elements: list[dict] = []
    for response in result["treeDump"]:
        if not isinstance(response, dict):
            continue
        if response.get("associatedRequestType") != ELEMENT_BUNDLE:
            continue
        bundle = response.get("resultData")
        if not isinstance(bundle, dict):
            continue
        traits = bundle.get(ATTR_TRAITS)
        traits = traits if isinstance(traits, int) else 0
        etype = _element_type(traits, is_root=not elements)
        elements.append({
            "type": etype,
            "role": f"AX{etype}",
            "role_description": "AXSearchField" if traits & TRAIT_SEARCH_FIELD else "",
            "AXLabel": _text(bundle.get(ATTR_LABEL)) or "",
            "AXUniqueId": _text(bundle.get(ATTR_IDENTIFIER)),
            "AXValue": _text(bundle.get(ATTR_VALUE)),
            "frame": _rect(bundle.get(ATTR_FRAME)),
            "enabled": not traits & TRAIT_NOT_ENABLED,
            "help": None,
            "custom_actions": _custom_actions(bundle.get(ATTR_CUSTOM_ACTIONS)),
        })
    if not elements:
        raise AxTreeError(f"{dump_type or 'tree dump'} holds no elements")
    return dump_type, elements


def _pmd3() -> SimpleNamespace:
    """The pymobiledevice3 pieces the reader uses, in one place so it can be
    tested without a phone."""
    try:
        from pymobiledevice3.dtx_service_provider import DtxServiceProvider
        from pymobiledevice3.remote.remote_service_discovery import (
            RemoteServiceDiscoveryService,
        )
    except ImportError as e:
        raise DeviceError(
            f"reading a physical iPhone's screen without WDA needs pymobiledevice3 "
            f"11.20.0 or later in quern's environment ({e})", tool="coredevice") from e

    class RemoteAX(DtxServiceProvider):
        SERVICE_NAME = SERVICE
        RSD_SERVICE_NAME = SERVICE

    return SimpleNamespace(RemoteServiceDiscoveryService=RemoteServiceDiscoveryService,
                           RemoteAX=RemoteAX)


async def _newest_tree(queue: asyncio.Queue) -> tuple[str, list[dict]]:
    """Read pushes until an AdditionalData one has had QUIET_S to be replaced.

    Pushes that are not tree dumps -- the device also sends notifications --
    are skipped. Returns only on a complete tree; the caller's deadline is what
    ends a wait for one that never comes.
    """
    newest: tuple[str, list[dict]] | None = None
    while True:
        try:
            if newest is None:
                selector, args = await queue.get()
            else:
                selector, args = await asyncio.wait_for(queue.get(), QUIET_S)
        except TimeoutError:
            assert newest is not None
            return newest
        if selector != PUSH_SELECTOR or not args or not isinstance(args[0], bytes):
            continue
        try:
            dump_type, elements = parse_push(args[0])
        except AxTreeError:
            continue
        if dump_type == "AdditionalData":
            newest = (dump_type, elements)
        elif newest is not None:
            # A newer InitialDump: the screen changed under the read, so the
            # pair it starts is the one to wait for.
            newest = None


async def read_tree(uuid: str, *, timeout: float = READ_TIMEOUT_S) -> list[dict]:
    """The on-screen accessibility tree of a physical iPhone, as `describe_all`
    returns it. Raises DeviceError when it cannot be read."""
    from server.device.performance import _device_address

    lib = _pmd3()
    address = await _device_address(uuid)
    rsd = lib.RemoteServiceDiscoveryService(address)
    started = time.monotonic()
    deadline = asyncio.timeout(timeout)
    try:
        async with deadline:
            try:
                await rsd.connect()
                provider = lib.RemoteAX(rsd)
                await provider.connect()
                try:
                    queue: asyncio.Queue = asyncio.Queue()
                    provider.dtx.ctx["control_dispatch_queue"] = queue
                    control = provider.dtx._ctrl_channel
                    await control.invoke("clientNeedsAccessibility:", True,
                                         expects_reply=False)
                    _, elements = await _newest_tree(queue)
                finally:
                    # NO ends the subscription on the device's side; a failure
                    # to send it must not hide the read's own outcome.
                    with contextlib.suppress(Exception):
                        await provider.dtx._ctrl_channel.invoke(
                            "clientNeedsAccessibility:", False, expects_reply=False)
                    with contextlib.suppress(Exception):
                        await provider.close()
            finally:
                with contextlib.suppress(Exception):
                    await rsd.close()
    except TimeoutError as e:
        if deadline.expired():
            raise DeviceError(
                f"{uuid[:8]} sent no complete accessibility tree within {timeout:g}s",
                tool="coredevice") from None
        raise DeviceError(f"accessibility service on {uuid[:8]} timed out: {e!r}",
                          tool="coredevice") from e
    except DeviceError:
        raise
    except Exception as e:  # pymobiledevice3 raises its own hierarchy
        raise DeviceError(f"accessibility service on {uuid[:8]} failed: {e!r}",
                          tool="coredevice") from e
    logger.info("[coredevice] tree of %s: %d elements in %.2fs",
                uuid[:8], len(elements), time.monotonic() - started)
    return elements


class CoreDeviceAxReader:
    """`describe_all` for a physical iPhone, from the tree push.

    A reader only: taps and every other action stay with WDA in this phase,
    so this is chosen for reads alone (`DeviceController._device_tree_reader`).
    """

    TOOL_NAME = "coredevice"

    async def describe_all(self, udid: str, *, snapshot_depth: int | None = None,
                           source_timeout: float | None = None,
                           probe: bool = True) -> list[dict]:
        # snapshot_depth and probe are WDA's and sim-bridge's knobs; the push
        # is the whole on-screen tree, so there is nothing for them to change.
        return await read_tree(udid, timeout=source_timeout or READ_TIMEOUT_S)
