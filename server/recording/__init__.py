"""Recording a device's actions, flows, logs and video to disk for a whole run.

`recorder` holds the recorder and the events file, `video` the simulator movie
through quern-media, and `cli` the `quern record` command.

Nothing is re-exported here, on purpose: import from the module that defines a
name. Tests patch module globals (`PAUSE_WAIT`, `os.kill`), and a patch applied
through a re-export lands on the package's copy while the code reads its own.
"""
