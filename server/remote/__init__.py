"""Reaching quern from another machine (#254, phase 3).

A TLS listener, off by default, that serves the same API to client keys --
never the owner's -- with a certificate quern makes itself and clients pin by
fingerprint. Pairing (server/remote/pairing.py) is how a client learns both
the fingerprint and a key without either crossing the network unprotected.
"""
