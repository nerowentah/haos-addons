"""Let's Encrypt certificates through certbot's Cloudflare DNS-01 plugin.

DNS-01 needs no open port: certbot puts a TXT record in the Cloudflare zone,
Let's Encrypt checks it, and the record is removed again. The Cloudflare API
token goes to certbot only through a 0600 credentials file, never on the
command line or in the log.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

CERTBOT = "/usr/bin/certbot"
ACME_DIR = Path("/data/acme")
PROPAGATION_SECONDS = 30
TIMEOUT_SECONDS = 600  # DNS propagation plus Let's Encrypt; certbot normally needs about a minute

RunFn = Callable[[list[str]], tuple[int, str]]

_ERROR_LINE = re.compile(r"^(?:Error|Detail|Hint|Type|An unexpected error|Certbot failed|.*error:)", re.IGNORECASE)


class AcmeError(RuntimeError):
    """certbot failed (the message is safe to show)."""


@dataclass(slots=True)
class Issued:
    """Where certbot left the files of one certificate."""

    fullchain: Path
    privkey: Path
    chain: Path


def _run(argv: list[str]) -> tuple[int, str]:
    """Run certbot; its output never contains the API token (it is only in the credentials file)."""

    try:
        done = subprocess.run(  # noqa: S603 - argv is built here, hostnames are validated
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=TIMEOUT_SECONDS,
            check=False,
            env={**os.environ, "HOME": str(ACME_DIR)},
        )
    except FileNotFoundError:
        return 127, "certbot is not installed"
    except subprocess.TimeoutExpired:
        return 124, f"certbot did not finish within {TIMEOUT_SECONDS // 60} minutes"
    return done.returncode, done.stdout.decode("utf-8", errors="replace")


def summarize(output: str) -> str:
    """The lines of certbot's output that explain a failure (at most three)."""

    lines = [line.strip() for line in output.splitlines() if line.strip()]
    picked = [line for line in lines if _ERROR_LINE.match(line)]
    text = " ".join((picked or lines[-2:])[:3])
    return text[:400] or "certbot failed without a message"


class Acme:
    """Issues and renews Let's Encrypt certificates for ``cert-name`` ids."""

    def __init__(self, base: Path = ACME_DIR, run: RunFn = _run, certbot: str = CERTBOT) -> None:
        self.base = base
        self.run = run
        self.certbot = certbot
        self.credentials = base / "cloudflare.ini"

    def _write_credentials(self, api_token: str) -> None:
        self.base.mkdir(parents=True, exist_ok=True)
        self.base.chmod(0o700)
        tmp = self.credentials.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"dns_cloudflare_api_token = {api_token}\n")
        tmp.replace(self.credentials)

    def _remove_credentials(self) -> None:
        self.credentials.unlink(missing_ok=True)

    def files(self, cert_id: str) -> Issued:
        live = self.base / "config" / "live" / cert_id
        return Issued(live / "fullchain.pem", live / "privkey.pem", live / "chain.pem")

    def argv(self, cert_id: str, hostnames: list[str], email: str, *, staging: bool) -> list[str]:
        argv = [
            self.certbot,
            "certonly",
            "--non-interactive",
            "--agree-tos",
            "--dns-cloudflare",
            f"--dns-cloudflare-credentials={self.credentials}",
            f"--dns-cloudflare-propagation-seconds={PROPAGATION_SECONDS}",
            f"--config-dir={self.base / 'config'}",
            f"--work-dir={self.base / 'work'}",
            f"--logs-dir={self.base / 'logs'}",
            f"--cert-name={cert_id}",
            "--key-type=ecdsa",
            "--force-renewal",  # the app decides when to renew
        ]
        argv += [f"--email={email}"] if email else ["--register-unsafely-without-email"]
        if staging:
            argv.append("--staging")
        for name in hostnames:
            argv += ["-d", name]
        return argv

    def obtain(self, cert_id: str, hostnames: list[str], api_token: str, email: str, *, staging: bool) -> Issued:
        """Issue (or renew) ``cert_id`` for ``hostnames``; raises AcmeError with certbot's reason."""

        if not api_token:
            raise AcmeError("Set cloudflare_api_token in the Configuration tab first")
        self._write_credentials(api_token)
        try:
            _LOGGER.info("Let's Encrypt: requesting %s%s", ", ".join(hostnames), " (staging)" if staging else "")
            code, output = self.run(self.argv(cert_id, hostnames, email, staging=staging))
        finally:
            self._remove_credentials()  # only needed while certbot runs
        for line in output.splitlines():
            if line.strip():
                _LOGGER.debug("certbot: %s", line)
        if code != 0:
            raise AcmeError(f"Let's Encrypt: {summarize(output)}")
        issued = self.files(cert_id)
        if not issued.fullchain.is_file() or not issued.privkey.is_file():
            raise AcmeError("Let's Encrypt: certbot finished but left no certificate files")
        return issued
