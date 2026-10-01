"""Element types that name the same thing on different iOS backends.

A simulator is read through the accessibility tree (sim-bridge, idb); a
physical device, and a simulator after `start_driver`, through WebDriverAgent,
which reports XCUITest's types. The same tab-bar item is a `RadioButton` in one
and a `Button` in the other, so a landmark or an `element_type` filter written
against one backend silently misses on the other.

`type` is left as each backend reported it (ADR 1 in
`docs/proposals/landmark-conventions.md`); the equivalence lives here, in the
comparison, and every caller that compares a wanted type with an element's type
goes through `type_matches`.

How far a type is widened depends on what else pins the element down (ADR 2):

- an identifier: the same **family** -- the identifier is what makes generic
  types safe, because the element carrying it *is* the target whatever a
  backend calls it;
- a label, and no identifier: a **safe pair** only;
- neither: exact, as before.

Every entry is measured, never inferred: all twelve QuernProbe screens read on
both backends on an iOS 18.6 simulator (spec §1). A pair is added the same way
or not at all -- the first assumption made during this work, that WDA sends an
`XCUIElementType` prefix, was wrong on contact.
"""

from __future__ import annotations

from typing import Literal

TypeRule = Literal["exact", "label", "identifier"]

#: Accessibility tree ↔ XCUITest, equated on a label or an identifier. Each is
#: one element under two names, and the label lands on the same element in
#: both trees.
SAFE_PAIRS: tuple[tuple[str, str], ...] = (
    ("RadioButton", "Button"),        # tab-bar items, 50 seen
    ("Heading", "StaticText"),        # screen titles, 10
    ("TextField", "SecureTextField"),  # password field
    ("CheckBox", "Switch"),           # UISwitch
    ("TabGroup", "SegmentedControl"),  # UISegmentedControl
    ("TextArea", "TextView"),         # UITextView
    ("Slider", "PageIndicator"),      # UIPageControl
)

#: Equated only on an identifier. The accessibility tree's generic types stand
#: for many specific ones -- `Group` was the tab bar, the navigation bar, the
#: toolbar, the table *and* the collection view -- so equating them by label or
#: by type alone would let any group match a tab-bar landmark.
#:
#: Deliberately absent: `Button`↔`StaticText` by label (spec §1.3). A More-list
#: row is one `Button` in the accessibility tree and a `Cell` holding a
#: `StaticText` to XCUITest, so the label match lands on the text *inside* the
#: row. It is in the rows family, where an identifier makes it safe.
FAMILIES: dict[str, tuple[str, ...]] = {
    "containers": (
        "Group", "Other", "TabBar", "NavigationBar", "Toolbar", "Table",
        "CollectionView",
    ),
    # Three rows differing only in their accessory came back Button,
    # StaticText, Button; XCUITest called all three Cell.
    "rows": ("Button", "StaticText", "Cell"),
    "indicators": (
        "GenericElement", "ProgressIndicator", "ActivityIndicator", "ColorWell",
        "StaticText",
    ),
    "headers": ("Heading", "Other", "StaticText"),
}

#: Reported by XCUITest with no labelled or identified counterpart in the
#: accessibility tree at all, so no matching rule can make a landmark on one
#: portable. `NavigationBar` is also in the containers family, from a frame
#: match; it still carried neither a label nor an identifier there.
NO_PORTABLE_COUNTERPART: frozenset[str] = frozenset(
    {"searchfield", "datepicker", "picker", "navigationbar"},
)


def _lower_pairs() -> dict[str, frozenset[str]]:
    partners: dict[str, set[str]] = {}
    for a, b in SAFE_PAIRS:
        partners.setdefault(a.lower(), set()).add(b.lower())
        partners.setdefault(b.lower(), set()).add(a.lower())
    return {k: frozenset(v) for k, v in partners.items()}


def _lower_families() -> dict[str, frozenset[str]]:
    members: dict[str, set[str]] = {}
    for family in FAMILIES.values():
        lowered = {t.lower() for t in family}
        for t in lowered:
            members.setdefault(t, set()).update(lowered)
    return {k: frozenset(v) for k, v in members.items()}


_PAIRED = _lower_pairs()
_FAMILY = _lower_families()


def rule_for(*, identifier: object, label: object) -> TypeRule:
    """The widening a selector earns from what else it pins down.

    `label` is any label selector -- exact, contains or prefix. An identifier
    wins over a label: it is the stronger pin, and the one that makes the
    generic types safe.
    """
    if identifier:
        return "identifier"
    if label:
        return "label"
    return "exact"


def type_matches(wanted: str, actual: str, rule: TypeRule) -> bool:
    """Whether an element of type `actual` satisfies a request for `wanted`.

    Case-insensitive, as type comparison always has been.
    """
    w, a = wanted.lower(), actual.lower()
    if w == a:
        return True
    if rule == "exact":
        return False
    if a in _PAIRED.get(w, ()):
        return True
    return rule == "identifier" and a in _FAMILY.get(w, ())


def related_types(wanted: str) -> frozenset[str]:
    """Every type, lowercased, that any rule could accept for `wanted`.

    For pre-filters that run before the rule is known -- a read narrowed by
    type, or a WDA query -- which must return a superset of what the rule will
    then accept, or they drop the equivalent before the rule ever sees it.
    """
    w = wanted.lower()
    return frozenset({w}) | _PAIRED.get(w, frozenset()) | _FAMILY.get(w, frozenset())


def related_type_names(wanted: str) -> list[str]:
    """`related_types`, spelled the way XCUITest spells them, sorted.

    For a WDA predicate, whose `==` and `IN` are case-sensitive. The caller's
    own spelling is kept as well as the table's, so a type the table does not
    know queries exactly as it did before this module existed.
    """
    own = wanted.lower()
    names = {_original(t) for t in related_types(wanted) if t != own}
    names.add(wanted)
    if own in _ORIGINAL:
        names.add(_ORIGINAL[own])
    return sorted(names)


def equivalence(wanted: str, actual: str) -> str | None:
    """`"RadioButton≈Button"` for a match made through equivalence, else None.

    Reported on the response, so a match that holds only by equivalence can be
    told from an exact one. An exact match carries nothing.
    """
    if wanted.lower() == actual.lower():
        return None
    return f"{wanted}≈{actual}"


def portability_finding(
    element: str, *, identifier: object, label: object,
) -> tuple[str, str] | None:
    """Why a landmark on `element` would match on one backend only, if it would.

    Returns `(code, message)` or None. The v2 conventions check (spec §3.3).

    Types in a safe pair are portable once they carry a label, even though most
    are also in a family whose other members need an identifier: a labelled
    `Button` is portable to a tab item and not to a table row, and quern cannot
    tell which a landmark means. Flagging every labelled `Button` and
    `StaticText` -- the two commonest landmarks there are -- would bury the
    findings that are certain under ones that mostly are not.
    """
    t = element.lower()
    if t in NO_PORTABLE_COUNTERPART:
        return (
            "no_portable_counterpart",
            f"{element} has no labelled counterpart in the accessibility tree, so "
            "this landmark cannot match on a simulator's default backend. Anchor "
            "the screen on its title instead.",
        )
    if identifier:
        return None
    if t in _PAIRED:
        if label:
            return None
        partners = ", ".join(sorted(_original(p) for p in _PAIRED[t]))
        return (
            "needs_identifier_or_label",
            f"A type-only {element} landmark matches on one backend only: the "
            f"other reports it as {partners}. Add an identifier or a label.",
        )
    if t in _FAMILY:
        return (
            "needs_identifier",
            f"{element} is a generic type that the other backend reports as "
            "something more specific, and only an identifier makes the two "
            "match. Add an identifier, or anchor on a different element.",
        )
    return None


_ORIGINAL = {
    t.lower(): t
    for t in [*(x for pair in SAFE_PAIRS for x in pair),
              *(x for fam in FAMILIES.values() for x in fam)]
}


def _original(lowered: str) -> str:
    return _ORIGINAL.get(lowered, lowered)
