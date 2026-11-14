"""Where certificates live, and how they are published to the shared ``/ssl`` folder.

* ``/data/certs/<id>/`` and ``/data/cas/<id>/`` hold the originals (private
  keys 0600, the app's own storage; backups include it).
* ``/ssl/ssl-cert-manager/`` is what other apps and Home Assistant read:
  ``<id>/{fullchain,privkey,chain}.pem``, ``default/`` (the certificate marked
  default), ``trust/ca-bundle.pem`` (Cloudflare's Origin CA roots plus every
  imported CA) and ``status.json`` (what the Cloudflare Tunnel app reads).
* ``/ssl/fullchain.pem`` and ``/ssl/privkey.pem`` - the names most apps expect -
  get the default certificate only when they do not exist yet or still hold
  what this app wrote last time. A file put there by anything else is never
  overwritten or removed.

:meth:`Files.sync` rebuilds the published tree from ``/data`` and is idempotent.
"""

from __future__ import annotations

import contextlib
import json
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import certs
from cryptography import x509
from safefile import atomic_write

_LOGGER = logging.getLogger(__name__)

DATA_DIR = Path("/data")
SSL_ROOT = Path("/ssl")
SHARE_NAME = "ssl-cert-manager"
DEFAULT_DIR = "default"
TRUST_DIR = "trust"
STATUS_FILE = "status.json"
RESERVED = {DEFAULT_DIR, TRUST_DIR}


def write_file(path: Path, data: bytes) -> bool:
    """Write ``path`` atomically, readable by its owner only; False when it already held exactly ``data``."""

    return atomic_write(path, data)


@dataclass(slots=True)
class SyncResult:
    """What a publish pass changed (``changed`` drives nginx reloads and tunnel checks)."""

    changed: bool = False
    root_note: str | None = None
    errors: list[str] = field(default_factory=list)


class Files:
    def __init__(self, data_dir: Path = DATA_DIR, ssl_root: Path = SSL_ROOT) -> None:
        self.data_dir = data_dir
        self.ssl_root = ssl_root
        self.share = ssl_root / SHARE_NAME
        self.certs_dir = data_dir / "certs"
        self.cas_dir = data_dir / "cas"
        self.owned_file = data_dir / "ssl_root_owned.json"

    # -- the originals in /data ------------------------------------------------------
    def save_cert(self, cert_id: str, chain: list[x509.Certificate], key: certs.PrivateKey) -> None:
        folder = self.certs_dir / cert_id
        write_file(folder / "privkey.pem", certs.key_pem(key))
        write_file(folder / "fullchain.pem", certs.chain_pem(chain))
        write_file(folder / "chain.pem", certs.chain_pem(chain[1:]))

    def save_cert_files(self, cert_id: str, fullchain: bytes, privkey: bytes) -> list[x509.Certificate]:
        """Copy certbot's files in (after checking them); returns the chain."""

        bundle = certs.parse_bundle(
            fullchain.decode("ascii", errors="replace") + privkey.decode("ascii", errors="replace")
        )
        if bundle.key is None:
            raise certs.CertError("No private key in the issued files")
        chain = certs.order_chain(bundle.certs, bundle.key)
        self.save_cert(cert_id, chain, bundle.key)
        return chain

    def load_cert(self, cert_id: str) -> tuple[list[x509.Certificate], certs.PrivateKey]:
        folder = self.certs_dir / cert_id
        bundle = certs.parse_bundle(
            (folder / "fullchain.pem").read_text(encoding="ascii")
            + (folder / "privkey.pem").read_text(encoding="ascii")
        )
        if bundle.key is None:
            raise certs.CertError(f"The private key of {cert_id} is missing")
        return bundle.certs, bundle.key

    def delete_cert(self, cert_id: str) -> None:
        shutil.rmtree(self.certs_dir / cert_id, ignore_errors=True)

    def save_ca(self, ca_id: str, cert: x509.Certificate, key: certs.PrivateKey | None) -> None:
        folder = self.cas_dir / ca_id
        write_file(folder / "cert.pem", certs.cert_pem(cert))
        if key is not None:
            write_file(folder / "key.pem", certs.key_pem(key))

    def load_ca(self, ca_id: str) -> tuple[x509.Certificate, certs.PrivateKey | None]:
        folder = self.cas_dir / ca_id
        text = (folder / "cert.pem").read_text(encoding="ascii")
        with contextlib.suppress(FileNotFoundError):
            text += (folder / "key.pem").read_text(encoding="ascii")
        bundle = certs.parse_bundle(text)
        return bundle.certs[0], bundle.key

    def delete_ca(self, ca_id: str) -> None:
        shutil.rmtree(self.cas_dir / ca_id, ignore_errors=True)

    # -- published paths ---------------------------------------------------------------
    def published(self, cert_id: str) -> tuple[Path, Path]:
        """``(fullchain, privkey)`` under /ssl for a certificate id (or ``default``)."""

        folder = self.share / cert_id
        return folder / "fullchain.pem", folder / "privkey.pem"

    @property
    def trust_bundle(self) -> Path:
        return self.share / TRUST_DIR / "ca-bundle.pem"

    @property
    def status_path(self) -> Path:
        return self.share / STATUS_FILE

    # -- /ssl/fullchain.pem ownership ---------------------------------------------------
    def _owned(self) -> dict[str, str]:
        try:
            data = json.loads(self.owned_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}

    def _may_write_root(self, name: str, owned: dict[str, str]) -> bool:
        path = self.ssl_root / name
        try:
            current = path.read_bytes()
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return owned.get(name) == certs.digest(current)

    def _sync_root(self, wanted: dict[str, bytes] | None, result: SyncResult) -> None:
        """Write (``wanted``) or remove (None) /ssl/fullchain.pem + privkey.pem, only where this app owns them."""

        owned = self._owned()
        names = ("fullchain.pem", "privkey.pem")
        if not all(self._may_write_root(name, owned) for name in names):
            if wanted is not None:
                result.root_note = (
                    "/ssl/fullchain.pem or /ssl/privkey.pem was put there by something else, so it was left alone. "
                    "Use the files in /ssl/ssl-cert-manager/default/ instead."
                )
            return
        for name in names:
            path = self.ssl_root / name
            if wanted is None:
                if name in owned:
                    path.unlink(missing_ok=True)
                    owned.pop(name, None)
                    result.changed = True
                continue
            data = wanted[name]
            if write_file(path, data):
                result.changed = True
            owned[name] = certs.digest(data)
        write_file(self.owned_file, json.dumps(owned).encode())

    # -- publish --------------------------------------------------------------------------
    def sync(  # noqa: PLR0912 - one branch per published file group
        self,
        cert_ids: list[str],
        default_id: str | None,
        trust_pem: str,
        status: dict[str, object],
        *,
        write_root: bool,
    ) -> SyncResult:
        """Make /ssl/ssl-cert-manager match /data (and the /ssl root files, where allowed)."""

        result = SyncResult()
        for cert_id in cert_ids:
            source = self.certs_dir / cert_id
            try:
                files = {name: (source / name).read_bytes() for name in ("fullchain.pem", "privkey.pem", "chain.pem")}
            except OSError as err:
                result.errors.append(f"{cert_id}: {err}")
                continue
            for name, data in files.items():
                if write_file(self.share / cert_id / name, data):
                    result.changed = True
            if cert_id == default_id:
                for name, data in files.items():
                    if write_file(self.share / DEFAULT_DIR / name, data):
                        result.changed = True
                root = {"fullchain.pem": files["fullchain.pem"], "privkey.pem": files["privkey.pem"]}
                self._sync_root(root if write_root else None, result)
        if default_id not in cert_ids:
            if (self.share / DEFAULT_DIR).exists():
                shutil.rmtree(self.share / DEFAULT_DIR, ignore_errors=True)
                result.changed = True
            self._sync_root(None, result)
        # Folders of deleted certificates.
        if self.share.is_dir():
            for folder in self.share.iterdir():
                if folder.is_dir() and folder.name not in RESERVED and folder.name not in cert_ids:
                    shutil.rmtree(folder, ignore_errors=True)
                    result.changed = True
        if write_file(self.trust_bundle, trust_pem.encode("ascii")):
            result.changed = True
        write_file(self.status_path, json.dumps(status, indent=2, sort_keys=True).encode("utf-8"))
        return result
