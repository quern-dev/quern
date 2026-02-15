# Quern Roadmap

**Last updated:** February 12, 2026
**Status:** Living document. Seeds and commitments coexist — read the status tags.

---

## How to Read This Document

Each item has a status tag:

- ✅ **Complete** — Shipped and validated
- 🔧 **In Progress** — Currently being built or tested
- 📐 **Designed** — Architecture doc exists, ready for implementation
- 💡 **Conceptual** — Discussed and directionally agreed, needs design work
- 🌱 **Seed** — Mentioned in conversation, worth preserving, not yet committed

---

## 1. Core Platform (Single-Machine Foundation)

Everything in this bucket runs locally on one developer's Mac. This is the open-source heart of the project — the complete tool, not a demo.

### 1.1 Device Log Capture (Phase 1)

| Item | Status | Notes |
|------|--------|-------|
| `idevicesyslog` streaming + parsing | ✅ Complete | Primary log source for device logs |
| Ring buffer storage | ✅ Complete | In-memory, configurable size |
| FastAPI server (`/logs/stream` SSE, `/logs/query`) | ✅ Complete | API key auth, process filtering |
| OSLog adapter (`log stream --style json`) | ✅ Complete | Mac-side unified logs only — does not capture device app logs |
| Log summary endpoint | ✅ Complete | Template-based, cursor support |
| Crash report watcher | ✅ Complete | `.ips` / `.crash` parsing |
| `xcodebuild` output parser | ✅ Complete | Build errors, warnings, test results |
| MCP tools for log access | ✅ Complete | `tail_logs`, `query_logs`, etc. |
| App-embedded log drain (Swift package) | 🌱 Seed | Originally Phase 1d. Deferred — more important than initially scoped. Would provide structured, always-on observability without modifying source code. Current workaround: agents add temporary `print()`/`NSLog()` calls. |
| SQLite persistent storage option | 🌱 Seed | Logs survive server restart. Not yet needed. |
| Log correlation engine (group by request/trace ID) | 🌱 Seed | Depends on app drain or proxy flow IDs. |

### 1.2 Network Traffic Inspection (Phase 2)

| Item | Status | Notes |
|------|--------|-------|
| mitmproxy integration via `mitmdump` subprocess | ✅ Complete | JSON-lines addon for structured flow capture |
| Flow store (capture, query, inspect) | ✅ Complete | 5,000 flow default, full request/response bodies |
| Traffic summary endpoint | ✅ Complete | By-host aggregation, error patterns, slow requests |
| Intercept rules (hold/modify/release) | ✅ Complete | mitmproxy filter expression syntax |
| Replay captured requests | ✅ Complete | With optional header/body modification |
| Mock responses | ✅ Complete | Pattern-matched response injection |
| Simulator proxy setup automation | ✅ Complete | `simctl keychain` cert install, localhost proxy |
| Setup guide endpoint (IP + steps) | ✅ Complete | Auto-detected host IP |
| MCP tools for proxy | ✅ Complete | `proxy_status`, `query_flows`, `intercept`, `mock`, etc. |
| Physical device proxy setup automation | 💡 Conceptual | Could use Phase 3 UI automation to walk through Settings. Alternatively, MDM-based cert push. Investigate during Phase 3d. |

### 1.3 Device Inspection & UI Control (Phase 3)

| Item | Status | Notes |
|------|--------|-------|
| `simctl` backend (boot, install, launch, screenshot, location, permissions) | ✅ Complete | Full simulator management |
| `idb` backend (accessibility tree, tap, swipe, type) | ✅ Complete | UI automation via `idb_companion`, nothing installed on simulator |
| DeviceController orchestrator | ✅ Complete | Routes operations to correct backend |
| MCP tools for device control | ✅ Complete | `screenshot`, `describe_ui`, `tap`, `swipe`, `type_text`, etc. |
| Physical device support (`devicectl` + WDA) | 📐 Designed | Phase 3d. Needed for NFC testing. `devicectl` handles device management (iOS 17+), WDA needed for UI automation on physical devices. |

### 1.4 Operational Polish (Phase 4)

| Item | Status | Notes |
|------|--------|-------|
| **4a: Process Lifecycle** | | |
| Daemon mode (fork, setsid, backgrounding) | ✅ Complete | `quern start` backgrounds, `--foreground` for debugging |
| `state.json` as single source of truth | ✅ Complete | `~/.quern/state.json` — PID, ports, devices |
| Idempotent start (detect running, reuse or restart) | ✅ Complete | Health check + PID file |
| Port scanning (9100 server, 9101 proxy, auto-increment on conflict) | ✅ Complete | Retired port 8080 |
| `ensure_server` MCP tool | ✅ Complete | Single entry point for agents |
| CLI subcommands (`start`, `stop`, `restart`, `status`) | ✅ Complete | |
| Proxy watchdog (crash detection, degraded status) | ✅ Complete | No auto-restart — agent decides |
| **4b: Multi-Device Support** | | |
| 4b-alpha: Device pool (claim/release, state file, locking) | ✅ Complete | `DevicePool` class, `~/.quern/device-pool.json`, MCP tools |
| 4b-beta: Session management | 🌱 Seed | Deferred — claim timeout (30 min auto-release) covers most use cases without formal sessions |
| 4b-gamma: Resolution protocol | ✅ Complete | `resolve_device()`, `ensure_devices()`, criteria matching, auto-boot, wait-for-available, diagnostic errors, controller fallback |
| 4b-delta: Testing, polish & agent UX | 📐 Designed | MCP guide discoverability, `clear_text` action, hierarchy option for `get_screen_summary`. Informed by real agent feedback. |
| **4c: Headless CLI Runner** | | |
| `quern run` command | 📐 Designed | `--device`, `--proxy`, `--prompt`, `--output`. Starts everything, executes scenario via AI agent, captures results, shuts down. Foundation for CI. |
| Automated scenario support | 💡 Conceptual | Scenario file format, step definitions |

### 1.5 Installation & Onboarding

| Item | Status | Notes |
|------|--------|-------|
| `quern setup` automated installer | ✅ Complete | Environment validation, Homebrew auto-install of deps |
| Dependency management (libimobiledevice, Xcode CLI, mitmdump, Node.js) | ✅ Complete | |
| README / getting started documentation | 💡 Conceptual | Needs writing for open-source launch |
| `pip install` distribution | 💡 Conceptual | PyPI package |
| MCP server npm package | 💡 Conceptual | `npx quern-mcp` |

---

## 2. Developer Experience (Human-Facing Tools)

These are the features that make Quern useful for *humans* directly, not just AI agents. This is where the first natural paid features live.

### 2.1 Results & Reporting

| Item | Status | Notes |
|------|--------|-------|
| Playwright-style HTML reports from test runs | 💡 Conceptual | Timeline view of network events, log viewer, screenshot gallery. High-impact — all the data is already structured. |
| Diagnostic bundle capture | 🌱 Seed | Bundle logs + flows + screenshots + UI state from a session into a shareable artifact. Prerequisite for the report viewer. |
| Session recorder | 🌱 Seed | Record agent actions + app state for replay and review. Building block for AI-native test runner. |

### 2.2 Real-Time Web UI

| Item | Status | Notes |
|------|--------|-------|
| Network inspector (flows streaming, payload inspection) | 🌱 Seed | "Modern Charles" replacement. Significant frontend effort. |
| Log viewer window | 🌱 Seed | Real-time filtered log stream |
| Live simulator screenshot / state view | 🌱 Seed | Center panel in the unified dashboard |
| Unified dashboard (network + logs + device state) | 🌱 Seed | The "Charles + Instruments + log reading" replacement. Designed for AI-assisted workflows. |

---

## 3. Agent Intelligence (AI-Native Capabilities)

Features that make the AI agent fundamentally more capable, beyond just providing it with tools.

### 3.1 App Graph & Test DSL

> **Design doc:** `quern-app-graph-and-dsl-design.md` — full design rationale, schema sketches, DSL syntax, and agent reasoning use cases.

| Item | Status | Notes |
|------|--------|-------|
| **App Graph** | | |
| Agent-driven app exploration → `app.graph.json` | 💡 Conceptual | Agent systematically visits every screen, records controls/transitions. Expensive but infrequent. |
| Graph schema (screens as nodes, actions as edges, marker-based screen identity) | 💡 Conceptual | Resilient to UI changes — screens identified by marker *combinations*, not single brittle IDs. |
| Source-file-to-screen linkage | 🌱 Seed | Graph nodes carry metadata linking to implementing source files. Enables pre-commit validation. |
| Pre-commit graph validation hook | 💡 Conceptual | Examines changed files, flags affected graph nodes/edges. Lightweight, no device needed. Graph changes live in the feature branch. |
| Periodic re-exploration + graph diff | 🌱 Seed | Agent re-explores flagged areas, proposes graph updates as a reviewable PR. |
| **Test DSL** | | |
| Two-level abstraction: granular steps + reusable flows (presets/verbs) | 💡 Conceptual | `tap "Sign In"` when the step IS the test; `do: logged_in_as(user)` when it's just setup. |
| `navigate_to` graph-resolved navigation | 💡 Conceptual | DSL uses graph pathfinding — tests don't specify *how* to reach a screen, just which screen. Resilient to nav changes. |
| Parameterized test data (`.data.quern` files) | 💡 Conceptual | Same flow, different data, no duplication. Data lives in repo alongside tests. |
| DSL → MCP tool call compiler | 🌱 Seed | Translates human-readable DSL into executable Quern tool calls. |
| `.quern` file organization (graph/, flows/, tests/, data/) | 💡 Conceptual | All version-controlled, diffable, reviewable in PRs. |
| **Agent Reasoning with Graph** | | |
| Graph as MCP resource for coding/debugging tasks | 💡 Conceptual | Agent consults graph to understand app structure *before reading code*. Useful for navigation, blast radius analysis, reproduction steps. |
| Graph query tools (`get_screen`, `find_path`, `screens_affected_by`, `tests_covering`) | 🌱 Seed | MCP tools for graph reasoning during any task, not just testing. |
| Test coverage analysis (untested screens/edges) | 🌱 Seed | Cross-reference graph against `.test.quern` files. |
| Graph enrichment (perf baselines, error hotspots, data flow, a11y) | 🌱 Seed | Graph accumulates richer metadata over time, becoming a multi-dimensional app model. |

### 3.2 AI-Native Test Runner

| Item | Status | Notes |
|------|--------|-------|
| Four-phase testing lifecycle | 💡 Conceptual | (1) AI explores app, builds graph (expensive, once). (2) Compiles tests into deterministic DSL scripts (automatic). (3) Executes DSL without AI (cheap, every push). (4) Triages failures with full context (expensive, only on failure). |
| DSL execution runtime | 🌱 Seed | Interprets `.test.quern` files, executes flows, reports results |
| Replay engine (deterministic DSL execution) | 🌱 Seed | Execute without AI involvement — the cheap inner loop |
| Test generation from graph | 🌱 Seed | Agent proposes test scenarios based on discovered flows and untested edges |
| Failure triage with diagnostic context | 🌱 Seed | On failure, agent gets logs + flows + screenshots + UI diff + graph context |

### 3.3 Self-Validating Coding Agents

| Item | Status | Notes |
|------|--------|-------|
| Closed feedback loop: build → deploy → observe → verify | 🌱 Seed | **The endgame.** Coding agents use Quern to verify their own changes. Transforms agents from code writers into developers. Emerges naturally from headless runner + test infrastructure. |
| Development workflow integration | 🌱 Seed | Dev says "okay, test that on the simulator" and the agent uses Quern to debug/verify the new feature. |

---

## 4. Infrastructure & Scale (Team/Enterprise)

The coordination layer that only matters when you outgrow a single machine. This is the natural commercial boundary — open core stays free, infrastructure is paid.

### 4.1 Device Farm

| Item | Status | Notes |
|------|--------|-------|
| Multi-device scheduling / provisioning | 🌱 Seed | Who gets which device when |
| Queue management | 🌱 Seed | |
| Multi-tenant access controls | 🌱 Seed | |
| DeviceFarmer compatibility / complementarity | 🌱 Seed | Quern handles AI-driven testing per device; farm layer handles fleet management |

### 4.2 CI/CD Integration

| Item | Status | Notes |
|------|--------|-------|
| GitHub Actions / GitLab CI integration | 🌱 Seed | Push → spin up device → run AI test scenarios → report. Depends on headless runner + device farm. |
| CI without writing test code | 🌱 Seed | The pitch: describe what you want tested in English. |

### 4.3 Team Features

| Item | Status | Notes |
|------|--------|-------|
| Cross-run analytics / trend detection | 🌱 Seed | Flaky test identification across a fleet |
| Result aggregation dashboard | 🌱 Seed | |
| Shared test scenarios | 🌱 Seed | |
| Role-based access / audit logs | 🌱 Seed | |

---

## 5. Platform Expansion

| Item | Status | Notes |
|------|--------|-------|
| Android support | 🌱 Seed | The architecture (MCP tools → HTTP API → device control + diagnostics) is platform-agnostic in principle. iOS is the proof of concept; the pattern is the product. |
| Web app support | 🌱 Seed | Playwright/Selenium equivalent with the same AI-agent-first approach |
| Desktop app support | 🌱 Seed | |

---

## 6. Open Source & Distribution

| Item | Status | Notes |
|------|--------|-------|
| GitHub migration (from Bitbucket) | 🔧 In Progress | Private repo first, then public |
| License decision | 💡 Conceptual | Options: Commons Clause + MIT (can't repackage/sell), BSL, full MIT with separate closed-source paid package. Deferred until the tool has users and community feedback. |
| Namespace claims | 💡 Conceptual | quern.dev domain, github.com/quern org, @quern or @querndev on X, PyPI `quern`, npm `quern-mcp` |
| Community documentation | 🌱 Seed | Contributing guide, architecture overview for contributors |

---

## 7. Known Bugs & Tech Debt

| Item | Status | Notes |
|------|--------|-------|
| Stale active device UDID → 500 instead of 400 | 🔧 Known Bug | Cached UDID for a shutdown simulator gives unfriendly error |

---

## Priority Guidance

**Current focus:** Phase 4 operational polish — making the tool reliable and pleasant to use before adding features.

**Near-term sequence:**
1. Finish Phase 4b (device resolution rules) — formalize and implement
2. Phase 4c (headless CLI runner) — `quern run` unlocks CI and the test runner vision
3. Physical device support (Phase 3d) — unblocks NFC development workflow
4. GitHub migration + namespace claims
5. Open-source launch prep (README, docs, license decision)

**The insight that should guide all prioritization:** Operational reliability must come before feature expansion. Process management friction wastes more time than missing features.

**The long-term north star:** Self-validating coding agents. Every other item on this roadmap is infrastructure that builds toward an agent that can write code, deploy it, observe the result, and verify correctness — without human intervention.
