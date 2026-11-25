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
import contextvars
import fcntl
import logging
import os
import plistlib
import re
import stat
import time
import weakref
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
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

#: How many times a read subscribes again, on a fresh connection, when the
#: device stops pushing before a complete tree (#684); and how long after
#: its newest InitialDump that is taken to have happened: this many of the
#: pair's own budget (`_pair_budget`), and never less than STALL_MIN_S. A
#: false start costs a fresh subscription, about 2 s.
RESUBSCRIBES = 2
STALL_PAIRS = 2.0
STALL_MIN_S = 2.5
#: And never less than this many times what the device took to send that
#: InitialDump, timed from the subscription or the InitialDump before it:
#: its own pace, where the per-element budget is a guess (#769). A map with
#: ~119 pins sent its first InitialDump 4.4 s after subscribing and its
#: complete tree 7.9 s after the newest, three of three; the per-element
#: budget gave up at 5.6 s and resubscribed, which started the phone over,
#: and no read of it ever finished. #684's stall sends its InitialDumps
#: about 1 s apart, so it is still given up on in STALL_MIN_S.
STALL_PACE = 2.5
#: The slowest pace seen on the subscription is what counts: a quick
#: InitialDump after a slow one does not shorten the wait the slow one earned,
#: or a map that sent two close together stalled as before (review). #684's
#: stall sends every InitialDump quickly, so its pace stays small. Capped, so
#: a late first push cannot hold off recovering a real stall for the whole
#: deadline (review): the ~119-pin map's 2.5 x 4.4 s is under it.
STALL_PACE_MAX_S = 12.0
#: A screen still building at the deadline with at least this many elements
#: in its InitialDump is too busy to read in time, said as such and not read
#: through WDA instead, whose /source timed out on the same map (#769).
BUSY_ELEMENTS = 100

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
#: The push types a password field as a plain TextField. Measured on an
#: iPhone 11 across a screen of 42 elements, five of them text fields: this
#: trait bit is on the password field, empty or filled, and on nothing else.
#: Not attribute 28, which looked like a marker on that screen and is True on
#: Safari's page text and links too (#690 review).
TRAIT_SECURE_TEXT = 0x1000000

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

#: How long a crash report takes to appear after its crash, at most, and how
#: often to look. ReportCrash writes it once the process has gone.
CRASH_REPORT_WAIT_S = 6.0
CRASH_REPORT_POLL_S = 1.5
#: How far before the read that saw the app a report may be dated: a report's
#: time is to the second, in the phone's local time.
CRASH_REPORT_SLACK_S = 2.0
#: How many apps a device's reads remember, and how many reads in a row may
#: fail to say whether one is still running before it is let go.
REMEMBERED_APPS = 3
UNKNOWN_READS = 3
#: The frame every report of #669's crash has: UIKit inserting nil there.
CRASH_669_FRAME = "_accessibilityWindowSections"

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

#: How long opening the accessibility service -- its DTX connection, which
#: may start `axauditd`, and the capability handshake -- may take before the
#: daemon is taken to have stopped answering (#734). Measured on an iPhone
#: 12: 10-20 ms with the daemon running, ten in ten; a read just after a
#: restart, which starts it, about 0.4 s more than a warm one.
#: pymobiledevice3's own bound is 15 s, which every read paid while the
#: daemon was wedged. Generous, so a slow daemon is not killed for a dead
#: one: a wedge is cleared when first found, so this is paid about once each.
HANDSHAKE_S = 5.0

#: How often a phone's `axauditd` may be restarted to recover it (#734): a
#: phone whose daemon does not come back is not killed in a loop.
RESTART_EVERY_S = 60.0

#: How long a phone whose push could not be recovered is not asked again
#: (#734): a call in that time fails or falls back at once, rather than each
#: paying the handshake's wait.
DOWN_S = 30.0

#: The daemon behind `SERVICE`.
DAEMON = "axauditd"

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


class TurnNotTaken(DeviceError):
    """A read whose deadline ran out while another read of the same phone had
    its turn: the phone was never asked (#734).

    Not a failure of the push, which is busy rather than broken, so nothing
    falls back to WDA over it: a fallback started WDA beside a healthy push,
    the overlap #734 removed, for every short read queued behind a long one
    (review on #736). The caller only needs more time.
    """


class ScreenTooBusy(DeviceError):
    """The screen has too many elements for the phone to send its complete
    tree within the read's deadline (#769).

    Measured on an iPhone 12, a map: 26 elements answered in 2.6 s, 128 in
    13 s, and 415 sent no complete tree in 45 s. Not a failure of the push,
    and not one WDA recovers from -- its /source timed out on the 128 -- so
    nothing falls back over it; the caller can make the screen smaller."""

    def __init__(self, uuid: str, elements: int, timeout: float) -> None:
        super().__init__(
            f"{uuid[:8]}'s screen has {elements} elements, and the phone did not finish "
            f"sending its accessibility tree within {timeout:g}s: a screen this busy -- "
            "a map with many pins, a very long list -- takes the phone longer to "
            "describe than the read allows. Make it smaller and read again: zoom a map "
            "in until 10-20 pins show, or filter or scroll a list. It was not read "
            "through WDA, which cannot read a screen this busy either.",
            tool="coredevice")
        self.elements = elements


class PushNotAnswering(DeviceError):
    """The phone's accessibility daemon no longer completes a new connection's
    handshake, and could not be recovered (#734).

    Measured on an iPhone 12 on iOS 27.2.1: under overlapping subscriptions
    `axauditd` stopped answering new connections and stayed that way for
    10 minutes and more, through Home and an app relaunch, while a screenshot
    over the same tunnel still worked. It ignores SIGTERM; a SIGKILL brought
    it back at once.
    """


class PushStalled(DeviceError):
    """The device stopped pushing before a complete tree (#684).

    Measured on an iPhone 11 after a scroll sweep's swipe, 3 to 6 reads in
    50: an InitialDump, a second about 1.1 s later, then nothing -- the
    channel open and its reader running at 8 s. The read waited out its
    deadline, 20 s, and the fallback started WDA. A subscription on a fresh
    connection answered in about 2 s, three times in three.
    """


class AppExitedAfterRead(AppExitedDuringRead):
    """The app a read saw crashed after the read had answered (#669).

    The crash can land after the tree has arrived and the read has settled --
    measured on an iPhone 11: the probe's whole Widgets tree at 4.0 s, the app
    gone at 4.3 s -- so the read that caused it saw nothing wrong, and every
    read after it showed SpringBoard as if that were the screen. The next read
    tells, from the process list and the phone's crash report for that pid.
    A subclass, so every guard against a read's crash covers it.
    """

    def __init__(self, app: str, pid: int) -> None:
        DeviceError.__init__(
            self,
            f"{app or 'the app'} (pid {pid}) crashed after a read of its screen through "
            "the accessibility service, so the screen it was on is gone: an iOS 27 bug "
            "in UIKit's accessibility code crashes some apps under this read (#669)",
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
            "role_description": _role_description(etype, traits),
            "AXLabel": _text(bundle.get(ATTR_LABEL)) or "",
            "AXUniqueId": _text(bundle.get(ATTR_IDENTIFIER)),
            "AXValue": _value(bundle.get(ATTR_VALUE)),
            "frame": _rect(bundle.get(ATTR_FRAME)),
            "enabled": not traits & TRAIT_NOT_ENABLED,
            "help": None,
            "custom_actions": _custom_actions(bundle.get(ATTR_CUSTOM_ACTIONS)),
        })
        refs.append(_ref(response.get("associatedTranslationObject")))
    if not elements:
        raise AxTreeError(f"{dump_type or 'tree dump'} holds no elements")
    return Tree(dump_type, elements, app_pid, refs)


def _value(value: Any) -> str | None:
    """An element's value as text, whole numbers included.

    A tab-bar item's value is an integer in the push -- 1 on the tab in front,
    0 on the others -- and so is a segmented control's segment, where WDA
    reports "1" for the selected one; read as text only, it was dropped, and
    every landmark with `selected: true` missed (#664). Measured on an iPhone
    11: the probe's tabs and segments and Clock's tabs, and nothing else on
    those screens or Settings'. A bool is a number to the archive too. A
    slider's float (50.0) is left out, as before: WDA says "50%".
    """
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    return _text(value)


def _role_description(etype: str, traits: int) -> str:
    """A search field's or a password field's subrole, as the simulator's
    tree reports them -- which is where quern's own rules look for them."""
    if etype == "TextField" and traits & TRAIT_SECURE_TEXT:
        # Unmarked, a password field's dots read as a rewrite of the typed
        # text, and as the keyboard's doing, so its value got set (#664).
        return "AXSecureTextField"
    return "AXSearchField" if traits & TRAIT_SEARCH_FIELD else ""


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
#: An attribute's value, and the element at a point: request types measured
#: on an iPhone 11 (#664), the hit-test's shape from macOS's own translator.
REQUEST_ATTRIBUTE = 2
REQUEST_HIT_TEST = 6
#: How long a hit-test may take, whole: connect, subscribe, ask, and read the
#: element's attributes. Measured at about 0.2 s; a slow answer is no answer.
POINT_TIMEOUT_S = 3.0
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


def _request(request_type: int, pid: int, object_id: int, *, attribute: int = 0,
             point: tuple[float, float] | None = None) -> bytes:
    """A keyed archive of an AXPTranslatorRequest: an attribute read of one
    element, or -- with `point` -- the hit-test at that point.

    The hit-test is addressed to pid 0, as macOS's translator addresses it:
    addressed to the app, the phone answered error 7 (#664).
    """
    uid = plistlib.UID
    objects: list = ["$null", None]

    def add(obj: Any) -> plistlib.UID:
        objects.append(obj)
        return uid(len(objects) - 1)

    params = uid(0)
    if point is not None:
        nsvalue = add({"$class": None, "NS.pointval": None, "NS.special": 1})
        objects[nsvalue.data]["NS.pointval"] = add(f"{{{point[0]:g}, {point[1]:g}}}")
        objects[nsvalue.data]["$class"] = add({"$classes": ["NSValue", "NSObject"],
                                               "$classname": "NSValue"})
        params = add({"$class": add({"$classes": ["NSDictionary", "NSObject"],
                                     "$classname": "NSDictionary"}),
                      "NS.keys": [add("point"), add("displayId")],
                      "NS.objects": [nsvalue, add(1)]})
    translation = add({"$class": add({"$classes": ["AXPTranslationObject", "NSObject"],
                                      "$classname": "AXPTranslationObject"}),
                       "bridgeDelegateToken": uid(0), "didPopuldateAppInfo": True,
                       "isApplicationElement": point is not None,
                       "objectID": add(object_id), "pid": pid, "rawElementData": uid(0)})
    objects[1] = {"$class": add({"$classes": ["AXPTranslatorRequest", "NSObject"],
                                 "$classname": "AXPTranslatorRequest"}),
                  "actionType": 0, "attributeType": attribute, "clientType": CLIENT_TYPE,
                  "parameters": params, "requestType": request_type,
                  "translation": translation}
    return plistlib.dumps({"$archiver": "NSKeyedArchiver", "$objects": objects,
                           "$top": {"root": uid(1)}, "$version": 100000},
                          fmt=plistlib.FMT_BINARY)


def _frame_text(value: Any) -> dict[str, float] | None:
    """A frame from an attribute read's "{{x, y}, {w, h}}"."""
    m = re.fullmatch(r"\{\{([-\d.e]+), ([-\d.e]+)\}, \{([-\d.e]+), ([-\d.e]+)\}\}",
                     value.strip()) if isinstance(value, str) else None
    if not m:
        return None
    x, y, w, h = (float(g) for g in m.groups())
    return {"x": x, "y": y, "width": w, "height": h}


async def _answer(provider: Any, queue: asyncio.Queue, payload: bytes,
                  request_type: int, attribute: int = 0) -> dict | None:
    """Send one request and take its answer from the pushes: the one carrying
    this request type and attribute. Tree pushes in between are passed over."""
    await provider.dtx._ctrl_channel.invoke("processDataFromHost:", payload,
                                            expects_reply=True)
    while True:
        selector, args = await queue.get()
        if selector != PUSH_SELECTOR or not args or not isinstance(args[0], bytes):
            continue
        try:
            answer = _unarchive(args[0])
        except Exception:  # noqa: BLE001 -- not an answer this request can use
            continue
        if (isinstance(answer, dict) and answer.get("associatedRequestType") == request_type
                and (answer.get("attribute") or 0) == attribute):
            return answer


async def element_at(uuid: str, x: float, y: float, *,
                     timeout: float = POINT_TIMEOUT_S) -> dict | None:
    """The element a touch at (x, y) would reach, from the phone's own
    hit-test (#664): `describe_point`'s shape, or None when it cannot be told.

    It answers with the deepest element there -- a button's title text, not
    the button. Measured on an iPhone 11: an overlay over the middle of a
    button answered "Overlay", the button's own centre "Arm overlay"; the
    whole ask took about 0.2 s. It needs a subscription, though no tree: on
    an unsubscribed connection nothing answered.
    """
    from server.device.performance import _device_address

    lib = _pmd3()
    conn = SimpleNamespace(rsd=None, provider=None)
    turn = _Turn(uuid)
    try:
        async with asyncio.timeout(timeout):
            _refuse_if_down(uuid)
            await _device_address(uuid)    # the id the turn is named by: `_Turn`
            await turn.take()
            # Again: the read ahead of this one may have marked it down while
            # this one waited (CodeRabbit on #735).
            _refuse_if_down(uuid)
            queue = await _subscribe(lib, await _device_address(uuid), conn, uuid)
            hit = await _answer(conn.provider, queue,
                                _request(REQUEST_HIT_TEST, 0, 0, point=(x, y)),
                                REQUEST_HIT_TEST)
            found = hit.get("resultData") if hit and not hit.get("error") else None
            if not (isinstance(found, dict) and isinstance(found.get("pid"), int)
                    and isinstance(found.get("objectID"), int)):
                return None
            values: dict[int, Any] = {}
            for attribute in (ATTR_LABEL, ATTR_IDENTIFIER, ATTR_FRAME, ATTR_TRAITS):
                answer = await _answer(
                    conn.provider, queue,
                    _request(REQUEST_ATTRIBUTE, found["pid"], found["objectID"],
                             attribute=attribute),
                    REQUEST_ATTRIBUTE, attribute)
                values[attribute] = answer.get("resultData") if answer else None
    except Exception as e:  # noqa: BLE001 -- could not tell is no obstruction
        logger.info("[coredevice] hit-test at (%g, %g) on %s: %r", x, y, uuid[:8], e)
        return None
    finally:
        # Outside the deadline, as every close is: cancelled inside it, the
        # close stopped part-way and left the connection open (CodeRabbit on
        # #705). `_close` bounds each step itself.
        try:
            await _close(conn.provider, conn.rsd)
        finally:
            # Given back even when the close is cancelled: `_close` swallows
            # Exception, not CancelledError, and a turn kept is every later
            # read of this phone failing until the server restarts (review).
            turn.give_back()
    traits = values[ATTR_TRAITS] if isinstance(values[ATTR_TRAITS], int) else 0
    return {"type": _element_type(traits, is_root=False),
            "AXLabel": _text(values[ATTR_LABEL]) or "",
            "AXUniqueId": _text(values[ATTR_IDENTIFIER]),
            "frame": _frame_text(values[ATTR_FRAME])}


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
                       held: dict | None = None,
                       since: float | None = None) -> tuple[Tree, Tree, Tree]:
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
    stall_by = None  # before a complete tree: when a pair under way is given up on
    # When the device was last asked or last answered: the subscription, then
    # each InitialDump. What it took to answer is its pace (#769).
    paced_from = since if since is not None else loop.time()
    pace = 0.0
    while True:
        if latest is None and stall_by is None:
            selector, args = await queue.get()
        elif latest is None:
            try:
                selector, args = await asyncio.wait_for(
                    queue.get(), max(stall_by - loop.time(), 0.0))
            except TimeoutError:
                raise PushStalled("the device stopped pushing before a complete tree",
                                  tool="coredevice") from None
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
        elif latest is None:
            # An InitialDump with no complete tree yet: its pair, or the
            # newest one's, is waited for -- not for ever (#684), and not less
            # than the device's own pace allows (#769).
            now = loop.time()
            pace, paced_from = max(pace, now - paced_from), now
            stall_by = now + max(STALL_PAIRS * _pair_budget(tree), STALL_MIN_S,
                                 min(STALL_PACE * pace, STALL_PACE_MAX_S))
        elif latest is not None:
            changing = True
            # Its pair gets the time a tree this size takes to build, and
            # never less than the settle window already allowed.
            pair_by = max(settle_by, loop.time() + _pair_budget(tree))


async def _app_alive(rsd: Any, lib: SimpleNamespace, pid: int) -> bool | None:
    """Whether a process is still running: True, False, or None when the
    device could not be asked.

    The in-read crash check's: only the listing is bounded, and the
    service's connect has what is left of the read's own deadline. Bounding
    the connect too hid a crash under a read behind a slow connect (review).
    """
    try:
        async with lib.AppService(rsd) as apps:
            processes = await asyncio.wait_for(apps.list_processes(), CLOSE_S)
    except Exception:
        return None
    return any(p.get("processIdentifier") == pid for p in processes if isinstance(p, dict))


async def _alive(rsd: Any, lib: SimpleNamespace, pids: list[int]) -> dict[int, bool | None]:
    """Whether each process is still running, from one process listing: None
    for every one when the device could not be asked. Bounded as a whole --
    the service's connect too: it asks about earlier reads' apps, and a hang
    there would hold every read to its deadline. A None is asked again."""
    async def listing() -> list:
        async with lib.AppService(rsd) as apps:
            return await apps.list_processes()
    try:
        processes = await asyncio.wait_for(listing(), CLOSE_S)
    except Exception:
        return dict.fromkeys(pids)
    running = {p.get("processIdentifier") for p in processes if isinstance(p, dict)}
    return {pid: pid in running for pid in pids}


def _turn_dir() -> Path:
    """Where every quern server this user runs takes a phone's turn across
    servers: one per user, whatever a server's QUERN_STATE_DIR or HOME, so a
    scratch or branch server and the user's own share it. QUERN_AX_LOCK_DIR
    moves it, for tests."""
    return Path(os.environ.get("QUERN_AX_LOCK_DIR") or f"/tmp/quern-{os.getuid()}")


#: How often a turn held by another quern server is asked for again.
TURN_POLL_S = 0.05

#: When a lock file that could not be used was last logged, by path.
_skip_logged: dict[str, float] = {}


async def _lock_identity(uuid: str) -> str:
    """The id every server names a phone's turn file by: its tunnel UDID,
    which either of the phone's ids resolves to through tunneld and
    devicectl, the same for every process on the Mac. Named by the id each
    server was given, a hardware UDID at one server and a CoreDevice UUID at
    another took two files for one phone (CodeRabbit on #736). The read's
    own address lookup resolves it next, so this costs no more. Unresolved,
    the id given: with no tunnel there is no read to overlap."""
    from server.device.ios.tunneld import resolve_tunnel_udid

    try:
        return await resolve_tunnel_udid(uuid) or uuid
    except Exception:  # noqa: BLE001 -- could not tell is the id given
        return uuid


def _open_own(path: Path) -> int:
    """`path` opened, made if missing, without following a symlink, and only
    if it is a regular file this user owns. OSError otherwise."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
            raise PermissionError(f"{path} is not a file this user owns: not using it")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _another_server_waiting(want: int) -> bool:
    """Whether another server holds the `.want` lock: waiting for this phone's
    turn. An answer it cannot get is no."""
    try:
        fcntl.flock(want, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError:
        return False
    fcntl.flock(want, fcntl.LOCK_UN)
    return False


class _Turn:
    """A phone's one subscription at a time (#734).

    Every failure measured needed more than one subscription open at once:
    two readers alone failed 14% of reads -- stalled pushes, streams the
    phone reset, handshakes unanswered -- and with app switches and reads
    abandoned on top, wedged `axauditd` within minutes, twice in two runs.
    One reader abandoning connections failed none of 146.

    Taken inside the caller's deadline, so waiting for another read counts
    against it, and given back only after the connection is closed, which
    happens outside it: the next subscription never overlaps a closing one.

    Two locks, in order: this server's, then a `flock` every quern server
    this user runs shares (`_turn_dir`), since a second server -- a scratch
    or branch one, which is how live tests run -- read the same phone at the
    same time (review). The kernel lets go of a `flock` when its process
    dies, so a server that crashed holds nothing. A lock file that cannot be
    made or locked, for any reason but another server holding it, is logged
    and this server goes on with its own lock alone: a lock quern cannot take
    must not stop every read.

    Only the turn is shared: the restart limit and the down window are each
    server's own, so two servers may each restart the daemon within a minute.

    The `flock` is named by the phone's tunnel UDID (`_lock_identity`), so
    every caller looks the phone's address up before taking the turn: that
    resolves the id, and a read that cannot resolve it has no tunnel to
    connect through. Resolved only inside the turn, an id that failed to
    resolve named the file by the id given, and the address lookup after it
    could recover tunneld and connect under that other name (CodeRabbit on
    #736). The address is looked up again once the turn is taken, since the
    tunnel may have changed while it waited.
    """

    #: Per event loop, by the loop itself: keyed by `id()`, a loop that has
    #: gone could hand its locks to a new one given its id (review). Each lock
    #: is kept only while a turn holds it: a lock that has been waited on
    #: holds its loop, so kept for good it kept every finished loop alive
    #: through the weak key (CodeRabbit on #736). One nobody holds has no
    #: one waiting on it, so a new one in its place loses nothing.
    _locks: weakref.WeakKeyDictionary[
        asyncio.AbstractEventLoop, weakref.WeakValueDictionary[str, asyncio.Lock]] = (
        weakref.WeakKeyDictionary())

    def __init__(self, uuid: str) -> None:
        loop = asyncio.get_running_loop()
        self._uuid = uuid
        locks = self._locks.setdefault(loop, weakref.WeakValueDictionary())
        self._lock = locks.setdefault(uuid, asyncio.Lock())
        self._held = False
        self._fd: int | None = None
        #: When this read began waiting for the turn; None until it did.
        self._asked: float | None = None
        #: Seconds spent waiting for the turn, once it was taken; None until.
        self.waited: float | None = None

    @property
    def taken(self) -> bool:
        return self.waited is not None

    @property
    def missed(self) -> bool:
        """Waited for the turn and never got it: the phone was not asked.
        Not a deadline that ran out before the wait began -- in the address
        lookup, which is the push's own trouble (review on #736)."""
        return self._asked is not None and self.waited is None

    async def take(self) -> None:
        self._asked = time.monotonic()
        await self._lock.acquire()
        try:
            await self._take_across_servers()
        except BaseException:
            self._lock.release()
            raise
        self._held = True
        self.waited = time.monotonic() - (self._asked or 0.0)

    async def _take_across_servers(self) -> None:
        from server.screenshots import private_dir

        name = re.sub(r"[^A-Za-z0-9-]", "_", await _lock_identity(self._uuid))
        directory = _turn_dir()
        path = directory / f"coredevice-ax-{name}.lock"
        try:
            # /tmp is shared: a directory another user made first, or a
            # symlink at the lock's name, was theirs to hold or swap (review).
            directory.parent.mkdir(parents=True, exist_ok=True)
            private_dir(directory, directory)
            fd: int | None = _open_own(path)
        except OSError as e:
            self._skipped(path, e)
            return
        want: int | None = None
        try:
            with contextlib.suppress(OSError):
                want = _open_own(path.with_suffix(".want"))
            # Another server waiting: give way first. A server handing its
            # turn from task to task took it back before the other's next
            # poll, every time -- measured, 0 of 3 tries got through (review).
            if want is not None and _another_server_waiting(want):
                await asyncio.sleep(2 * TURN_POLL_S)
            announced = False
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self._fd, fd = fd, None
                    return
                except BlockingIOError:
                    if want is not None and not announced:
                        # Held while this one waits, so the holder gives way.
                        with contextlib.suppress(OSError):
                            fcntl.flock(want, fcntl.LOCK_SH | fcntl.LOCK_NB)
                            announced = True
                    await asyncio.sleep(TURN_POLL_S)    # another server's read
                except OSError as e:
                    self._skipped(path, e)
                    return
        finally:
            if fd is not None:
                os.close(fd)
            if want is not None:
                os.close(want)                          # and with it, the announcement

    def _skipped(self, path: Path, e: OSError) -> None:
        """No turn across servers this time: this server's lock alone. Said
        on the answer, and logged at most once a minute a file (review)."""
        now = time.monotonic()
        if now - _skip_logged.get(str(path), -60.0) >= 60.0:
            _skip_logged[str(path)] = now
            logger.warning("[coredevice] no turn across servers for %s (%s): %r",
                           self._uuid[:8], path, e)
        if (notes := _NOTES.get()) is not None:
            notes.append(f"quern could not take {self._uuid[:8]}'s turn across quern "
                         f"servers ({e}), so another quern server may have read it at "
                         "the same time (#734).")

    def give_back(self) -> None:
        if not self._held:
            return
        self._held = False
        try:
            if self._fd is not None:
                fd, self._fd = self._fd, None
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)
        finally:
            self._lock.release()

    def timed_out(self, timeout: float, what: str) -> str:
        """Why a deadline ended, as the caller should hear it: the phone never
        asked when the turn did not come, or how much of it went waiting."""
        if self.missed:
            return (f"waited {timeout:g}s for another accessibility read of "
                    f"{self._uuid[:8]} to finish; the phone was not asked")
        if self.waited and self.waited >= 0.5:
            return (f"{what} ({self.waited:.1f}s of the {timeout:g}s waiting for another "
                    "read of this phone)")
        return what


#: When each phone's `axauditd` was last restarted, and until when a phone
#: whose push could not be recovered is skipped (time.monotonic()).
_restarted_at: dict[str, float] = {}
_down_until: dict[str, float] = {}

#: What the push did that the current request's answer should say (#734):
#: a daemon restart, a turn it could not take across servers. Per request,
#: not per phone: kept per phone, a restart made by a call whose answer never
#: says so -- typing's correction, a front-app check -- was claimed by
#: whichever answer on that phone asked next, minutes later (review). A note
#: with no list to go in is logged and nothing else.
_NOTES: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar(
    "coredevice_notes", default=None)


def collect_notes() -> list[str]:
    """A fresh list that this task's push calls from here on, and the tasks
    they start, add a sentence to for what the answer should say."""
    restarts: list[str] = []
    _NOTES.set(restarts)
    return restarts


def _refuse_if_down(uuid: str) -> None:
    left = _down_until.get(uuid, 0.0) - time.monotonic()
    if left > 0:
        raise PushNotAnswering(
            f"the accessibility daemon on {uuid[:8]} is not answering and could not be "
            f"restarted; the push is not asked again for {left:.0f}s", tool="coredevice")


def _handshake_failed(e: BaseException) -> bool:
    """Whether opening the service went unanswered: quern's own bound, or
    pymobiledevice3's "DTX capability handshake timed out". Not a reader
    cancelled mid-handshake (a ConnectionResetError that also says
    "handshake"), which is a connection ended, not a daemon wedged (review)."""
    return isinstance(e, TimeoutError) or (
        isinstance(e, ConnectionError) and "capability handshake timed out" in str(e))


def _executable(process: dict) -> str:
    """A process listing entry's executable path or URL: CoreDevice gives it
    as ``{"relative": "file:///..."}``."""
    url = process.get("executableURL")
    if isinstance(url, dict):
        url = url.get("relative")
    return url if isinstance(url, str) else ""


#: What `_restart_daemon` did.
RESTARTED, TOO_SOON, NOT_RESTARTED = "restarted", "too soon", "not restarted"


async def _restart_daemon(uuid: str, rsd: Any, lib: SimpleNamespace) -> str:
    """SIGKILL the phone's `axauditd`, which launchd starts again on the next
    connection: RESTARTED; TOO_SOON within RESTART_EVERY_S of the last; or
    NOT_RESTARTED when the phone refused, or ran none."""
    import signal

    now = time.monotonic()
    if now - _restarted_at.get(uuid, -RESTART_EVERY_S) < RESTART_EVERY_S:
        return TOO_SOON
    _restarted_at[uuid] = now
    try:
        async with lib.AppService(rsd) as apps:
            processes = await asyncio.wait_for(apps.list_processes(), CLOSE_S)
            pids = [p["processIdentifier"] for p in processes
                    if isinstance(p, dict) and isinstance(p.get("processIdentifier"), int)
                    and _executable(p).endswith("/" + DAEMON)]
            for pid in pids:
                await asyncio.wait_for(apps.send_signal_to_process(pid, signal.SIGKILL), CLOSE_S)
    except Exception as e:  # noqa: BLE001 -- could not restart is reported as such
        logger.warning("[coredevice] could not restart %s on %s: %r", DAEMON, uuid[:8], e)
        return NOT_RESTARTED
    if not pids:
        logger.warning("[coredevice] no %s running on %s to restart", DAEMON, uuid[:8])
        return NOT_RESTARTED
    logger.warning("[coredevice] %s on %s stopped answering; restarted it (pid %s)",
                   DAEMON, uuid[:8], ", ".join(map(str, pids)))
    return RESTARTED


async def _connect_service(uuid: str, lib: SimpleNamespace, conn: SimpleNamespace, *,
                           spare: bool = False) -> None:
    """Open the accessibility service on `conn.rsd`, recovering a daemon that
    no longer completes the handshake (#734): one restart, then one more try.
    Raises PushNotAnswering when that fails.

    The phone is skipped for DOWN_S only when it cannot be recovered: the
    restart refused, or the fresh daemon not answering either. Not when the
    last restart was too recent -- that read fails, the next tries again --
    and never over a `spare` connection, which opens beside a first that is
    merely silent (#684) and may yet answer: measured, marking the phone down
    over one skipped every read for 26 s while the daemon was fine.
    """
    conn.provider = lib.RemoteAX(conn.rsd)
    started = time.monotonic()
    try:
        await asyncio.wait_for(conn.provider.connect(), HANDSHAKE_S)
        return
    except (TimeoutError, ConnectionError) as e:
        if not _handshake_failed(e):
            raise
        first = e
    # Logged before anything is done about it: a slow daemon taken for a dead
    # one would show here as a time just over the bound (review).
    logger.warning("[coredevice] the accessibility service on %s did not open in %.1fs: %r",
                   uuid[:8], time.monotonic() - started, first)
    await _close(conn.provider, None)
    conn.provider = None
    if spare:
        # Killing the daemon would end the first subscription too.
        raise PushNotAnswering(
            f"a second connection to the accessibility daemon on {uuid[:8]} did not "
            f"answer within {HANDSHAKE_S:g}s ({first!r})", tool="coredevice") from first
    outcome = await _restart_daemon(uuid, conn.rsd, lib)
    if outcome == RESTARTED:
        conn.provider = lib.RemoteAX(conn.rsd)
        try:
            await asyncio.wait_for(conn.provider.connect(), HANDSHAKE_S)
        except (TimeoutError, ConnectionError) as e:
            if not _handshake_failed(e):
                raise
        else:
            if (restarts := _NOTES.get()) is not None:
                restarts.append(
                    f"The accessibility daemon on {uuid[:8]} had stopped answering a "
                    "new connection, so quern restarted it (#734).")
            return
    if outcome == TOO_SOON:
        raise PushNotAnswering(
            f"the accessibility daemon on {uuid[:8]} did not answer a connection within "
            f"{HANDSHAKE_S:g}s ({first!r}); it was restarted less than "
            f"{RESTART_EVERY_S:g}s ago, so not again yet", tool="coredevice") from first
    _down_until[uuid] = time.monotonic() + DOWN_S
    raise PushNotAnswering(
        f"the accessibility daemon on {uuid[:8]} did not answer a connection within "
        f"{HANDSHAKE_S:g}s ({first!r}) and could not be restarted; the push is not "
        f"asked again for {DOWN_S:g}s", tool="coredevice") from first


async def _subscribe(lib: SimpleNamespace, address: Any, conn: SimpleNamespace,
                     uuid: str, *, spare: bool = False) -> asyncio.Queue:
    """Connect and subscribe to the tree push, filling `conn.rsd` and
    `conn.provider` as each opens, so the caller's cleanup closes whatever
    did. `spare`: opened beside a subscription still open (#684)."""
    conn.rsd = lib.RemoteServiceDiscoveryService(address)
    await conn.rsd.connect()
    await _connect_service(uuid, lib, conn, spare=spare)
    queue: asyncio.Queue = asyncio.Queue()
    conn.provider.dtx.ctx["control_dispatch_queue"] = queue
    await conn.provider.dtx._ctrl_channel.invoke(
        "clientNeedsAccessibility:", True, expects_reply=False)
    # The device's pace is timed from here (#769).
    conn.subscribed_at = asyncio.get_running_loop().time()
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
    """A subscription that has pushed something (#684).

    When the first stays silent for FIRST_PUSH_S, a second is opened on a
    fresh connection beside it, and whichever pushes first is kept; the other
    is closed. The first is not closed to make room: a heavy screen whose
    first push is merely late still wins, rather than being thrown away for a
    read that starts over (#692 review). Neither wait is bounded here: the
    caller's deadline ends both, and `conn` -- which the caller closes -- holds
    the first until the second has won, the spare is closed on any way out.
    """
    queue = await _subscribe(lib, address, conn, uuid)
    if await _first_push(queue, FIRST_PUSH_S):
        return queue
    logger.info("[coredevice] no push from %s in %gs: subscribing on a fresh "
                "connection beside it (#684)", uuid[:8], FIRST_PUSH_S)
    spare = SimpleNamespace(rsd=None, provider=None)
    try:
        try:
            spare_queue = await _subscribe(lib, address, spare, uuid, spare=True)
        except Exception as e:  # noqa: BLE001 - the first may still answer
            logger.info("[coredevice] the fresh connection to %s failed (%r); "
                        "waiting on the first", uuid[:8], e)
            await _close(spare.provider, spare.rsd)
            return queue
        while queue.empty() and spare_queue.empty():
            await asyncio.sleep(0.05)
    except BaseException:
        await asyncio.shield(_close(spare.provider, spare.rsd))
        raise
    # The loser is closed after the winner's first push arrived, and that
    # close is the Mac's time, not the device's: counted as its pace, a slow
    # close could reach the cap and turn a stall into "too busy" (CodeRabbit
    # on #772). So the winner's start is moved on by what the close took.
    loop = asyncio.get_running_loop()
    if not queue.empty():
        closing = loop.time()
        await _close(spare.provider, spare.rsd)
        _shift_start(conn, loop.time() - closing)
        return queue
    closing = loop.time()
    await _close(conn.provider, conn.rsd)
    conn.rsd, conn.provider = spare.rsd, spare.provider
    conn.subscribed_at = getattr(spare, "subscribed_at", None)
    _shift_start(conn, loop.time() - closing)
    return spare_queue


def _shift_start(conn: SimpleNamespace, by: float) -> None:
    """Move a subscription's recorded start on by `by` seconds, if it has one."""
    if getattr(conn, "subscribed_at", None) is not None:
        conn.subscribed_at += by


async def _settled(uuid: str, lib: SimpleNamespace, address: Any, conn: SimpleNamespace,
                   held: dict) -> tuple[Tree, Tree, Tree]:
    """Subscribe and take the settled tree, subscribing again on a fresh
    connection when the device stops pushing first (#684). `conn` holds
    whichever connection is open, for the caller to close; the caller's
    deadline bounds every attempt.

    The first tree is the first attempt's: a caller compares it with the
    newest to see the app change under the read (#669), and an app that
    stalled one attempt and died before the next must not drop out of that
    comparison (CodeRabbit on #702)."""
    first_seen: Tree | None = None
    for attempt in range(RESUBSCRIBES + 1):
        queue = await _subscribed(uuid, lib, address, conn)
        try:
            tree, first, newest = await _newest_tree(
                queue, held, since=getattr(conn, "subscribed_at", None))
            if first_seen is not None:
                first = first_seen
                held["first"] = first_seen
            return tree, first, newest
        except PushStalled:
            first_seen = first_seen or held.get("first")
            # A stalled attempt's trees are not the next attempt's: a big
            # InitialDump left in `held` turned a push that then stayed
            # silent into "too busy" (review). The first is kept, above.
            held.pop("newest", None)
            if attempt == RESUBSCRIBES:
                raise
            logger.info("[coredevice] %s stopped pushing before a complete tree: "
                        "subscribing again on a fresh connection (#684)", uuid[:8])
            await _close(conn.provider, conn.rsd)
            conn.rsd = conn.provider = None
    raise AssertionError("unreachable")


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
    # Every step runs even when the caller is cancelled during one: the turn
    # is given back after this, and a step skipped could leave a transport
    # open beside the next subscription (CodeRabbit on #735). The
    # cancellation is raised again once they have.
    cancelled: BaseException | None = None
    for step in steps:
        try:
            await asyncio.wait_for(step(), CLOSE_S)
        except asyncio.CancelledError as e:
            cancelled = e
        except Exception:  # noqa: BLE001 -- a close that fails changes nothing
            pass
    if cancelled is not None:
        raise cancelled


async def _with_settled_tree(uuid: str, timeout: float, what: str,
                             use: Callable[[Any, Tree, Tree, Tree], Any], *,
                             unasked: str = "") -> Any:
    """Connect, subscribe, take the settled tree, and hand it to `use` with the
    provider, then close: `use(provider, tree, first, newest)`'s answer.

    A DeviceError from `use` passes through; a timeout says it may have
    happened (`what` names it); anything else from pymobiledevice3 is a
    DeviceError.
    """
    from server.device.performance import _device_address

    lib = _pmd3()
    conn = SimpleNamespace(rsd=None, provider=None)
    turn = _Turn(uuid)
    try:
        try:
            async with asyncio.timeout(timeout):
                _refuse_if_down(uuid)
                await _device_address(uuid)    # the id the turn is named by: `_Turn`
                await turn.take()
                _refuse_if_down(uuid)    # again, after waiting (CodeRabbit on #735)
                address = await _device_address(uuid)
                tree, first, newest = await _settled(uuid, lib, address, conn, {})
                if (first.app_pid is not None and newest.app_pid != first.app_pid
                        and await _app_alive(conn.rsd, lib, first.app_pid) is False):
                    # The read killed the app (#669): said, as read_tree says
                    # it, so the caller can stop reading this device's push.
                    raise AppExitedDuringRead(first.elements[0]["AXLabel"], first.app_pid)
                return await use(conn.provider, tree, first, newest)
        finally:
            # Outside the deadline, as read_tree's is: the deadline firing in
            # the close left it half-done (review on #734).
            try:
                await _close(conn.provider, conn.rsd)
            finally:
                turn.give_back()
    except DeviceError:
        raise
    except TimeoutError as e:
        if turn.missed:
            # `what` may say it might have happened; it did not (review).
            raise TurnNotTaken(turn.timed_out(timeout, "") + (f"; {unasked}" if unasked else ""),
                               tool="coredevice") from e
        raise DeviceError(turn.timed_out(
            timeout, f"{what} on {uuid[:8]} timed out after {timeout:g}s"),
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
        uuid, timeout, "setting a value (it may or may not have been set)", use,
        unasked="nothing was set")


async def front_app(uuid: str, *, timeout: float = READ_TIMEOUT_S) -> tuple[int | None, str]:
    """The pid and name of the app on screen, from the tree push (#664).

    Measured on an iPhone 11: the pid is the one `devicectl process launch`
    reports for it. SpringBoard's name is a single space.
    """
    async def use(provider: Any, tree: Tree, first: Tree, newest: Tree) -> tuple:
        return tree.app_pid, tree.elements[0].get("AXLabel") or ""

    return await _with_settled_tree(uuid, timeout, "reading the app on screen", use)


async def read_tree(uuid: str, *, timeout: float = READ_TIMEOUT_S,
                    previous_pids: tuple[int, ...] = (),
                    seen: dict | None = None) -> list[dict]:
    """The on-screen accessibility tree of a physical iPhone, as `describe_all`
    returns it. Raises DeviceError when it cannot be read.

    `seen`, when given, is filled with the app in front (`pid`, `app`), and
    with whether each of `previous_pids` other than it is still running
    (`alive`: pid -> True, False, or None when the phone could not be asked),
    asked on this read's own connection."""
    from server.device.performance import _device_address

    lib = _pmd3()
    started = time.monotonic()
    conn = SimpleNamespace(rsd=None, provider=None)
    held: dict = {}
    deadline = asyncio.timeout(timeout)
    turn = _Turn(uuid)
    try:
        try:
            # The address lookup is inside the deadline too: on a cache miss it
            # asks devicectl, and a read must not outlast the time it was given.
            async with deadline:
                _refuse_if_down(uuid)
                await _device_address(uuid)    # the id the turn is named by: `_Turn`
                await turn.take()
                _refuse_if_down(uuid)    # again, after waiting (CodeRabbit on #735)
                address = await _device_address(uuid)
                tree, first, newest = await _settled(uuid, lib, address, conn, held)
                elements = tree.elements
                # The app changed under the read: switched, or killed by it.
                # Only the process list can tell those apart. "Could not ask"
                # is not taken as dead: the read stands, as it would have.
                if first.app_pid is not None and newest.app_pid != first.app_pid:
                    first_alive = await _app_alive(conn.rsd, lib, first.app_pid)
                    if first_alive is False:
                        raise AppExitedDuringRead(first.elements[0]["AXLabel"], first.app_pid)
                    if first_alive is None and seen is not None:
                        # Could not tell: remembered, so the next read asks.
                        seen["unasked"] = (first.app_pid, first.elements[0]["AXLabel"])
                if seen is not None:
                    seen["pid"] = newest.app_pid
                    seen["app"] = newest.elements[0]["AXLabel"]
                    others = [p for p in previous_pids if p != newest.app_pid]
                    if others:
                        seen["alive"] = await _alive(conn.rsd, lib, others)
        finally:
            try:
                await _close(conn.provider, conn.rsd)
            finally:
                turn.give_back()
    except TimeoutError as e:
        if deadline.expired() and held.get("latest") is not None:
            # The caller's deadline (a short `source_timeout`) ended a wait for
            # a newer tree, with a complete one in hand: that is the answer,
            # not "sent no complete tree", which would be false (#677 review).
            # The crash check is skipped -- its process query has no time left
            # -- and "could not ask" is not taken as dead, as above.
            logger.info("[coredevice] tree of %s answered at the %gs deadline",
                        uuid[:8], timeout)
            latest = held["latest"]
            if seen is not None and "pid" not in seen:
                # Still remembered as the app this read saw, so a crash it
                # causes is looked for at the next read (CodeRabbit on #701).
                seen["pid"] = latest.app_pid
                seen["app"] = latest.elements[0]["AXLabel"] if latest.elements else ""
            return _refuse_bare(latest.elements, uuid)
        newest = held.get("newest")
        if (deadline.expired() and not turn.missed and newest is not None
                and newest.dump_type == "InitialDump"
                and len(newest.elements) >= BUSY_ELEMENTS):
            # Still describing a screen this big at the deadline: too busy to
            # read in time, not broken, and not a read for WDA (#769).
            raise ScreenTooBusy(uuid, len(newest.elements), timeout) from None
        if deadline.expired():
            # The phone was never asked when the deadline came while this
            # read waited its turn: said as such, not as the phone's fault,
            # which sent an agent looking at the app (review).
            raise (TurnNotTaken if turn.missed else DeviceError)(turn.timed_out(
                timeout, f"{uuid[:8]} sent no complete accessibility tree within {timeout:g}s"),
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


#: `(udid, since, app) -> [(pid, report text)]`, as
#: `CoreDeviceAxReader.crash_reports`: the phone's newest reports, `app`'s
#: first, or None when the phone cannot be asked at all.
CrashReports = Callable[[str, float, str], Awaitable[list[tuple[int | None, str]] | None]]


@dataclass
class _InFront:
    """An app a read saw in front, still to be accounted for."""

    pid: int
    app: str
    #: When the read that saw it began (time.time()).
    when: float
    #: Reads since that could not tell whether it is still running.
    unknown: int = 0


class CoreDeviceAxReader:
    """`describe_all` for a physical iPhone, from the tree push.

    A reader of whole screens only: element lookups, taps and every other
    action stay with WDA in this phase, so it is chosen for unfiltered reads
    alone (`DeviceController._native_ui_elements`).
    """

    TOOL_NAME = "coredevice"

    async def describe_point(self, udid: str, x: float, y: float) -> dict | None:
        """The element a touch at (x, y) would reach (`element_at`)."""
        return await element_at(udid, x, y)

    def __init__(self) -> None:
        #: The apps each device's reads saw in front, newest first: the last
        #: one, and any earlier one not yet known to be running or gone.
        self._front: dict[str, list[_InFront]] = {}
        #: Bumped by forget_front, so a read in flight does not put back what
        #: quern has just ended.
        self._forgotten: dict[str, int] = {}
        #: The phone's newest crash reports (`CrashReports`). Set by the
        #: controller, which knows the phone's hardware UDID; None reads none,
        #: and no crash after a read is ever claimed.
        self.crash_reports: CrashReports | None = None

    def forget_front(self, udid: str) -> None:
        """quern ended or replaced an app itself (terminate, install,
        uninstall): a pid gone at the next read is no crash."""
        self._front.pop(udid, None)
        self._forgotten[udid] = self._forgotten.get(udid, 0) + 1

    def note_front(self, udid: str, pid: int | None, app: str, when: float) -> None:
        """A push read made elsewhere saw this app in front -- launch_app's
        check -- so a crash that read causes is looked for too. Added to what
        is remembered, never over it: an earlier app's crash may still be
        unasked (review)."""
        if isinstance(pid, int):
            others = [e for e in self._front.get(udid, []) if e.pid != pid]
            self._front[udid] = [_InFront(pid, app, when), *others][:REMEMBERED_APPS]

    async def describe_all(self, udid: str, *, snapshot_depth: int | None = None,
                           source_timeout: float | None = None,
                           probe: bool = True) -> list[dict]:
        # snapshot_depth and probe are WDA's and sim-bridge's knobs; the push
        # is the whole on-screen tree, so there is nothing for them to change.
        timeout = source_timeout or READ_TIMEOUT_S
        remembered = list(self._front.get(udid, []))
        generation = self._forgotten.get(udid, 0)
        seen: dict = {}
        kwargs = {"previous_pids": tuple(e.pid for e in remembered), "seen": seen}
        # The window a report may be dated in starts when the read did: a
        # crash during its close or its process query predates the return.
        started = time.time()
        try:
            elements = await read_tree(udid, timeout=timeout, **kwargs)
        except BareAppTree:
            await asyncio.sleep(BARE_CONFIRM_S)
            # The rejected attempt's app is not the one the retry answers
            # with; what it could not tell about (`unasked`) still stands
            # (CodeRabbit on #701).
            seen.pop("pid", None)
            seen.pop("app", None)
            elements = await read_tree(udid, timeout=timeout, **kwargs)
        pid, app = seen.get("pid"), seen.get("app") or ""
        if not isinstance(pid, int):
            return elements
        alive = seen.get("alive") or {}
        still_open: list[_InFront] = []
        gone: list[_InFront] = []
        for e in remembered:
            if e.pid == pid:
                continue
            state = alive.get(e.pid)
            if state is None:
                # Not known to be running or gone -- the phone could not be
                # asked, or the deadline came first -- so asked again next
                # read, a few times, rather than the question being lost.
                if e.unknown + 1 < UNKNOWN_READS:
                    still_open.append(_InFront(e.pid, e.app, e.when, e.unknown + 1))
            elif state is False and e.app != app:
                # Another app in front, and this one gone: a crash after the
                # read that saw it, if the phone's report says so (#669). The
                # same app under a new pid was relaunched, which is not the
                # case asked about -- a crash and a relaunch before the next
                # read are not told apart.
                gone.append(e)
        unasked = seen.get("unasked")
        if unasked and unasked[0] != pid and all(e.pid != unasked[0] for e in still_open):
            # The app that changed under this read, not known to be gone.
            still_open.insert(0, _InFront(unasked[0], unasked[1] or "", started, 1))
        crashed = None
        for e in gone:
            if await self._crashed_under_a_read(udid, e):
                crashed = e
                break
        # Committed only now: a check cancelled midway (the caller left)
        # leaves the question for the next read. An app noted meanwhile, by
        # a launch check, is kept.
        # By identity, not pid: a launch check may have noted again a pid
        # this read started with, and that newer record is the one to keep
        # (CodeRabbit on #701).
        noted = [e for e in self._front.get(udid, [])
                 if not any(e is r for r in remembered) and e.pid != pid]
        still_open = [e for e in still_open if e.pid not in {n.pid for n in noted}]
        if self._forgotten.get(udid, 0) != generation:
            # quern ended an app meanwhile: no earlier question stands, and
            # no crash found for one is claimed (CodeRabbit on #701).
            still_open = []
            crashed = None
        self._front[udid] = [_InFront(pid, app, started), *noted, *still_open][:REMEMBERED_APPS]
        if crashed is not None:
            raise AppExitedAfterRead(crashed.app, crashed.pid)
        return elements

    async def _crashed_under_a_read(self, udid: str, previous: _InFront) -> bool:
        """Whether the phone has a report of #669's crash for that pid.

        Waits a few seconds for it: ReportCrash writes the report after the
        process has gone. Anything else -- no report, another crash, a report
        that cannot be read -- is not claimed as #669.
        """
        if self.crash_reports is None:
            return False
        pid, when = previous.pid, previous.when
        # The whole wait is bounded, a slow listing or pull included.
        deadline = time.monotonic() + CRASH_REPORT_WAIT_S
        while True:
            left = deadline - time.monotonic()
            try:
                reports = await asyncio.wait_for(
                    self.crash_reports(udid, when - CRASH_REPORT_SLACK_S, previous.app),
                    max(left, 0.1))
            except Exception as e:  # noqa: BLE001 -- not this time: try again in the time left
                logger.info("[coredevice] crash reports of %s unread: %s", udid[:8], e)
                reports = []
            if reports is None:
                return False      # the phone cannot be asked (no USB): never will be
            for report_pid, text in reports:
                if report_pid == pid:
                    return CRASH_669_FRAME in text
            if time.monotonic() + CRASH_REPORT_POLL_S >= deadline:
                return False
            await asyncio.sleep(CRASH_REPORT_POLL_S)
