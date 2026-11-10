# Documenting Environments

*`quern://app-knowledge-guide/environments` -- part of `quern://app-knowledge-guide`, the app knowledge base guide. Start from `quern://app-knowledge-guide`.*

`environments.md` captures the available server environments and how to switch between them. Add each one to `.quern/config.json`'s `environments` object as well, with its domain and deep-link pattern (`quern://app-knowledge-guide/tour`, step 3). Not all apps have multiple environments — if the app only targets one backend, skip this file.

**What to capture:**

- Base URL per environment
- How to switch (plist key, build variant, settings toggle, deep link)
- Which test accounts work where
- Behavioral differences (sandboxed payments, relaxed rate limits, test data)
- Any proxy/cert setup differences

**Ask the user early:** "Does this app have staging/dev environments? How do I switch between them?" This shapes everything else — test accounts, network capture setup, and which behaviors are real vs environment-specific.
