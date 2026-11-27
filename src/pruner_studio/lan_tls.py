"""Persistent LAN listener identity, generated locally on first use."""
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import ipaddress
import os
from pathlib import Path
import secrets
import ssl
import subprocess
import tempfile

from Crypto.Hash import SHA256
from Crypto.PublicKey import ECC
from Crypto.Signature import DSS
from Crypto.Util.asn1 import (
    DerBitString, DerBoolean, DerInteger, DerObject, DerObjectId, DerOctetString,
    DerSequence, DerSetOf,
)


def _certificate(key, addresses):
    signature_type = DerSequence([DerObjectId("1.2.840.10045.4.3.2")])
    cn = DerSequence([DerObjectId("2.5.4.3"), DerObject(12, b"Pruner Studio LAN")])
    name = DerSequence([DerSetOf([cn])])
    now = datetime.now(timezone.utc)
    expiry = now.replace(year=now.year + 10, day=28 if (now.month, now.day) == (2, 29) else now.day)

    def date(value):
        if value.year < 2050:
            return DerObject(23, value.strftime("%y%m%d%H%M%SZ").encode())
        return DerObject(24, value.strftime("%Y%m%d%H%M%SZ").encode())

    def extension(oid, value, critical=False):
        parts = [DerObjectId(oid)]
        if critical:
            parts.append(DerBoolean(True))
        return DerSequence([*parts, DerOctetString(value.encode())])

    names = [DerObject(22, b"localhost", implicit=2)]
    for ip in sorted({"127.0.0.1", *addresses}):
        names.append(DerObject(4, ipaddress.ip_address(ip).packed, implicit=7))
    extensions = DerSequence([
        extension("2.5.29.19", DerSequence(), critical=True),
        extension("2.5.29.15", DerObject(3, bytes([7, 128])), critical=True),
        extension("2.5.29.37", DerSequence([DerObjectId("1.3.6.1.5.5.7.3.1")])),
        extension("2.5.29.17", DerSequence(names)),
    ], explicit=3)
    tbs = DerSequence([DerInteger(2, explicit=0), secrets.randbits(159) or 1,
                       signature_type, name, DerSequence([date(now - timedelta(minutes=5)), date(expiry)]),
                       name, key.public_key().export_key(format="DER"), extensions]).encode()
    signature = DSS.new(key, "fips-186-3", encoding="der").sign(SHA256.new(tbs))
    return DerSequence([tbs, signature_type, DerBitString(signature)]).encode()


def _private_write(path, pem):
    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            if os.name == "nt":
                subprocess.run(["icacls", name, "/inheritance:r", "/grant:r", "*S-1-3-4:(F)"],
                               check=True, stdin=subprocess.DEVNULL, capture_output=True,
                               creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                os.chmod(name, 0o600)
            handle.write(pem.encode("ascii"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def certificate_fingerprint(cert):
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert.read_text("ascii"))).hexdigest()


def extract_cert_spki_der(cert_der: bytes) -> bytes:
    """Extract the raw SubjectPublicKeyInfo (SPKI) DER sequence from an X.509 certificate."""
    cert_seq = DerSequence().decode(cert_der)
    tbs_seq = DerSequence().decode(cert_seq[0])
    # X.509 v3 TBSCertificate: [0]version, serial, sigAlg, issuer, validity, subject, subjectPublicKeyInfo
    if len(tbs_seq) < 7:
        raise ValueError("Invalid X.509 TBSCertificate")
    spki_index = 6 if (tbs_seq[0] and (tbs_seq[0][0] & 0xE0) == 0xA0) else 5
    return bytes(tbs_seq[spki_index])


def spki_fingerprint_from_der(cert_der: bytes) -> str:
    """Return 64-char lowercase hex SHA-256 of the certificate's SubjectPublicKeyInfo (SPKI)."""
    return hashlib.sha256(extract_cert_spki_der(cert_der)).hexdigest()


def spki_pin_from_der(cert_der: bytes) -> str:
    """Return RFC 7469 / OkHttp CertificatePinner pin format: 'sha256/<base64>'."""
    digest = hashlib.sha256(extract_cert_spki_der(cert_der)).digest()
    return "sha256/" + base64.b64encode(digest).decode("ascii")


def certificate_spki_fingerprint(cert: Path) -> str:
    """Return stable 64-char lowercase hex SHA-256 of the public key SPKI (survives SAN cert renewals)."""
    der = ssl.PEM_cert_to_DER_cert(cert.read_text("ascii"))
    return spki_fingerprint_from_der(der)


def certificate_spki_pin(cert: Path) -> str:
    """Return stable RFC 7469 SPKI pin ('sha256/<base64>') for the persistent LAN identity."""
    der = ssl.PEM_cert_to_DER_cert(cert.read_text("ascii"))
    return spki_pin_from_der(der)


def certificate_san_ips(cert):
    """Extract normalized IP SAN strings from a PEM X.509 v3 certificate."""
    try:
        der = ssl.PEM_cert_to_DER_cert(cert.read_text("ascii"))
        cert_seq = DerSequence().decode(der)
        tbs_seq = DerSequence().decode(cert_seq[0])
        ext_wrapper = DerObject().decode(tbs_seq[7])
        exts = DerSequence().decode(ext_wrapper.payload)
        for ext_der in exts:
            ext = DerSequence().decode(ext_der)
            if DerObjectId().decode(ext[0]).value != "2.5.29.17":
                continue
            san_octets = DerOctetString().decode(ext[-1]).payload
            san_seq = DerSequence().decode(san_octets)
            ips = set()
            for item in san_seq:
                if item and item[0] == 0x87:
                    gn = DerObject().decode(item)
                    ips.add(str(ipaddress.ip_address(gn.payload)))
            return ips
    except Exception:
        return set()
    return set()


def load_identity(directory, addresses):
    cert = directory / "lan_tls_cert.pem"
    private = directory / "lan_tls_key.pem"
    needed = {str(ipaddress.ip_address(ip)) for ip in {"127.0.0.1", *addresses}}
    existing_ips = certificate_san_ips(cert) if (cert.exists() and private.exists()) else set()
    if not cert.exists() or not private.exists() or not needed.issubset(existing_ips):
        if private.exists():
            key = ECC.import_key(private.read_bytes())
        else:
            key = ECC.generate(curve="P-256")
            _private_write(private, key.export_key(format="PEM"))
        merged = sorted({"127.0.0.1", *needed, *sorted(existing_ips)[-12:]})
        _private_write(cert, ssl.DER_cert_to_PEM_cert(_certificate(key, merged)))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert, private)
    return str(cert), str(private), certificate_fingerprint(cert), context
