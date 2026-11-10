# Choosing Landmarks

*`quern://app-knowledge-guide/choosing-landmarks` -- part of `quern://app-knowledge-guide`, the app knowledge base guide. Start from `quern://app-knowledge-guide`.*

A landmark is an element selector that must be present (or absent) for a screen to be recognized. All landmarks for a screen must match — they're AND'd together. Pick the smallest set that uniquely identifies the screen. In order of reliability:

1. **Accessibility identifier** — locale-independent and usually stable across app versions. Strongest signal when one exists.
2. **Navigation bar title or unique button identifier** — one per screen, rarely changes.
3. **Tab bar selection state** — when a screen lives behind a tab, `{ element: "RadioButton", identifier: "tab.foo", selected: true }` distinguishes "this screen is active" from "this tab merely exists." Required when several screens share the same tab.* identifier pattern.
4. **Unique label text** — works for screens with no stable identifier. Locale-dependent; flag with a comment if the app is localized.
5. **`absent: true`** — sometimes the cleanest disambiguator is "the parent screen's compose button is *not* present." Use sparingly.

**Make landmarks portable across backends.** The same screen is read through the accessibility tree on a simulator and through WDA on a phone (or a simulator after `start_driver`), and they name some elements differently. Quern matches across the two when a landmark carries enough to make it safe, so:

- Give every landmark a label or an identifier. A type-only landmark is matched exactly, so on a type the two backends name differently (`RadioButton`, `Heading`, `Group`, …) it holds on one backend only.
- An identifier that only repeats the element's label pins no more than the label does. WDA reports the label as the identifier when an element has none, so such a landmark gets the label rule, not the family.
- Give generic types (`Group`, `Other`, `GenericElement`, `Cell`) an identifier; a label is not enough for them.
- Don't anchor on `navigationBar`, `searchField`, `datePicker` or `picker`: the accessibility tree exposes none of them with a label or identifier. Use the screen title — a `Heading` with its label — instead.
- Start each new screen file with `landmark_conventions: 2`. It is the version you wrote the file for, not a claim it complies; quern computes that on load. See [Auditing a knowledge base](https://github.com/quern-dev/quern/blob/main/docs/screen-landmarks.md#auditing-a-knowledge-base).

After authoring, run `validate_landmarks` (or the `quern validate` HTTP endpoint) on the knowledge base to catch overlapping landmarks and read its `conventions` block. Two screens whose landmark sets are subsets of each other will collide — at least one needs a distinguishing element.
