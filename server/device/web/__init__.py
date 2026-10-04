"""Web content inside apps: reading WebKit views on simulators and devices.

`webinspector` speaks the Web Inspector protocol to a simulator's WebKit,
`web_content` turns what it reads into elements, and `web_probing` locates web
content on screen where there is no inspector to ask.

Nothing is re-exported here: import from the module that defines a name.
"""
