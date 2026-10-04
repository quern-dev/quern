# Landmarks across backends, and versioned landmark conventions — Spec

**Status:** implemented in the follow-up to #336 / #362. The tables live in
`server/device/element_types.py`; the conventions check in
`server/knowledge/landmarks.py`. User-facing reference:
[`../screen-landmarks.md`](../screen-landmarks.md#across-backends).
**Raised:** 2026-10-01, while landing #362 (WDA on simulators).
**Related:** #336, #362; [`knowledge-base-health.md`](knowledge-base-health.md)
(where knowledge-base warnings belong) and
[`kb-drift-measurement.md`](kb-drift-measurement.md) (where it diverges from
reality); [`../screen-landmarks.md`](../screen-landmarks.md) (the landmark
reference this changes).

---

## 0. The problem

A landmark names an element type, and **the type depends on which backend read
the screen.** On a simulator the default is the accessibility tree (sim-bridge,
or idb as a fallback). A physical iPhone, and since #362 a simulator after
`start_driver`, is read by WebDriverAgent, which reports XCUITest's types. The
same tab-bar item is a `RadioButton` in one and a `Button` in the other.

So a knowledge base recorded on one backend silently fails on the other:

- **A landmark written on a simulator has never matched the same screen on a
  phone.** This predates #362; it was simply never measured. #362 made it
  visible by putting both vocabularies on one machine.
- **The same applies to `tap_element`'s `element_type`.** The conformance suite
  opens the More list with `element_type="RadioButton"`, which finds nothing on
  a simulator in WDA mode.
- **Nothing tells an agent which conventions a knowledge base follows.** The
  last convention change, `identify_by` → `landmarks` in April 2026, was
  handled with a diagnostic precisely because old and new files could not
  otherwise be told apart.

This spec covers both halves: matching across backends (§2), and letting a
knowledge base declare the conventions it is written for, with compliance
computed rather than claimed (§3).

## 1. What was measured

All twelve QuernProbe screens were read through quern's own API, once on the
accessibility tree and once in WDA mode, on the same iOS 18.6 simulator.
Elements were matched across the two trees by identifier, then label, using the
nearest frame where a label repeats. Two screens, **Widgets** and **Lists**,
were added to the probe for this (§5) because it lacked 17 standard element
types. The Scroll screen was excluded: WDA's `/source` timed out on its long
list and returned a navigation-only fallback, which the response reported as
`degraded`.

### 1.1 Pairs that differ

| Accessibility tree | XCUITest | Count | Where |
|---|---|---|---|
| `RadioButton` | `Button` | 50 | tab items |
| `Heading` | `StaticText` | 10 | screen titles |
| `Group` | `TabBar` | 10 | tab bar |
| `Button` | `StaticText` | 6 | More-list rows (see 1.3) |
| `Button` | `Cell` | 2 | table rows |
| `StaticText` | `Cell` | 1 | a table row |
| `Group` | `NavigationBar`, `Toolbar`, `Table`, `CollectionView` | 1 each | containers |
| `GenericElement` | `ProgressIndicator`, `ActivityIndicator`, `ColorWell`, `StaticText` | 1 each | indicators, colour well, table footer |
| `Heading` | `Other` | 1 | table section header |
| `TextField` | `SecureTextField` | 1 | password field |
| `CheckBox` | `Switch` | 1 | toggle |
| `TabGroup` | `SegmentedControl` | 1 | segmented control |
| `TextArea` | `TextView` | 1 | multi-line text |
| `Slider` | `PageIndicator` | 1 | page control |

The same in both: `StaticText` (28), `Button` (25), `Application` (12),
`TextField` (3), `Slider`, `Image`.

### 1.2 Three things the table does not show at a glance

- **The accessibility tree's generic types cover many specific ones.** `Group`
  is the tab bar, the navigation bar, the toolbar, the table *and* the
  collection view. `GenericElement` is a progress bar, a spinner, a colour well
  and a table footer. Equating `Group` with `TabBar` would let any group match
  a tab-bar landmark.
- **The accessibility tree is not consistent with itself.** Three table rows
  differing only in their accessory came back as `Button`, `StaticText`,
  `Button`. XCUITest called all three `Cell`. Only the identifier was stable.
- **Some types have no labelled counterpart at all.** XCUITest reports a
  `SearchField`, `DatePicker`, `Picker` and `NavigationBar`. The accessibility
  tree exposed none of them with a label or identifier, so a landmark on one of
  these cannot be portable whatever the matching rule.

### 1.3 A pair that must never be equated

`Button` ↔ `StaticText` (6) is not one element named two ways. In the
accessibility tree a More-list row is a single `Button`; to XCUITest it is a
`Cell` containing a `StaticText`, and the label match lands on the *text inside
the row*. Equating the two by label would let every button landmark match plain
text.

## 2. Matching across backends

### 2.1 The rule

A landmark's `element` matches an element's `type` according to what else the
landmark pins down:

| Landmark has | Type must be |
|---|---|
| an `identifier` | in the same **family** as the element's type (§2.2) |
| a `label` or `label_contains`, no identifier | an exact match or a **safe pair** (§2.2) |
| neither | an exact match, as today |

The identifier is what makes the generic types safe: if a landmark names
`lists_row_1`, the element carrying that identifier *is* the row, and whether a
backend calls it `Button`, `StaticText` or `Cell` is vocabulary, not identity.

Comparison stays case-insensitive, as it is today.

**An identifier that repeats the element's label is a label.** WDA reports
`name` -- the identifier, or the label when there is none -- and has no
attribute holding the identifier alone (measured: `rawIdentifier` and
`identifier` are rejected in a predicate, and `name == 'More'` matches the
More tab, which has no identifier). So the family rule applies per element,
and only where the identifier differs from that element's label; otherwise
a `Button` selector would reach the rows family and land on a title.
(Found by independent review.)

**An exact match wins.** When an element of exactly the named type matches, the
equivalents are dropped. Equivalence is how a selector written on one backend
finds its element on the other, not a way to widen a selector that already
found it: otherwise a screen holding both a `Button` and a `RadioButton`
labelled "Home" would turn an unambiguous `tap_element` into an ambiguous one.
(Settled during implementation.)

### 2.2 The tables

**Safe pairs** — equated on label or identifier:

`RadioButton`↔`Button` · `Heading`↔`StaticText` · `TextField`↔`SecureTextField`
· `CheckBox`↔`Switch` · `TabGroup`↔`SegmentedControl` · `TextArea`↔`TextView`
· `Slider`↔`PageIndicator`

**Families** — equated only on identifier. A family is a safe pair plus the
generic types the accessibility tree uses for it:

- containers: `Group`, `Other`, `TabBar`, `NavigationBar`, `Toolbar`, `Table`,
  `CollectionView`
- rows: `Button`, `StaticText`, `Cell`
- indicators: `GenericElement`, `ProgressIndicator`, `ActivityIndicator`,
  `ColorWell`, `StaticText`
- headers: `Heading`, `Other`, `StaticText`

The tables are data, kept beside the matcher with the measurement that
justified each entry. A pair is added only when measured, never inferred — the
first assumption made during this work (that WDA sends `XCUIElementType`
prefixes) was wrong on contact.

### 2.3 Saying so

A match that went through a pair or a family reports it on the identification
result — for example `matched_via: "RadioButton≈Button"` — so a landmark that
matches only by equivalence is distinguishable from an exact one. Exact matches
carry nothing.

### 2.4 `tap_element`

`element_type` on `tap_element` (and the other element filters) uses the same
rule against the same tables. The same equivalence is reported on the response.

## 3. Versioned landmark conventions

### 3.1 The file declares a target

Each screen file may declare the conventions it is written for:

```yaml
---
screen: home
landmark_conventions: 2
landmarks:
  - element: RadioButton
    identifier: tab.home
---
```

This is a **target, not a claim of compliance.** It says what the file is
written for. Whoever writes or migrates the file sets it, and the
`init_app_knowledge` templates and the guides use the current version, so new
files start current. **Quern never writes it.**

### 3.2 Compliance is computed

On `load_landmarks` and `validate_landmarks`, quern checks every file against
the conventions and reports each file in one of four states:

| Declared | Computed | Reported as |
|---|---|---|
| nothing | — | **undeclared** — read as v1 |
| older than current | — | **behind** — with what migrating would change |
| current | findings | **targets current but does not meet it** — the landmarks and why |
| current | clean | **current** |

The two views answer different questions and both are true:

- `grep -L "landmark_conventions: 2" screens/*.md` — which files have *not been
  migrated*. No loading needed.
- the `conventions` block in the load response — which files *actually
  comply*.

Per [`knowledge-base-health.md`](knowledge-base-health.md), these findings
appear on the two calls that ask about the knowledge base. They do not appear
on every identification: a warning on every response is noise, and noise is
how the one that matters gets missed.

### 3.3 The versions

- **v1** — everything before this spec, and every file that declares nothing.
- **v2** — this spec. The computed checks:
  1. A landmark whose type is in a §1.1 pair or family, and which lacks the
     identifier or label that §2.1 needs for that pair to be safe. It matches
     on one backend only. Implemented as: a type-only landmark on a paired type
     (`needs_identifier_or_label`), and a landmark on a type that is in a
     family but no pair, without an identifier (`needs_identifier`). A
     labelled `Button` or `StaticText` is *not* flagged, although each is also
     in a family: it is portable to a tab item and not to a table row, quern
     cannot tell which a landmark means, and flagging the two commonest
     landmarks would bury the certain findings. (Settled during
     implementation.)
  2. A landmark on a type with no labelled counterpart in the accessibility
     tree (`SearchField`, `DatePicker`, `Picker`, `NavigationBar`). It is not
     portable; anchor the screen on its title instead.
  3. `identify_by` without `landmarks` — the existing `legacy_format`
     diagnostic, folded in.

### 3.4 Versioned interpretation

Because each file declares the conventions it is written for, a later version
that changes what a field *means* can read each file by the rules it declares,
instead of breaking every knowledge base written before the change. v2 does not
need this — see §6.3 — but it is what makes a future v3 possible without a
repeat of the `identify_by` diagnostic.

## 4. Auditing an existing knowledge base

This goes into the agent guide in this form.

1. **Load it and read the `conventions` block.** Every file is reported as
   undeclared, behind, failing or current, with the specific landmarks at fault.
2. **Fix what it reports.** Typically: add an `identifier` to a type-only
   landmark, or move a landmark off a non-portable type onto the screen title.
3. **Set `landmark_conventions: 2`** on each file once it is fixed, and reload
   to confirm it reports current.
4. **Optionally, confirm behaviour as well as form.** Run `validate_landmarks`
   against the live screen on the default backend, then again after
   `start_driver`. A landmark that identifies the screen on only one of them is
   the thing this spec exists to catch.

Step 4 checks against reality; steps 1–3 check against the conventions. A file
can pass the first three and still be wrong about the app, which is
[`kb-drift-measurement.md`](kb-drift-measurement.md)'s territory, not this
one's.

## 5. Probe app

Two screens are added to QuernProbe, reached through More so that the five
tab-bar items the conformance suite drives do not change:

- **Widgets** — a search bar, progress and activity indicators, a page control,
  an image, a pull-down menu button, a colour well, a compact date picker, a
  text view, a picker wheel, and a standalone toolbar.
- **Lists** — a table with a section header and footer and three accessory
  types, and a collection view.

Everything is on screen at once: a fair comparison needs both backends to see
every element, and WDA falls back to navigation chrome on a long list. The
toolbar is standalone and nothing touches large titles, because screens in the
More list share one navigation stack and anything set on it would leak onto
the others.

## 6. Decisions

### ADR 1 — Equate types in matching, not in `type`

**Date:** 2026-10-01 · **Status:** accepted

**Context.** Two places could absorb the difference: the `type` each backend
reports, or the comparison landmarks and element filters make.

**Decision.** In the comparison. `type` stays what each backend reported.

**Why.** Rewriting `type` would make every backend's output dishonest about
what it saw — the reason #362 added `xcui_type` rather than changing `type` —
and would change what existing consumers read. Matching is one function, used
by landmarks and element filters alike, so the equivalence lives in one place.

### ADR 2 — Identifier, label and neither get different rules

**Date:** 2026-10-01 · **Status:** accepted

**Context.** A flat table equating, say, `Group` with `TabBar` would be simple,
and wrong: §1.2 shows `Group` standing for five different containers.

**Decision.** The §2.1 rule. Generic types are equated only when an identifier
already pins the element; specific pairs also on a label; nothing is widened
for a type-only landmark.

**Alternatives considered.** A single table (rejected: false matches on generic
types). Equivalence for everything carrying a label (rejected: §1.3, where the
label lands on a different element).

### ADR 3 — The version is a declared target; compliance is computed

**Date:** 2026-10-01 · **Status:** accepted

**Context.** The goal is to tell at a glance, across a whole knowledge base,
whether its files follow the current landmark conventions.

**Decision.** Each file declares `landmark_conventions: N` as the conventions
it is *written for*. Quern never writes it. Compliance is computed on every
load and validation and reported per file (§3.2).

**Alternatives considered.**

1. **Compute only, no field in the files.** Always true, but you cannot see
   migration progress without loading the whole knowledge base.
2. **A compliance stamp in each file.** Visible, but it is a claim, and it goes
   stale on the first edit after the audit that wrote it — reading as current
   exactly when it is not. The same failure as a recorded certificate trust
   outliving a simulator erase (`cert-trust-model.md`, ADR 1), and this repo
   reports what it checked rather than what it recorded.
3. **A compliance stamp written only by quern, carrying a fingerprint of the
   audited landmarks**, so an edit invalidates it detectably. Sound, but more
   machinery than the problem needs, and it still asks the file to carry a
   statement about its own correctness.

A declared target has none of these problems. It cannot go stale because it
claims nothing; it is visible with `grep`; and it is the hook versioned
interpretation needs (§3.4). Reached in conversation by combining the per-file
visibility of (2) with the computation of (1).

### ADR 4 — The tables come from measurement

**Date:** 2026-10-01 · **Status:** accepted

Every pair and family in §2.2 is backed by an observed element in §1, and new
entries are added the same way. The probe screens in §5 exist to make that
possible for types it did not have.

### ADR 5 — Equivalence applies to undeclared files too

**Date:** 2026-10-01 · **Status:** accepted (was open question 1)

Files that declare no `landmark_conventions` (every knowledge base written
before this spec) get the §2 matching rule as well. The widening is safe under
§2.1, existing knowledge bases are the ones that gain from it, and versioned
interpretation (§3.4) is reserved for changes that alter what a field *means*,
which this does not. Declaring v2 changes what is *checked* (§3.3), not how a
file is matched.

### ADR 6 — `tap_element` is in the same change

**Date:** 2026-10-01 · **Status:** accepted (was open question 2)

`element_type` on `tap_element` and the other element filters uses the §2 rule
and tables in the same change as landmarks (§2.4). The conformance suite
already depends on it: it opens the More list as a `RadioButton`, which fails
on a simulator in WDA mode.

## 7. Open questions

1. **Coverage beyond the probe.** The tables cover what the probe exercises on
   iOS 18.6. Other iOS versions, SwiftUI-specific controls, and web content
   inside a `WebView` were not measured. Entries are added as they are.
