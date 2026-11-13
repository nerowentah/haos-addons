"""Read, check and make TLS certificates (``cryptography`` only, no network).

Everything the panel imports goes through :func:`parse_bundle`: PEM text with
any mix of certificates and one private key. Nothing here logs key material;
errors name what is wrong, never the content.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import ipaddress
import re
from dataclasses import dataclass, field

from cryptography import x509
from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed448, ed25519, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

PrivateKey = rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey | ed25519.Ed25519PrivateKey | ed448.Ed448PrivateKey

MAX_ISSUED_DAYS = 397  # what browsers accept for a server certificate, also from a private CA
_PEM_RE = re.compile(r"-----BEGIN ([A-Z0-9 ]+)-----\r?\n.*?-----END \1-----", re.DOTALL)
_KEY_LABELS = {"PRIVATE KEY", "RSA PRIVATE KEY", "EC PRIVATE KEY"}
_DNS_RE = re.compile(
    r"^(?=.{1,253}$)(\*\.)?(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)


class CertError(ValueError):
    """A certificate, key or name that cannot be used (the message is safe to show)."""


@dataclass(slots=True)
class Bundle:
    """What one import holds: certificates in file order, and at most one private key."""

    certs: list[x509.Certificate] = field(default_factory=list)
    key: PrivateKey | None = None


@dataclass(slots=True)
class CertInfo:
    """What the panel shows about a certificate."""

    subject: str
    issuer: str
    names: list[str]
    not_before: dt.datetime
    not_after: dt.datetime
    fingerprint: str
    is_ca: bool
    self_signed: bool

    def days_left(self, now: dt.datetime | None = None) -> int:
        now = now or dt.datetime.now(dt.UTC)
        return int((self.not_after - now).total_seconds() // 86400)

    def as_dict(self) -> dict[str, object]:
        return {
            "subject": self.subject,
            "issuer": self.issuer,
            "names": list(self.names),
            "not_before": self.not_before.isoformat(),
            "not_after": self.not_after.isoformat(),
            "fingerprint": self.fingerprint,
            "is_ca": self.is_ca,
            "self_signed": self.self_signed,
        }


# -- parsing ------------------------------------------------------------------------
def parse_bundle(text: str) -> Bundle:
    """Every certificate and the private key in PEM ``text``; raises CertError on anything unusable."""

    bundle = Bundle()
    blocks = list(_PEM_RE.finditer(text or ""))
    if not blocks:
        raise CertError("No PEM data found (expected -----BEGIN CERTIFICATE----- or -----BEGIN PRIVATE KEY-----)")
    for block in blocks:
        label, pem = block.group(1), block.group(0).encode("ascii", errors="replace")
        if label == "CERTIFICATE":
            try:
                bundle.certs.append(x509.load_pem_x509_certificate(pem))
            except ValueError as err:
                raise CertError("A certificate in the file is damaged") from err
        elif label in _KEY_LABELS:
            if bundle.key is not None:
                raise CertError("The files hold more than one private key; import one certificate at a time")
            bundle.key = _load_key(pem)
        elif label == "ENCRYPTED PRIVATE KEY":
            raise CertError(
                "The private key is password-protected; remove the password first (openssl pkey -in key.pem)"
            )
        # Anything else (parameters, CSRs...) is ignored.
    return bundle


def _load_key(pem: bytes) -> PrivateKey:
    try:
        key = serialization.load_pem_private_key(pem, password=None)
    except TypeError as err:  # encrypted (legacy "Proc-Type: 4,ENCRYPTED")
        raise CertError("The private key is password-protected; remove the password first") from err
    except (ValueError, UnsupportedAlgorithm) as err:
        raise CertError("The private key is damaged or of an unsupported type") from err
    if not isinstance(key, PrivateKey):
        raise CertError("Unsupported private key type (use RSA, EC or Ed25519)")
    return key


def info(cert: x509.Certificate) -> CertInfo:
    """Summary of one certificate."""

    return CertInfo(
        subject=cert.subject.rfc4514_string(),
        issuer=cert.issuer.rfc4514_string(),
        names=names(cert),
        not_before=cert.not_valid_before_utc,
        not_after=cert.not_valid_after_utc,
        fingerprint=fingerprint(cert),
        is_ca=is_ca(cert),
        self_signed=cert.issuer == cert.subject,
    )


def fingerprint(cert: x509.Certificate) -> str:
    return cert.fingerprint(hashes.SHA256()).hex(":").upper()


def names(cert: x509.Certificate) -> list[str]:
    """DNS names and IP addresses the certificate covers (the common name when it has no SAN)."""

    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        cns = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
        return [str(cns[0].value)] if cns else []
    found = [str(name) for name in san.get_values_for_type(x509.DNSName)]
    found += [str(ip) for ip in san.get_values_for_type(x509.IPAddress)]
    return found


def is_ca(cert: x509.Certificate) -> bool:
    try:
        return bool(cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca)
    except x509.ExtensionNotFound:
        return False


def covers(cert_names: list[str], hostname: str) -> bool:
    """Whether a certificate with ``cert_names`` is valid for ``hostname`` (one-label wildcards)."""

    host = hostname.lower().rstrip(".")
    for name in (n.lower() for n in cert_names):
        if name == host:
            return True
        if name.startswith("*.") and "." in host and host.split(".", 1)[1] == name[2:]:
            return True
    return False


def same_key(cert: x509.Certificate, key: PrivateKey) -> bool:
    """Whether ``key`` is the private key of ``cert``."""

    public = serialization.PublicFormat.SubjectPublicKeyInfo
    der = serialization.Encoding.DER
    return cert.public_key().public_bytes(der, public) == key.public_key().public_bytes(der, public)


def signed_by(cert: x509.Certificate, issuer: x509.Certificate) -> bool:
    """Whether ``issuer`` signed ``cert`` (signature check only)."""

    if cert.issuer != issuer.subject:
        return False
    try:
        cert.verify_directly_issued_by(issuer)
    except (ValueError, TypeError, InvalidSignature, UnsupportedAlgorithm):
        return False
    return True


def order_chain(certs: list[x509.Certificate], key: PrivateKey | None) -> list[x509.Certificate]:
    """Leaf first, then each issuer, as servers must send them; raises CertError when they do not chain."""

    if not certs:
        raise CertError("No certificate found")
    pool = list(certs)
    if key is not None:
        leaf = next((c for c in pool if same_key(c, key)), None)
        if leaf is None:
            raise CertError("The private key does not belong to any of the certificates")
    else:
        issuers = {c.issuer for c in pool}
        leaves = [c for c in pool if c.subject not in issuers or c.issuer == c.subject]
        leaf = leaves[0] if leaves else pool[0]
    chain = [leaf]
    pool.remove(leaf)
    while pool:
        nxt = next((c for c in pool if signed_by(chain[-1], c)), None)
        if nxt is None:
            break
        chain.append(nxt)
        pool.remove(nxt)
    # Certificates that are not part of the chain are dropped, but a chain that
    # stops at a non-root while unrelated certificates remain is a mistake.
    if pool and not chain[-1].issuer == chain[-1].subject and any(c.issuer != c.subject for c in pool):
        raise CertError("Some certificates in the file do not belong to this certificate's chain")
    return chain


def check_server_cert(chain: list[x509.Certificate], now: dt.datetime | None = None) -> None:
    """Reject a leaf that cannot serve TLS: a CA, expired, not yet valid, or without any name."""

    now = now or dt.datetime.now(dt.UTC)
    leaf = chain[0]
    if is_ca(leaf):
        raise CertError("This is a CA certificate; use Import CA instead")
    if leaf.not_valid_after_utc <= now:
        raise CertError(f"The certificate expired on {leaf.not_valid_after_utc:%Y-%m-%d}")
    if leaf.not_valid_before_utc > now + dt.timedelta(days=1):
        raise CertError(f"The certificate is not valid before {leaf.not_valid_before_utc:%Y-%m-%d}")
    if not names(leaf):
        raise CertError("The certificate names no host (no subject alternative name or common name)")


# -- names --------------------------------------------------------------------------
def parse_names(text: object) -> list[str]:
    """Host names and IP addresses from a comma or space separated list, de-duplicated."""

    found: list[str] = []
    for raw in re.split(r"[,\s]+", str(text or "")):
        item = raw.strip().lower().rstrip(".")
        if not item:
            continue
        item = item.removeprefix("https://").removeprefix("http://").split("/", 1)[0]
        try:
            item = str(ipaddress.ip_address(item.strip("[]")))
        except ValueError:
            if not _DNS_RE.match(item):
                raise CertError(f"'{item}' is not a host name or IP address") from None
        if item not in found:
            found.append(item)
    if not found:
        raise CertError("Enter at least one host name, e.g. home.example.com")
    return found


def public_names(hostnames: list[str]) -> list[str]:
    """Names a public CA (Let's Encrypt, Cloudflare) can issue for: DNS names with a dot, no IPs."""

    bad = [n for n in hostnames if "." not in n or _is_ip(n) or n.endswith((".local", ".lan", ".home.arpa"))]
    if bad:
        raise CertError(f"A public certificate needs names under your own domain; not: {', '.join(bad)}")
    return hostnames


def _is_ip(name: str) -> bool:
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return False
    return True


# -- making keys and certificates ---------------------------------------------------
def new_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def key_pem(key: PrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )


def cert_pem(cert: x509.Certificate) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)


def chain_pem(chain: list[x509.Certificate]) -> bytes:
    return b"".join(cert_pem(c) for c in chain)


def _san(hostnames: list[str]) -> x509.SubjectAlternativeName:
    entries: list[x509.GeneralName] = []
    for name in hostnames:
        try:
            entries.append(x509.IPAddress(ipaddress.ip_address(name)))
        except ValueError:
            entries.append(x509.DNSName(name))
    return x509.SubjectAlternativeName(entries)


def csr_pem(key: PrivateKey, hostnames: list[str]) -> str:
    """A certificate signing request for ``hostnames`` (the first is the common name)."""

    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostnames[0][:64])]))
        .add_extension(_san(hostnames), critical=False)
        .sign(key, hashes.SHA256())  # type: ignore[arg-type]
    )
    return csr.public_bytes(serialization.Encoding.PEM).decode("ascii")


def check_issuer(ca_cert: x509.Certificate, ca_key: PrivateKey) -> None:
    """An imported CA must be a CA, still valid, and come with its own key."""

    if not is_ca(ca_cert):
        raise CertError(
            "This certificate is not a CA (BasicConstraints CA:TRUE is missing); import it as a certificate"
        )
    if not same_key(ca_cert, ca_key):
        raise CertError("The private key does not belong to the CA certificate")
    if ca_cert.not_valid_after_utc <= dt.datetime.now(dt.UTC):
        raise CertError(f"The CA certificate expired on {ca_cert.not_valid_after_utc:%Y-%m-%d}")
    try:
        usage = ca_cert.extensions.get_extension_for_class(x509.KeyUsage).value
    except x509.ExtensionNotFound:
        return
    if not usage.key_cert_sign:
        raise CertError("The CA certificate may not sign certificates (KeyUsage keyCertSign is missing)")


def issue(
    ca_cert: x509.Certificate,
    ca_key: PrivateKey,
    hostnames: list[str],
    days: int = MAX_ISSUED_DAYS,
    now: dt.datetime | None = None,
) -> tuple[x509.Certificate, ec.EllipticCurvePrivateKey]:
    """A server certificate for ``hostnames`` signed by an imported CA, with a new P-256 key."""

    check_issuer(ca_cert, ca_key)
    now = now or dt.datetime.now(dt.UTC)
    not_after = min(now + dt.timedelta(days=min(days, MAX_ISSUED_DAYS)), ca_cert.not_valid_after_utc)
    key = new_key()
    algorithm = None if isinstance(ca_key, ed25519.Ed25519PrivateKey | ed448.Ed448PrivateKey) else hashes.SHA256()
    builder = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostnames[0][:64])]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(not_after)
        .add_extension(_san(hostnames), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)  # type: ignore[arg-type]
    )
    return builder.sign(ca_key, algorithm), key  # type: ignore[arg-type]


def digest(data: bytes) -> str:
    """SHA-256 of file contents (used to recognise files this app wrote)."""

    return hashlib.sha256(data).hexdigest()
