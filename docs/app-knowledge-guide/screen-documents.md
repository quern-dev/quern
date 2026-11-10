# Writing Screen Documents

*`quern://app-knowledge-guide/screen-documents` -- part of `quern://app-knowledge-guide`, the app knowledge base guide. Start from `quern://app-knowledge-guide`.*

Copy `screens/_template.md` and fill it in. Key principles:

- **`landmarks` is the most important field.** Quern evaluates it server-side against the live UI tree to answer "what screen am I on?" — it's the foundation of `identify_screen`, `get_screen_summary?identify=true`, and any future automation that needs to recognize where the agent is. Use the most unique, stable elements (nav bar titles, screen-specific identifiers).
- **`landmarks` is the only field consulted for matching.** Screens written before April 2026 may also carry `identify_by:`; nothing reads it. Put prose notes in the body of the document instead, where they will be read.
- **Include actual quern tool calls.** Don't write "tap the login button" — write `tap_element label="Sign In" element_type="button"`. The agent will copy-paste these.
- **Be precise about element types.** Use the exact types from `get_ui_tree` for quern tool calls — don't guess. For anything that will become an XCUITest selector, use `xcui_type` from a simulator with `start_driver` running, not `type`. See `quern://app-knowledge-guide/element-types`.
- **Record landmarks on the backend you will identify with.** Landmarks match an element's `type`, and `type` follows the backend: the tab a landmark recorded on sim-bridge calls `RadioButton` is `Button` while the simulator is in WDA mode, and will not match there.
- **Document failure modes.** What alerts, errors, or unexpected states can occur? How should the agent recover?
