---
purpose: Defines the meaningful app states that affect navigation, available features, and screen behavior.
---

# App States

States are preconditions referenced throughout the knowledge base. Each state
describes a mode the app can be in that changes what the agent sees or can do.

<!-- Define each state the app can be in. Common categories:

## Authentication

| State | How to Detect | How to Enter | How to Exit |
|---|---|---|---|
| Logged out | ... | ... | ... |
| Logged in | ... | ... | ... |

## Subscription / Tier

| State | How to Detect | How to Enter | How to Exit |
|---|---|---|---|
| Free tier | ... | ... | ... |
| Premium | ... | ... | ... |

## Onboarding

| State | How to Detect | How to Enter | How to Exit |
|---|---|---|---|
| First launch (FTUE) | ... | ... | ... |
| Onboarding complete | ... | ... | ... |

## Environment

| State | How to Detect | How to Enter | How to Exit |
|---|---|---|---|
| Production | ... | ... | ... |
| Staging | ... | ... | ... |

## Connectivity

| State | How to Detect | How to Enter | How to Exit |
|---|---|---|---|
| Online | ... | ... | ... |
| Offline | ... | ... | ... |

-->

<!-- For each state, fill in:
     - **How to Detect**: What the agent can check to confirm this state.
       Use quern tools: read_app_plist for flags, get_screen_summary for UI signals,
       query_flows for network indicators.
     - **How to Enter**: Steps to reach this state. Prefer the fastest method:
       deep link, app state restore, plist change, or manual flow.
     - **How to Exit**: Steps to leave this state.
-->

<!-- States that affect many screens should be documented here.
     States local to a single screen belong in that screen's "States" section. -->
