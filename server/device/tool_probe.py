"""Moved to `server/tooling/tool_probe.py` (#396). A forwarder for code from
releases before the move that runs against this tree during an update -- an old
server's WDA setup imports `probe_stdout` inside a function. See
`tool_updates.py` here for the reasoning. Import from `server.tooling`.
"""

from server.tooling.tool_probe import probe_stdout  # noqa: F401
