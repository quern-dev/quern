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
from collections.abc import Callable
from dataclasses import dataclass, field
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

#: The quiet wait instead when the screen sent notifications during the read:
#: it is changing, and an app that is going to die under the read -- see
#: `AppExitedDuringRead` -- is replaced about a second after its last tree.
UNSETTLED_QUIET_S = 1.5

#: `associatedRequestType` of the response naming the application element.
APPLICATION_RESPONSE = 4

#: Once a complete tree is in hand, the longest a read waits for a newer one.
#: A loading screen settles within a second or so; one that never settles
#: (a clock, a spinner) is answered with its newest complete tree.
SETTLE_MAX_S = 2.0

#: A pair already under way -- a newer InitialDump in, its AdditionalData not
#: yet -- gets the time the device takes to build one, which grows with the
#: tree: about 0.9 s plus 15 ms an element, as AXe measured. Measured here
#: (#676): after a tap into Settings > Battery the 28-element pair completed
#: 0.87-1.02 s after its InitialDump, and in one run of three that ran past
#: SETTLE_MAX_S, so the read answered with the 12-element page still loading.
PAIR_BASE_S = 0.9
PAIR_PER_ELEMENT_S = 0.015

#: However many pairs are under way, a read answers this long after its first
#: complete tree, with the newest complete tree it has.
SETTLE_HARD_S = 4.0

#: How long a bare tree waits before it is read again (#664): one read is not
#: taken as the wedge, so a screen caught in a moment of change is not
#: reported as a phone that needs a restart. Measured on an iPhone 15 Pro, a
#: read straight after a launch already carried the app's name, 10 of 10.
BARE_CONFIRM_S = 1.0

#: How long a subscription waits for its first push of any kind before it is
#: made again on a fresh connection, once (#684). Measured on an iPhone 11: a
#: subscription made the moment an app was relaunched got nothing for 20 s,
#: in 2 of 3 tries; asking again on the same connection got nothing either,
#: and a fresh connection had the tree 1.6 s later. A healthy first push
#: comes at about 1.0-1.3 s, every time it was measured; a needless fresh
#: connection costs only itself.
FIRST_PUSH_S = 3.0

#: Each cleanup step's bound: unsubscribing and closing must not hold a read
#: up, or change its outcome.
CLOSE_S = 2.0

_NUMBER = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")


class AxTreeError(ValueError):
    """A push that is not an accessibility tree quern can read."""


class AppExitedDuringRead(DeviceError):
    """The app on screen died while its tree was being read (#669).

    An iOS bug, not the app's or quern's: on iOS 27 a tree read crashes some
    UIKit apps inside UIKit's own accessibility code
    (`_accessibilityWindowSections` inserting nil) -- measured on 27.0.1 and
    27.2.1, on a tab that is a UINavigationController reached through a tab
    bar's More list. Unsubscribing as soon as the first dump arrives does not
    save it, so the read cannot be made safe once it has started; this is how
    the caller learns that the read did it, rather than meeting an empty
    screen with no explanation.
    """

    def __init__(self, app: str, pid: int) -> None:
        super().__init__(
            f"{app or 'the app'} (pid {pid}) exited while its screen was read through "
            "the accessibility service: an iOS 27 bug in UIKit's accessibility code "
            "crashes some apps under this read (#669)",
            tool="coredevice")
        self.app = app
        self.pid = pid


class BareAppTree(DeviceError):
    """The phone answered for the app on screen with a bare, unlabelled
    `Application` and nothing else (#664).

    Measured on an iPhone 15 Pro on iOS 27.0.1: every app launched after
    some point read this way, while SpringBoard and apps already running kept
    reading normally, the shape of the simulator wedge in `ax_recovery.py`
    (#66). A restart cleared it; what started it is not known. A healthy
    app's `Application` carries the app's name, so an app with no
    accessibility of its own does not read as this. Raised rather than
    returned because the tree reads as a screen holding nothing, which
    every lookup on it then reports as not found.
    """

    def __init__(self, uuid: str) -> None:
        super().__init__(
            f"{uuid[:8]}'s accessibility service answered with an empty tree for the "
            "app on screen, a bare unlabelled Application. On a phone this has been "
            "a wedge in iOS: apps launched after it read empty while the home screen "
            "reads normally, and restarting the phone clears it (#664)",
            tool="coredevice")


def _refuse_bare(elements: list[dict], uuid: str) -> list[dict]:
    """The tree, unless it is the wedge's bare `Application` (#664)."""
    if (len(elements) == 1 and elements[0].get("type") == "Application"
            and not elements[0].get("AXLabel")):
        raise BareAppTree(uuid)
    return elements


@dataclass
class Tree:
    """One tree dump: its kind, its elements, and the pid of its app.

    `refs` holds each element's `(pid, objectID)`, by index, or None where the
    push carried none: what a request about that element names (`set_value`).
    """

    dump_type: str
    elements: list[dict]
    app_pid: int | None
    refs: list[tuple[int, int] | None] = field(default_factory=list)


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
            memo[index] = None  # a string that names itself decodes to None
            memo[index] = out = decode(obj.get("NS.string"))
            return out
        if name in ("NSData", "NSMutableData"):
            memo[index] = out = obj.get("NS.data")
            return out
        if name == "NSValue":
            for key in ("NS.rectval", "NS.pointval", "NS.sizeval"):
                if key in obj:
                    memo[index] = None
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
    except RecursionError as e:
        # Nesting no real tree reaches: the push is skipped like any other
        # unreadable one, rather than ending the read.
        raise AxTreeError("keyed archive nested too deeply") from e


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
    """`parse_tree` as ``(dump_type, elements)``."""
    tree = parse_tree(data)
    return tree.dump_type, tree.elements


def parse_tree(data: bytes) -> Tree:
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
    refs: list[tuple[int, int] | None] = []
    app_pid = None
    for response in result["treeDump"]:
        if (isinstance(response, dict)
                and response.get("associatedRequestType") == APPLICATION_RESPONSE
                and isinstance(response.get("resultData"), dict)
                and isinstance(response["resultData"].get("pid"), int)):
            app_pid = response["resultData"]["pid"]
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
        refs.append(_ref(response.get("associatedTranslationObject")))
    if not elements:
        raise AxTreeError(f"{dump_type or 'tree dump'} holds no elements")
    return Tree(dump_type, elements, app_pid, refs)


def _ref(translation: Any) -> tuple[int, int] | None:
    """An element's `(pid, objectID)` from its translation object, or None."""
    if not isinstance(translation, dict):
        return None
    pid, object_id = translation.get("pid"), translation.get("objectID")
    if isinstance(pid, int) and isinstance(object_id, int) and not isinstance(pid, bool):
        return pid, object_id
    return None


#: AXPTranslatorRequest's type for setting an attribute, and the client it
#: speaks as -- what Apple's own translator sends (#664, measured).
REQUEST_SET_ATTRIBUTE = 8
CLIENT_TYPE = 2


def set_value_request(pid: int, object_id: int, value: str) -> bytes:
    """A keyed archive asking the device to set an element's value (#664).

    The shape Apple's AccessibilityPlatformTranslation archives, built here so
    quern needs no private framework. `objectID` is an object of its own, not
    an inline integer: measured on an iPhone 15 Pro, a request carrying it
    inline is accepted and silently does nothing.
    """
    uid = plistlib.UID
    objects: list = [
        "$null",
        {"$class": uid(8), "actionType": 0, "attributeType": ATTR_VALUE,
         "clientType": CLIENT_TYPE, "parameters": uid(2),
         "requestType": REQUEST_SET_ATTRIBUTE, "translation": uid(5)},
        {"$class": uid(7), "NS.keys": [uid(3)], "NS.objects": [uid(4)]},
        "value",
        value,
        {"$class": uid(6), "bridgeDelegateToken": uid(0), "didPopuldateAppInfo": True,
         "isApplicationElement": False, "objectID": uid(9), "pid": pid,
         "rawElementData": uid(0)},
        {"$classes": ["AXPTranslationObject", "NSObject"],
         "$classname": "AXPTranslationObject"},
        {"$classes": ["NSDictionary", "NSObject"], "$classname": "NSDictionary"},
        {"$classes": ["AXPTranslatorRequest", "NSObject"],
         "$classname": "AXPTranslatorRequest"},
        object_id,
    ]
    return plistlib.dumps({"$archiver": "NSKeyedArchiver", "$objects": objects,
                           "$top": {"root": uid(1)}, "$version": 100000},
                          fmt=plistlib.FMT_BINARY)


def _pmd3() -> SimpleNamespace:
    """The pymobiledevice3 pieces the reader uses, in one place so it can be
    tested without a phone."""
    try:
        from pymobiledevice3.dtx_service_provider import DtxServiceProvider
        from pymobiledevice3.remote.core_device.app_service import AppServiceService
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
                           RemoteAX=RemoteAX, AppService=AppServiceService)


def _pair_budget(initial: Tree) -> float:
    """How long a pair started by this InitialDump gets to complete: the time a
    device takes to build a tree its size (PAIR_BASE_S, PAIR_PER_ELEMENT_S).
    The newest InitialDump's, which supersedes an earlier one under way."""
    return PAIR_BASE_S + PAIR_PER_ELEMENT_S * len(initial.elements)


async def _newest_tree(queue: asyncio.Queue,
                       held: dict | None = None) -> tuple[Tree, Tree, Tree]:
    """Read pushes until a complete tree has had a quiet spell to be replaced.

    Returns the newest complete tree, the first tree of the read, and the
    newest tree of either kind. A caller compares the first with the newest
    to see whether the app changed under the read: on the crash (#669) the
    replacing app's InitialDump arrives inside the wait, while its complete
    tree may come after it, so comparing with the complete tree misses it.

    A complete tree is an AdditionalData push, the one with identifiers.
    Pushes that are not tree dumps are the device's notifications; a static
    screen sends none, so any at all mean the screen is changing, and the
    quiet spell is UNSETTLED_QUIET_S rather than QUIET_S. A push that cannot
    be decoded is skipped. A newer InitialDump means the screen changed under
    the read, so its pair is waited for; but once a complete tree is in hand
    the wait for anything newer ends SETTLE_MAX_S later, and the newest
    complete tree is the answer -- unless a newer pair is under way then: it
    gets PAIR_BASE_S plus PAIR_PER_ELEMENT_S for each element its InitialDump
    carries, to finish (#676), and a complete tree that arrives after the
    settle window gets a quiet spell of its own, so a page loading in stages
    is waited for stage by stage. SETTLE_HARD_S after the first complete tree
    the read answers regardless. `held`, when given, is kept up to date with
    the trees in hand ("latest", "first", "newest"), so a caller whose own
    deadline ends the wait can still answer with them. A screen that never stops changing -- a
    clock, a spinner -- would otherwise never settle, and every read of it
    would run out the caller's deadline with trees in hand. Before the first
    complete tree, the caller's deadline is what ends the wait.
    """
    loop = asyncio.get_running_loop()
    first: Tree | None = None
    newest: Tree | None = None  # of either kind
    latest: Tree | None = None
    changing = False  # an InitialDump arrived after `latest`
    unsettled = False  # notifications arrived during the read
    settle_by = 0.0
    pair_by = 0.0  # when a pair under way is given up on
    hard_by = 0.0
    while True:
        if latest is None:
            selector, args = await queue.get()
        else:
            deadline = min(pair_by, hard_by) if changing else settle_by
            remaining = deadline - loop.time()
            if remaining <= 0:
                return latest, first or latest, newest or latest
            quiet = UNSETTLED_QUIET_S if unsettled else QUIET_S
            try:
                selector, args = await asyncio.wait_for(
                    queue.get(), remaining if changing else min(quiet, remaining))
            except TimeoutError:
                return latest, first or latest, newest or latest
        if selector != PUSH_SELECTOR or not args or not isinstance(args[0], bytes):
            continue
        try:
            tree = parse_tree(args[0])
        except AxTreeError:
            unsettled = True
            continue
        first = first or tree
        newest = tree
        if held is not None:
            held.update(first=first, newest=newest)
        if tree.dump_type == "AdditionalData":
            if latest is None:
                # The wait for a newer tree, counted from the first complete
                # one; long enough to see the app replaced when the screen is
                # in flux.
                settle_by = loop.time() + max(SETTLE_MAX_S, UNSETTLED_QUIET_S)
                hard_by = loop.time() + SETTLE_HARD_S
            elif loop.time() >= settle_by:
                # A later stage, completed after the settle window: it gets the
                # quiet spell any complete tree gets, or the next stage -- or a
                # dying app's replacement (#669) -- is never seen (#677 review).
                quiet = UNSETTLED_QUIET_S if unsettled else QUIET_S
                settle_by = min(hard_by, loop.time() + quiet)
            latest, changing = tree, False
            if held is not None:
                held["latest"] = latest
        elif latest is not None:
            changing = True
            # Its pair gets the time a tree this size takes to build, and
            # never less than the settle window already allowed.
            pair_by = max(settle_by, loop.time() + _pair_budget(tree))


async def _app_alive(rsd: Any, lib: SimpleNamespace, pid: int) -> bool | None:
    """Whether a process is still running: True, False, or None when the
    device could not be asked."""
    try:
        async with lib.AppService(rsd) as apps:
            processes = await asyncio.wait_for(apps.list_processes(), CLOSE_S)
    except Exception:
        return None
    return any(p.get("processIdentifier") == pid for p in processes if isinstance(p, dict))


async def _subscribe(lib: SimpleNamespace, address: Any, conn: SimpleNamespace) -> asyncio.Queue:
    """Connect and subscribe to the tree push, filling `conn.rsd` and
    `conn.provider` as each opens, so the caller's cleanup closes whatever
    did."""
    conn.rsd = lib.RemoteServiceDiscoveryService(address)
    await conn.rsd.connect()
    conn.provider = lib.RemoteAX(conn.rsd)
    await conn.provider.connect()
    queue: asyncio.Queue = asyncio.Queue()
    conn.provider.dtx.ctx["control_dispatch_queue"] = queue
    await conn.provider.dtx._ctrl_channel.invoke(
        "clientNeedsAccessibility:", True, expects_reply=False)
    return queue


async def _first_push(queue: asyncio.Queue, within: float) -> bool:
    """Whether anything arrives on `queue` within `within` s, without taking it."""
    end = asyncio.get_running_loop().time() + within
    while queue.empty():
        if asyncio.get_running_loop().time() >= end:
            return False
        await asyncio.sleep(0.05)
    return True


async def _subscribed(uuid: str, lib: SimpleNamespace, address: Any,
                      conn: SimpleNamespace) -> asyncio.Queue:
    """A subscription that has pushed something: made once more on a fresh
    connection when the first stays silent (#684). The second is not watched:
    the caller's own deadline ends it."""
    queue = await _subscribe(lib, address, conn)
    if await _first_push(queue, FIRST_PUSH_S):
        return queue
    logger.info("[coredevice] no push from %s in %gs: subscribing on a fresh "
                "connection (#684)", uuid[:8], FIRST_PUSH_S)
    await _close(conn.provider, conn.rsd)
    conn.rsd = conn.provider = None
    return await _subscribe(lib, address, conn)


async def _close(provider: Any, rsd: Any) -> None:
    """Unsubscribe and close, each step bounded and its failure swallowed.

    Outside the read's deadline on purpose: a deadline that fired here would
    leave the provider open and report a tree already read as never read. NO
    ends the subscription on the device's side; it is sent even when the read
    failed, since the device may still be pushing.
    """
    steps: list = []
    if provider is not None:
        steps.append(lambda: provider.dtx._ctrl_channel.invoke(
            "clientNeedsAccessibility:", False, expects_reply=False))
        steps.append(provider.close)
    if rsd is not None:
        steps.append(rsd.close)
    for step in steps:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(step(), CLOSE_S)


async def _with_settled_tree(uuid: str, timeout: float, what: str,
                             use: Callable[[Any, Tree, Tree, Tree], Any]) -> Any:
    """Connect, subscribe, take the settled tree, and hand it to `use` with the
    provider, then close: `use(provider, tree, first, newest)`'s answer.

    A DeviceError from `use` passes through; a timeout says it may have
    happened (`what` names it); anything else from pymobiledevice3 is a
    DeviceError.
    """
    from server.device.performance import _device_address

    lib = _pmd3()
    conn = SimpleNamespace(rsd=None, provider=None)
    try:
        async with asyncio.timeout(timeout):
            try:
                address = await _device_address(uuid)
                queue = await _subscribed(uuid, lib, address, conn)
                tree, first, newest = await _newest_tree(queue, {})
                if (first.app_pid is not None and newest.app_pid != first.app_pid
                        and await _app_alive(conn.rsd, lib, first.app_pid) is False):
                    # The read killed the app (#669): said, as read_tree says
                    # it, so the caller can stop reading this device's push.
                    raise AppExitedDuringRead(first.elements[0]["AXLabel"], first.app_pid)
                return await use(conn.provider, tree, first, newest)
            finally:
                await _close(conn.provider, conn.rsd)
    except DeviceError:
        raise
    except TimeoutError as e:
        raise DeviceError(f"{what} on {uuid[:8]} timed out after {timeout:g}s",
                          tool="coredevice") from e
    except Exception as e:  # pymobiledevice3 raises its own hierarchy
        raise DeviceError(f"accessibility service on {uuid[:8]} failed: {e!r}",
                          tool="coredevice") from e


async def set_value(uuid: str, match: Callable[[dict], bool], value: str, *,
                    timeout: float = READ_TIMEOUT_S) -> None:
    """Set the value of the first on-screen element `match` accepts, through
    the accessibility service (#664).

    The text is set, not typed: the app's editing callbacks do not run for it
    (`UITextFieldDelegate` did not fire, measured on an iPhone 11). Nothing
    here says whether it took; the caller reads the field back. Raises
    DeviceError when the element is not on screen or the request cannot be
    sent. A timeout may come after the request went: it says so.
    """
    async def use(provider: Any, tree: Tree, first: Tree, newest: Tree) -> None:
        if first.app_pid is not None and newest.app_pid != first.app_pid:
            # Switched, or killed by the read (#669): whatever is on screen
            # now is not the field's app (#682 review).
            raise DeviceError(
                f"the app on {uuid[:8]}'s screen changed while the field was "
                "looked up; nothing was set", tool="coredevice")
        _refuse_bare(tree.elements, uuid)
        found = [i for i, e in enumerate(tree.elements) if match(e)]
        if len(found) != 1:
            # More than one is refused, never the first taken: the text would
            # go to whichever came first (#682 review).
            raise DeviceError(
                f"the field to set is not in {uuid[:8]}'s accessibility tree"
                if not found else
                f"{len(found)} elements on {uuid[:8]}'s screen match the field; "
                "nothing was set", tool="coredevice")
        ref = tree.refs[found[0]] if found[0] < len(tree.refs) else None
        if ref is None:
            raise DeviceError(
                f"{uuid[:8]}'s accessibility tree does not say how to address "
                "the field", tool="coredevice")
        await provider.dtx._ctrl_channel.invoke(
            "processDataFromHost:", set_value_request(*ref, value), expects_reply=True)

    await _with_settled_tree(
        uuid, timeout, "setting a value (it may or may not have been set)", use)


async def front_app(uuid: str, *, timeout: float = READ_TIMEOUT_S) -> tuple[int | None, str]:
    """The pid and name of the app on screen, from the tree push (#664).

    Measured on an iPhone 11: the pid is the one `devicectl process launch`
    reports for it. SpringBoard's name is a single space.
    """
    async def use(provider: Any, tree: Tree, first: Tree, newest: Tree) -> tuple:
        return tree.app_pid, tree.elements[0].get("AXLabel") or ""

    return await _with_settled_tree(uuid, timeout, "reading the app on screen", use)


async def read_tree(uuid: str, *, timeout: float = READ_TIMEOUT_S) -> list[dict]:
    """The on-screen accessibility tree of a physical iPhone, as `describe_all`
    returns it. Raises DeviceError when it cannot be read."""
    from server.device.performance import _device_address

    lib = _pmd3()
    started = time.monotonic()
    conn = SimpleNamespace(rsd=None, provider=None)
    held: dict = {}
    deadline = asyncio.timeout(timeout)
    try:
        try:
            # The address lookup is inside the deadline too: on a cache miss it
            # asks devicectl, and a read must not outlast the time it was given.
            async with deadline:
                address = await _device_address(uuid)
                queue = await _subscribed(uuid, lib, address, conn)
                tree, first, newest = await _newest_tree(queue, held)
                elements = tree.elements
                # The app changed under the read: switched, or killed by it.
                # Only the process list can tell those apart. "Could not ask"
                # is not taken as dead: the read stands, as it would have.
                if (first.app_pid is not None and newest.app_pid != first.app_pid
                        and await _app_alive(conn.rsd, lib, first.app_pid) is False):
                    raise AppExitedDuringRead(first.elements[0]["AXLabel"], first.app_pid)
        finally:
            await _close(conn.provider, conn.rsd)
    except TimeoutError as e:
        if deadline.expired() and held.get("latest") is not None:
            # The caller's deadline (a short `source_timeout`) ended a wait for
            # a newer tree, with a complete one in hand: that is the answer,
            # not "sent no complete tree", which would be false (#677 review).
            # The crash check is skipped -- its process query has no time left
            # -- and "could not ask" is not taken as dead, as above.
            logger.info("[coredevice] tree of %s answered at the %gs deadline",
                        uuid[:8], timeout)
            return _refuse_bare(held["latest"].elements, uuid)
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
    return _refuse_bare(elements, uuid)


class CoreDeviceAxReader:
    """`describe_all` for a physical iPhone, from the tree push.

    A reader of whole screens only: element lookups, taps and every other
    action stay with WDA in this phase, so it is chosen for unfiltered reads
    alone (`DeviceController._native_ui_elements`).
    """

    TOOL_NAME = "coredevice"

    async def describe_all(self, udid: str, *, snapshot_depth: int | None = None,
                           source_timeout: float | None = None,
                           probe: bool = True) -> list[dict]:
        # snapshot_depth and probe are WDA's and sim-bridge's knobs; the push
        # is the whole on-screen tree, so there is nothing for them to change.
        timeout = source_timeout or READ_TIMEOUT_S
        try:
            return await read_tree(udid, timeout=timeout)
        except BareAppTree:
            await asyncio.sleep(BARE_CONFIRM_S)
            return await read_tree(udid, timeout=timeout)
