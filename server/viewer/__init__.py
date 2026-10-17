"""The LAN viewer for recordings with video (#449).

A separate, opt-in listener -- `viewer.enabled` in config.json, port 9120 by
default -- that serves only viewer pages and their streams, each behind a
token issued per recording. No API route is reachable through it, whatever
the token. See `app.py` for the routes and `grants.py` for the tokens.
"""
