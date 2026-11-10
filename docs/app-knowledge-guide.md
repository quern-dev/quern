# App Knowledge Base — Agent Guide

You have a `.quern/knowledge/` directory in this project for documenting the app under test. This guide explains how to build and maintain it.

## What This Is

The app knowledge base is a set of markdown files that capture everything you need to navigate, test, and debug this app: screen identification, navigation graphs, deep link shortcuts, device quirks, and domain terminology. It is optimized for agent consumption — precise, structured, and directly actionable with quern tool calls.

## Pages

This guide is split into pages, one per subject. Read the one you need:

- `quern://app-knowledge-guide/getting-started` -- Getting Started: a new or existing knowledge base: config.json and app.md first, then the tour
- `quern://app-knowledge-guide/before-you-start` -- Before You Start: preparing the device: resolving it, and pre-granting permissions so prompts do not interrupt the tour
- `quern://app-knowledge-guide/tour` -- The Guided Tour: the guided tour with the user: how it works and the order to cover the app in
- `quern://app-knowledge-guide/config-json` -- Updating config.json During the Tour: keeping .quern/config.json current: plist watch targets, state flags, saved checkpoints, environments
- `quern://app-knowledge-guide/element-types` -- Common iOS Element Types: element types from the accessibility tree vs XCUITest (start_driver), and which to use where
- `quern://app-knowledge-guide/per-screen` -- What to Capture Per Screen: what to record for each screen: identification, key elements, navigation, states, dynamic content, quirks
- `quern://app-knowledge-guide/asking` -- What to Ask the User: what to ask the user, and when, instead of a checklist
- `quern://app-knowledge-guide/stubs` -- Stubs: Tracking Undiscovered Screens: stub files for screens you know about but have not visited
- `quern://app-knowledge-guide/navigation-edges` -- Writing Navigation Edges: leads_to and app.md's global_leads_to: their format, and how tap_element checks them
- `quern://app-knowledge-guide/screen-documents` -- Writing Screen Documents: screen documents from the template; landmarks is the field matching uses
- `quern://app-knowledge-guide/choosing-landmarks` -- Choosing Landmarks: which elements make good landmarks, in order of strength
- `quern://app-knowledge-guide/no-landmarks` -- When a Knowledge Base Has No Landmarks: load_landmarks returning screens: 0 and what each skipped[] reason means
- `quern://app-knowledge-guide/landmarks-sync` -- Keeping Landmarks in Sync: landmarks that drifted from the app: the signs, and fixing them
- `quern://app-knowledge-guide/auditing` -- Auditing an Existing Knowledge Base: checking an older knowledge base against the landmark conventions
- `quern://app-knowledge-guide/identifier-reliability` -- Identifier Reliability: misleading, shared or missing accessibility identifiers, and what to match on instead
- `quern://app-knowledge-guide/dynamic-labels` -- Dynamic and Long Labels: labels with variable content: matching a stable part
- `quern://app-knowledge-guide/web-views` -- Documenting Web Views: screens built on web content, which the UI tree cannot see into
- `quern://app-knowledge-guide/overlay-panels` -- Overlay Panels: bottom sheets, map cards and other panels, documented inside their parent screen
- `quern://app-knowledge-guide/flows` -- Writing Flow Documents: flow documents: setup first, actionable preconditions, verifiable steps
- `quern://app-knowledge-guide/alerts` -- Documenting Alerts: alert documents: permission prompts, popups and other transient UI
- `quern://app-knowledge-guide/app-states` -- Documenting App States: states.md: app-wide modes such as logged in, onboarding, feature flags
- `quern://app-knowledge-guide/environments` -- Documenting Environments: environments.md: server environments and switching between them
- `quern://app-knowledge-guide/deep-links` -- Documenting Deep Links: deep links in deep-links/deep_links.json: finding them, and verifying each one
- `quern://app-knowledge-guide/maintaining` -- Maintaining the Knowledge Base: keeping it current: updating on app changes, quirks, verifying before trusting
