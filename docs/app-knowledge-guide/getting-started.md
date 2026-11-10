# Getting Started

*`quern://app-knowledge-guide/getting-started` -- part of `quern://app-knowledge-guide`, the app knowledge base guide. Start from `quern://app-knowledge-guide`.*

If `.quern/knowledge/` was just initialized from templates, start with:

1. **Fill in `.quern/config.json`** — This is the machine-readable project config. Set `bundle_id` and `app_name` first (use `list_apps` on a device where the app is installed if unknown). If you can find the Xcode project/workspace in the directory, set `workspace` and discover available schemes. Set `url_scheme` and `universal_link_domains` if the user provides them. Check `platform`: `"ios"` or `"android"`, which device requests default to. `init_app_knowledge` fills it in for a project that builds for one platform; for one that builds for both, ask the user which they test on, or leave it `null` to choose per request.
2. **Fill in `app.md`** — Entry points, global navigation (as `global_leads_to`, so tab taps are checked), and test accounts. The bundle ID and URL scheme should match what you put in config.json.
3. **Launch the app** and begin the guided tour (see `quern://app-knowledge-guide/tour`).

If `.quern/knowledge/` already has content, read the existing files and `.quern/config.json` before making changes. Your job is to extend and correct, not overwrite.
