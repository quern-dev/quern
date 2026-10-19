"""Pairing: one code, both sides proven, the code never sent (#254, phase 3).

`quern pair` on the server makes a one-time code; the person types it into
`quern connect` on the client. The client connects over TLS *without*
trusting the certificate, notes its fingerprint, and the two exchange proofs:

    hello   client -> nonce_c               server -> nonce_s, proof_s
    finish  client -> proof_c               server -> a client key

    proof_s = HMAC(secret, "server" | id | nonce_c | nonce_s | fingerprint)
    proof_c = HMAC(secret, "client" | id | nonce_c | nonce_s | fingerprint)

The server signs its own certificate's fingerprint; the client checks the
proof against the fingerprint it actually saw, and computes its own over the
same. A machine in the middle presents a different certificate, so it can
neither make a server proof the client accepts nor turn a client proof into
one the server accepts -- not without the secret, which is never sent and is
~60 bits, valid for five minutes, and voided after five failed attempts.

The code a person types is `IIII-SSSS-SSSS-SSSS`: a 4-character id, sent in
the clear so the server can find the code, and the 12-character secret.
Crockford base32, so 0/O and 1/I/L read the same.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass, field

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_READ_AS = str.maketrans({"O": "0", "I": "1", "L": "1"})
ID_LEN, SECRET_LEN = 4, 12
#: How long a code lasts once made.
CODE_SECONDS = 300
#: Attempts -- hellos and failed finishes together -- before a code is void.
MAX_ATTEMPTS = 5
_NONCE_HEX = 64


class PairingError(Exception):
    """A pairing step refused; `status` is the HTTP status to answer with."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class _Code:
    id: str
    secret: str
    label: str
    expires: float
    attempts: int = 0
    nonces: tuple[str, str] | None = field(default=None, repr=False)


def _random(n: int) -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(n))


def parse(code: str) -> tuple[str, str]:
    """A typed code to (id, secret): case, dashes and spaces ignored, and the
    letters people mistake for digits read as those digits."""
    cleaned = "".join(c for c in code.upper() if c not in "- ").translate(_READ_AS)
    if len(cleaned) != ID_LEN + SECRET_LEN or any(c not in _ALPHABET for c in cleaned):
        raise PairingError("that is not a pairing code: it is 16 letters and digits, "
                           "like ABCD-EFGH-JKMN-PQRS")
    return cleaned[:ID_LEN], cleaned[ID_LEN:]


def proof(side: str, secret: str, code_id: str, nonce_c: str, nonce_s: str,
          fingerprint: str) -> str:
    message = "\0".join(("quern-pair", side, code_id, nonce_c, nonce_s, fingerprint))
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def new_nonce() -> str:
    return secrets.token_hex(_NONCE_HEX // 2)


def _valid_nonce(value: str) -> bool:
    return (isinstance(value, str) and len(value) == _NONCE_HEX
            and all(c in "0123456789abcdef" for c in value))


class PairingCodes:
    """The codes this server has made and not yet used up. In memory only:
    a code outlives neither its five minutes nor the process."""

    def __init__(self, clock=time.monotonic) -> None:
        self._codes: dict[str, _Code] = {}
        self._clock = clock

    def create(self, label: str) -> tuple[str, int]:
        """A new code for a client to be labelled `label`; (code, seconds)."""
        self._prune()
        code_id = _random(ID_LEN)
        while code_id in self._codes:
            code_id = _random(ID_LEN)
        secret = _random(SECRET_LEN)
        self._codes[code_id] = _Code(code_id, secret, label, self._clock() + CODE_SECONDS)
        typed = code_id + secret
        return "-".join(typed[i:i + 4] for i in range(0, len(typed), 4)), CODE_SECONDS

    def hello(self, code_id: str, nonce_c: str, fingerprint: str) -> tuple[str, str]:
        """The server's half: a fresh nonce and its proof over its own
        certificate. Counts as an attempt, so the proofs an eavesdropper can
        collect are bounded too."""
        code = self._get(code_id)
        if not _valid_nonce(nonce_c):
            raise PairingError("a pairing nonce is 64 lowercase hex characters")
        self._attempt(code)
        nonce_s = new_nonce()
        code.nonces = (nonce_c, nonce_s)
        return nonce_s, proof("server", code.secret, code.id, nonce_c, nonce_s, fingerprint)

    def finish(self, code_id: str, nonce_c: str, nonce_s: str, client_proof: str,
               fingerprint: str) -> str:
        """The client's half, checked over this server's own certificate. The
        code's label on success, and the code is used up; a failed proof
        counts against it."""
        code = self._get(code_id)
        if code.nonces != (nonce_c, nonce_s):
            self._attempt(code)
            raise PairingError("this pairing exchange did not start here: begin again", 409)
        expected = proof("client", code.secret, code.id, nonce_c, nonce_s, fingerprint)
        if not (isinstance(client_proof, str) and hmac.compare_digest(client_proof, expected)):
            self._attempt(code)
            raise PairingError(
                "the client's proof does not match: the code was mistyped, or the "
                "connection is not to this server's certificate", 403)
        del self._codes[code.id]
        return code.label

    def _get(self, code_id: str) -> _Code:
        self._prune()
        code = self._codes.get(code_id) if isinstance(code_id, str) else None
        if code is None:
            raise PairingError("no such pairing code: it expired, was used, or was "
                               "mistyped -- make a new one with `quern pair`", 404)
        return code

    def _attempt(self, code: _Code) -> None:
        code.attempts += 1
        if code.attempts > MAX_ATTEMPTS:
            self._codes.pop(code.id, None)
            raise PairingError("too many attempts with this pairing code: it is void; "
                               "make a new one with `quern pair`", 403)

    def _prune(self) -> None:
        now = self._clock()
        for code_id in [i for i, c in self._codes.items() if c.expires <= now]:
            del self._codes[code_id]
