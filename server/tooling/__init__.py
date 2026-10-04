"""The host's external tools: finding them, versioning them, updating them.

`tool_probe` runs a tool's version or health command with a timeout, for the
device backends and for setup. `tool_versions` collects every install site of
the tools quern depends on and what each reports; `tool_updates` plans and
applies upgrades from that (`quern update --tools`).

Nothing is re-exported here: import from the module that defines a name.
"""
