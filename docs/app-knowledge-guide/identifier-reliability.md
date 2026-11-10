# Identifier Reliability

*`quern://app-knowledge-guide/identifier-reliability` -- part of `quern://app-knowledge-guide`, the app knowledge base guide. Start from `quern://app-knowledge-guide`.*

Accessibility identifiers in real apps are frequently misleading, reused, or missing. This is the biggest source of silent agent failures. Be defensive:

**Misleading identifiers.** An element's accessibility identifier may not match its visible label or purpose. For example, a "Quick Guide" button might have identifier `BackButton`, or a "Help Center" link might have identifier `Drafts`. When you document an element, always record the *visible label* alongside the identifier. If they differ significantly, flag it explicitly in the Notes column — a future agent trusting the identifier alone will tap the wrong thing.

**Shared identifiers.** Multiple elements on the same screen may share an identifier. For example, both "Go Premium" and "Redeem Code" might use `_Upgrade button`. When you discover shared identifiers:

1. Note it in both elements' documentation.
2. In tool calls, prefer `tap_element` with `label` (the visible text) rather than relying on the identifier.
3. If neither label nor identifier uniquely identifies the element, document the position or use a combination: element_type + label + parent context via `children_of`.

**Missing identifiers.** Some elements have no identifier at all. Use `label` (visible text) + `element_type` as the primary selector. Document this so agents don't waste time searching for an identifier that doesn't exist.

**These could be bugs.** Misleading, shared, or missing identifiers are usually not intentional — they're accessibility defects in the app code, typically fixable with a single line setting `accessibilityIdentifier`. When you discover one during the tour, document it immediately in `quirks/identifier-issues.md` and flag it to the user as something worth investigating right away. Don't just work around it silently. A short list of "these identifiers need attention" is high-value, low-effort work that improves both agent reliability and the app's general accessibility.

**General rule:** When writing `tap_element` commands in screen docs, use the most reliable selector you found during the tour — usually `label` for visible text. Add a comment if the identifier is known to be misleading or shared. The Key Elements table should capture both label and identifier so future agents can choose the right approach.
