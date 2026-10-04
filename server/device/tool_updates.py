"""Moved to `server/tooling/tool_updates.py` (#396). This forwarder is for one
caller: the `quern update` of releases 0.18.1-0.18.3.

Those updaters swap the source tree and then keep running in the same process,
and `_report_tool_updates` imports, after the swap, from the *new* tree:

    from server.device.tool_updates import actionable, format_offer, plan_updates
    from server.device.tool_versions import collect_sites

Without this file that import fails after the swap -- the update stops with
the new code installed and nothing rebuilt or restarted, the #212 failure.
`tests/test_old_updater_imports.py` pins the exact names. Nothing in this tree
imports from here; import from `server.tooling`. Remove when no supported
upgrade path starts from 0.18.3 or older.
"""

from server.tooling.tool_updates import actionable, format_offer, plan_updates  # noqa: F401
