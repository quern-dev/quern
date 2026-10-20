"""Checking that a documented navigation tap arrived (#491).

`tap_element` reports `ok` once the backend accepts the tap. Through WDA that
means `/wda/tap` answered 200, and the app can still ignore it: measured on the
probe app, a More-tab tap returned `ok` and the next read still showed the Text
tab, and during #475 up to half of WDA's drags were dropped with the same 200.
An agent then acts against a screen it never reached, and the failure surfaces
calls later as an unrelated `not_found`.

A knowledge base already says where a tap should lead: each screen file's
`leads_to` lists `tap_element` calls and the screen each reaches. This module
turns those into edges, decides whether a tap is one of them, and checks the
screen afterwards.

The rules, each of which exists to keep the check from crying wolf:

* **Only a documented edge is checked**, and only from the screen it is
  documented on. The screen before the tap has to be identified exactly;
  unidentified or ambiguous means no claim either way.
* **The tapped element has to match the edge**, not only the call. A call
  asks for a label; the edge is about the element that was actually tapped.
* **Arrival is polled for, until a deadline.** A push or sheet still animating
  is waited out rather than read half-way, and the success case returns on the
  first read that identifies the target.
* **A conditional edge never reports a miss.** It exists only in some states,
  and the state may not have held.
* **An unidentified screen afterwards is not a miss.** It may be the target
  read mid-transition, or a screen the knowledge base does not know.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from server.device.element_types import related_type_names
from server.device.ui_elements import find_element
from server.models import NavEdge, UIElement

#: The `tap_element` arguments an edge can carry. Anything else in an action
#: (`value=`, `snapshot_depth=`) does not change which element is tapped.
SELECTOR_KEYS = ("label", "identifier", "label_contains", "label_prefix", "element_type")

#: How long to wait for a documented navigation to arrive. A tab switch reads
#: as arrived on the first read; a push or sheet animates for about 0.35-0.5s.
#: The full wait is paid only by a tap that did not navigate.
NAVIGATION_TIMEOUT = 3.0

#: The longest wait an edge or a call can ask for.
MAX_TIMEOUT = 60.0

#: A miss is claimed only from a read that *began* at least this long after
#: the tap. A read is a snapshot of the moment it starts, and a sim-bridge read
#: takes 1.6-3.9s (measured, mostly probing containers for the tab and
#: navigation bars the landmarks name): one begun as the tap landed can return
#: the old screen after the new one has arrived. A push animates for ~0.35s.
MISS_MIN_AGE = 1.0

#: However slow the reads, the wait ends here. Past it with no read late
#: enough to claim a miss, the answer is "could not tell".
HARD_CAP = 10.0

#: Between reads while waiting. Reads themselves take 0.1-0.5s through WDA, so
#: this only stops a fast backend from spinning.
_POLL_GAP = 0.15

_ACTION_RE = re.compile(r"^\s*(?:mcp__[\w-]+__)?tap_element\b(?P<args>.*)$", re.S)
_ARG_RE = re.compile(
    r"""(?P<key>\w+)\s*[=:]\s*(?:"(?P<dq>[^"]*)"|'(?P<sq>[^']*)'|(?P<bare>[^\s,)]+))"""
)


def parse_action(action: object) -> dict[str, str] | None:
    """The selector of a `tap_element` action, or None for any other action.

    Accepts the template's form, `tap_element label="More" element_type="Button"`,
    and the call form, `tap_element(label="More", element_type="Button")`.
    Only the selector keys are kept. None when the action is not a
    `tap_element` call, or names neither a label nor an identifier -- an edge
    that cannot be matched to a tap is not an edge.
    """
    if not isinstance(action, str):
        return None
    m = _ACTION_RE.match(action)
    if m is None:
        return None
    selector: dict[str, str] = {}
    for arg in _ARG_RE.finditer(m.group("args")):
        key = arg.group("key")
        if key not in SELECTOR_KEYS:
            continue
        val = next(
            (v for v in (arg.group("dq"), arg.group("sq"), arg.group("bare")) if v is not None),
            "",
        )
        if val:
            selector[key] = val
    if not any(selector.get(k) for k in ("label", "identifier", "label_contains", "label_prefix")):
        return None
    return selector


def link_target(raw: object) -> str | None:
    """The screen a `leads_to` entry names: `[[screens/more]]` -> `more`.

    Also accepts a bare name, a `.md` suffix and an Obsidian alias
    (`[[screens/more|More]]`). None when nothing usable is there.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if text.startswith("[[") and text.endswith("]]"):
        text = text[2:-2]
    text = text.split("|", 1)[0].strip()
    if text.startswith("screens/"):
        text = text[len("screens/"):]
    if text.endswith(".md"):
        text = text[:-3]
    text = text.strip()
    if not text or "..." in text:
        return None
    return text


def parse_leads_to(raw: object) -> tuple[list[NavEdge], list[dict]]:
    """Edges from a screen's `leads_to`, and the entries that could not be used.

    Each unusable entry is reported with a reason rather than dropped in
    silence: an edge the author believes is checked, and is not, is the same
    false all-clear this whole check exists to remove. Targets are as written;
    `resolve_targets` maps file names to screen names once every file is read.
    """
    edges: list[NavEdge] = []
    unusable: list[dict] = []
    if raw is None:
        return edges, unusable
    if not isinstance(raw, list):
        return edges, [{"entry": raw, "reason": "leads_to is not a list"}]
    for entry in raw:
        if not isinstance(entry, dict):
            unusable.append({"entry": entry, "reason": "not a mapping"})
            continue
        target = link_target(entry.get("screen"))
        if target is None:
            unusable.append({"entry": entry, "reason": "names no screen"})
            continue
        selector = parse_action(entry.get("action"))
        if selector is None:
            unusable.append({
                "entry": entry,
                "reason": "the action is not a tap_element call with a label or identifier",
            })
            continue
        condition = entry.get("condition")
        raw_timeout = entry.get("timeout")
        timeout = None
        if raw_timeout is not None:
            if (isinstance(raw_timeout, (int, float)) and not isinstance(raw_timeout, bool)
                    and 0 < raw_timeout <= MAX_TIMEOUT):
                timeout = float(raw_timeout)
            else:
                # Reported, and the edge kept at the default wait: a typo must
                # not silently make a slow screen read as a dropped tap.
                unusable.append({
                    "entry": entry,
                    "reason": f"timeout must be a number of seconds, 0 to {MAX_TIMEOUT:g}; "
                              f"the edge is checked with the default {NAVIGATION_TIMEOUT:g}s",
                })
        edges.append(NavEdge(
            target=target, **selector,
            condition=str(condition) if condition not in (None, "") else None,
            timeout=timeout,
        ))
    return edges, unusable


def _label_overlap(
    call: tuple[str | None, str | None, str | None],
    edge: tuple[str | None, str | None, str | None],
) -> bool:
    """Whether two label selectors -- (exact, contains, prefix) -- could pick
    the same element. Exact against exact is equality, as `tap_element`
    matches it: `label="OK"` can never tap the element `label="Book"` names.
    Only a substring or prefix selector widens it."""
    ce, cc, cp = (t.lower() if t else None for t in call)
    ee, ec, ep = (t.lower() if t else None for t in edge)
    if ce and ee:
        return ce == ee
    if ce:
        return bool((ec and ec in ce) or (ep and ce.startswith(ep)))
    if ee:
        return bool((cc and cc in ee) or (cp and ee.startswith(cp)))
    # Two widened selectors: they could overlap whenever either text holds
    # the other. Loose by design; the tapped element is matched exactly.
    a = [t for t in (cc, cp) if t]
    b = [t for t in (ec, ep) if t]
    return any(x in y or y in x for x in a for y in b)


def could_match_call(
    edge: NavEdge,
    *,
    label: str | None = None,
    identifier: str | None = None,
    label_contains: str | None = None,
    label_prefix: str | None = None,
    element_type: str | None = None,
) -> bool:
    """Whether a call could tap the element this edge describes.

    Decides only whether to pay for identifying the screen before the tap;
    the tapped element is matched exactly afterwards (`matches_tapped`). So it
    errs towards yes, but needs the call and the edge to name the element the
    same way: the same identifier, or label selectors that can pick the same
    element. A call by identifier against an edge written by label is not
    checked, because nothing before the tap connects the two.
    """
    if edge.identifier and identifier and edge.identifier != identifier:
        return False
    if element_type and edge.element_type:
        if edge.element_type.lower() not in {
            t.lower() for t in related_type_names(element_type)
        }:
            return False
    if edge.identifier and identifier:
        return True
    return _label_overlap(
        (label, label_contains, label_prefix),
        (edge.label, edge.label_contains, edge.label_prefix),
    )


def matches_tapped(edge: NavEdge, tapped: dict) -> bool:
    """Whether the element `tap_element` reports tapping is this edge's.

    The same rules `tap_element` used to find it -- exact label, case
    insensitive; exact identifier; the type widened across backends -- applied
    to the one element that was tapped.
    """
    element = UIElement(
        type=str(tapped.get("type") or ""),
        label=str(tapped.get("label") or ""),
        identifier=tapped.get("identifier"),
    )
    return bool(find_element(
        [element],
        label=edge.label, label_contains=edge.label_contains,
        label_prefix=edge.label_prefix, identifier=edge.identifier,
        element_type=edge.element_type,
    ))


@dataclass
class Identified:
    """One identification: the screen, the app whose set it is in, and how."""

    #: "exact", "ambiguous" or "none".
    confidence: str
    screen: str | None = None
    app: str | None = None
    candidates: list[str] | None = None


#: Reads the device's whole screen and identifies it. Raises on a failed read.
IdentifyFn = Callable[[], Awaitable[Identified]]


@dataclass
class Plan:
    """What to check after the tap: the screen before, and the edges from it."""

    before: Identified
    edges: list[NavEdge]


def plan_for(
    before: Identified,
    edges_from: Callable[[str, str], Sequence[NavEdge]],
    call: dict,
) -> Plan | None:
    """The edges a tap from `before` could take, or None for no check.

    `edges_from(app, screen)` returns that screen's edges. No claim is made
    unless the screen before was identified exactly.
    """
    if before.confidence != "exact" or before.screen is None or before.app is None:
        return None
    edges = [e for e in edges_from(before.app, before.screen) if could_match_call(e, **call)]
    if not edges:
        return None
    return Plan(before=before, edges=edges)


async def verify(
    plan: Plan,
    tapped: dict,
    identify: IdentifyFn,
    *,
    timeout: float | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> dict | None:
    """Wait for the tap to arrive where its edge says, and report the result.

    None when the tapped element is not the edge's: the call looked like a
    documented tap, but the element it reached is not the one the edge names.
    Otherwise the `navigation` object for the response.
    """
    edges = [e for e in plan.edges if matches_tapped(e, tapped)]
    if not edges:
        return None
    if timeout is None:
        # The slowest documented edge sets the wait: a screen that loads over
        # the network is documented with its own `timeout:`.
        timeout = max(
            (e.timeout for e in edges if e.timeout is not None),
            default=NAVIGATION_TIMEOUT,
        )
    report_timeout = timeout
    targets = sorted({e.target for e in edges})
    firm = sorted({e.target for e in edges if e.condition is None})
    expected: str | list[str] = targets[0] if len(targets) == 1 else targets
    report: dict = {"expected": expected, "from": plan.before.screen}
    if not firm:
        report["conditional"] = True

    start = clock()
    last: Identified | None = None
    #: The latest identification from a read begun late enough to claim a miss.
    settled: Identified | None = None
    failures = 0
    while True:
        began = clock() - start
        try:
            last = await identify()
        except Exception:  # noqa: BLE001 -- a failed read is "could not tell", never a miss
            failures += 1
        else:
            if (
                last.confidence == "exact"
                and last.app == plan.before.app
                and last.screen in targets
            ):
                report["arrived"] = True
                if len(targets) > 1:
                    report["on"] = last.screen
                report["waited_ms"] = round((clock() - start) * 1000)
                return report
            if began >= MISS_MIN_AGE:
                settled = last
        elapsed = clock() - start
        if elapsed >= timeout and (
            settled is not None or elapsed >= max(HARD_CAP, timeout + MISS_MIN_AGE)
        ):
            break
        await sleep(_POLL_GAP)

    report["waited_ms"] = round((clock() - start) * 1000)
    if last is None:
        report["arrived"] = None
        report["reason"] = "read_failed"
        report["read_failures"] = failures
        return report
    if settled is None:
        # Every read that answered began too soon after the tap to show
        # where it went -- a slow backend, or the late reads failing.
        report["arrived"] = None
        report["reason"] = "no_settled_read"
        return report
    last = settled
    if last.confidence != "exact" or last.app != plan.before.app:
        # The target half-drawn, or a screen the knowledge base does not know.
        report["arrived"] = None
        report["reason"] = "ambiguous" if last.confidence == "ambiguous" else "unidentified"
        if last.candidates:
            report["candidates"] = last.candidates
        return report
    if not firm:
        # Only conditional edges matched, and their condition may not have held.
        report["arrived"] = None
        report["reason"] = "conditional"
        report["on"] = last.screen
        return report
    report["arrived"] = False
    report["on"] = last.screen
    report["stayed"] = last.screen == plan.before.screen
    report["timeout_s"] = report_timeout
    return report


def warning_for(navigation: dict) -> str | None:
    """The top-level warning for a missed navigation, or None.

    Never "tap again". A screen that stays put while it works -- a sign-in
    waiting on the network -- reads exactly like a dropped tap, and repeating
    a submit or a purchase is worse than the miss this reports.
    """
    if navigation.get("arrived") is not False:
        return None
    expected = navigation["expected"]
    want = expected if isinstance(expected, str) else " or ".join(expected)
    if navigation.get("stayed"):
        where = f"the screen is still {navigation['on']!r}"
    else:
        where = f"it is on {navigation['on']!r} instead"
    return (
        f"The tap was delivered, but the knowledge base says it leads to {want!r} "
        f"and {where} after {navigation['waited_ms']}ms. Either the app dropped "
        "the tap, or what it started (a request, a slow screen) has not finished. "
        "Do not act as if the new screen had been reached: check the screen first. "
        "Repeat the tap only if repeating it is harmless -- not for a submit, a "
        "purchase or a send. If this screen legitimately takes longer, give the "
        "edge a `timeout:` in the knowledge base or pass navigation_timeout."
    )
