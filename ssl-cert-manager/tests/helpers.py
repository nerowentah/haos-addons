"""Test helpers: make CA and server certificates on the fly (no files, no network)."""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "rootfs" / "app"))

import certs
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

NOW = dt.datetime.now(dt.UTC)


def make_ca(name: str = "Home Test CA", *, days: int = 3650) -> tuple[x509.Certificate, ec.EllipticCurvePrivateKey]:
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(NOW - dt.timedelta(days=1))
        .not_valid_after(NOW + dt.timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )
    return cert, key


def make_leaf(
    names: list[str], ca: tuple[x509.Certificate, ec.EllipticCurvePrivateKey], *, days: int = 90, start_days: int = -1
) -> tuple[x509.Certificate, ec.EllipticCurvePrivateKey]:
    ca_cert, ca_key = ca
    leaf, key = certs.issue(ca_cert, ca_key, names, days=days, now=NOW + dt.timedelta(days=start_days + 1))
    return leaf, key


def pem(*items: object) -> str:
    out = []
    for item in items:
        if isinstance(item, x509.Certificate):
            out.append(certs.cert_pem(item).decode())
        else:
            out.append(certs.key_pem(item).decode())  # type: ignore[arg-type]
    return "".join(out)
