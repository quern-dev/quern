"""Moved to `server/tooling/tool_versions.py` (#396). A forwarder for the
`quern update` of releases 0.18.1-0.18.3 only; see `tool_updates.py` here for
why it exists and when to remove it. Import from `server.tooling`.
"""

from server.tooling.tool_versions import collect_sites  # noqa: F401
