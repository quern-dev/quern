"""The remote listener's certificate: made by quern, pinned by clients.

No certificate authority is involved. Quern generates a self-signed ECDSA
P-256 certificate the first time the remote listener starts, and every client
pins its SHA-256 fingerprint, which pairing delivers -- so there is nothing
to buy, no domain to own, and nothing a public CA could issue on someone
else's behalf. The names in it (hostname, LAN addresses, configured hosts)
are for whoever inspects it; clients check the fingerprint, not the names.

`quern remote rotate-cert` replaces it, which ends every pin: each client must
pair again. That is what a rotation is for.
"""

from __future__ import annotations

import datetime
import hashlib
import ipaddress
import logging
import os
import socket
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: Ten years: the pin, not the expiry, is what clients trust, and a
#: certificate that lapsed under a running fleet would cut every client off.
VALID_DAYS = 3650


@dataclass(frozen=True)
class RemoteCert:
    cert_path: Path
    key_path: Path
    fingerprint: str            # sha256 of the DER certificate, lowercase hex


def cert_dir() -> Path:
    from server.config import CONFIG_DIR

    return CONFIG_DIR / "remote"


def fingerprint_of(der: bytes) -> str:
    return hashlib.sha256(der).hexdigest()


def show(fingerprint: str) -> str:
    """A fingerprint the way people compare them: `SHA256:AB:CD:...`."""
    pairs = [fingerprint[i:i + 2].upper() for i in range(0, len(fingerprint), 2)]
    return "SHA256:" + ":".join(pairs)


def normalise(fingerprint: str) -> str:
    """Any spelling -- with or without `SHA256:`, colons, case -- to the hex
    `fingerprint_of` produces. Raises ValueError for anything else."""
    value = fingerprint.strip()
    if value.lower().startswith("sha256:"):
        value = value[7:]
    value = value.replace(":", "").lower()
    if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError(f"not a SHA-256 fingerprint: {fingerprint!r}")
    return value


def load_or_create(names: list[str]) -> RemoteCert:
    """The certificate on disk, made first if there is none. One that cannot
    be read is not replaced -- that would end every pin silently -- but
    raised, so the listener reports it and stays off."""
    directory = cert_dir()
    cert_path, key_path = directory / "cert.pem", directory / "key.pem"
    if not cert_path.exists() or not key_path.exists():
        _generate(cert_path, key_path, names)
    from cryptography import x509
    from cryptography.hazmat.primitives.serialization import Encoding

    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    return RemoteCert(cert_path=cert_path, key_path=key_path,
                      fingerprint=fingerprint_of(cert.public_bytes(Encoding.DER)))


def rotate(names: list[str]) -> RemoteCert:
    """A new certificate in place of the old: every client must pair again."""
    directory = cert_dir()
    for name in ("cert.pem", "key.pem"):
        (directory / name).unlink(missing_ok=True)
    return load_or_create(names)


def names_for(hosts: list[str], lan: list[str]) -> list[str]:
    """What to name in the certificate: this Mac's hostname, its LAN
    addresses, and the configured extra hosts."""
    found = [socket.gethostname(), *lan, *hosts]
    out: list[str] = []
    for name in found:
        if name and name not in out:
            out.append(name)
    return out


def _generate(cert_path: Path, key_path: Path, names: list[str]) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,
                                            f"quern {socket.gethostname()}"[:64])])
    alt: list[x509.GeneralName] = []
    for name in names:
        try:
            alt.append(x509.IPAddress(ipaddress.ip_address(name)))
        except ValueError:
            alt.append(x509.DNSName(name))
    now = datetime.datetime.now(datetime.UTC)
    builder = (x509.CertificateBuilder()
               .subject_name(subject).issuer_name(subject)
               .public_key(key.public_key())
               .serial_number(x509.random_serial_number())
               .not_valid_before(now - datetime.timedelta(minutes=5))
               .not_valid_after(now + datetime.timedelta(days=VALID_DAYS)))
    if alt:
        builder = builder.add_extension(x509.SubjectAlternativeName(alt), critical=False)
    cert = builder.sign(key, hashes.SHA256())
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(cert_path.parent, 0o700)
    _write_private(key_path, key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    logger.info("Made the remote listener's certificate, %s", show(
        fingerprint_of(cert.public_bytes(serialization.Encoding.DER))))


def _write_private(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    os.chmod(path, 0o600)
