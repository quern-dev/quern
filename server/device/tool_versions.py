"""Moved to `server/tooling/tool_versions.py` (#396). A forwarder for code from
releases before the move that runs against this tree during an update; see
`tool_updates.py` here for why it exists, how the names were found, and when it
can go. Import from `server.tooling.tool_versions`.
"""

from server.tooling.tool_versions import collect_sites, upgrade_note  # noqa: F401
