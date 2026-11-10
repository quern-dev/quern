# Dynamic and Long Labels

*`quern://app-knowledge-guide/dynamic-labels` -- part of `quern://app-knowledge-guide`, the app knowledge base guide. Start from `quern://app-knowledge-guide`.*

Some elements have labels that include dynamic, variable-length content. For example, an attributes row might have a label like `"Attributes, Not recommended for kids, Stroller accessible, Dogs allowed, Available 24/7, Takes less than one hour, Park and grab, Parking nearby, Stealth required"` — a comma-separated list that changes per item.

This is a separate issue from identifier reliability. The label is *correct*, but it's not stable across instances.

**How to handle dynamic labels:**

- **Document the stable prefix.** If the label always starts with a fixed string (e.g., `"Attributes, "`), record that prefix and note that the rest varies.
- **Use partial matching.** In `tap_element`, pass the stable portion as `label_prefix` (or `label_contains` for a stable middle); `label` matches the whole label.
- **In the Key Elements table**, write the stable portion followed by `...` and explain the dynamic part in Notes. Example: `Attributes, ...` with note "Comma-separated list of cache attributes, varies per cache."
- **Prefer identifier over label** for dynamic-label elements — this is the one case where identifiers are more reliable than labels, if one exists.
