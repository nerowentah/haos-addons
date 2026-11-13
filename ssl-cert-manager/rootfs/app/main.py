#!/usr/bin/env python3
"""Home Assistant app: SSL Cert Manager - gets, imports, renews and publishes TLS certificates."""

from __future__ import annotations

import contextlib
import copy
import datetime as dt
import json
import logging
import os
import queue
import re
import signal
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import FrameType

import acme as acme_mod
import certs
import files as files_mod
import ha_config
import origin_ca as origin_mod
import proxy as proxy_mod
import reach
from config import Config, ConfigError, apply_log_level, load_config, load_options
from cryptography import x509
from cryptography.x509.oid import NameOID
from store import Store
from supervisor import (
    Supervisor,
    SupervisorError,
    effective_targets,
    load_supervisor_token,
    merge_discovered,
    merge_s6_environment,
)
from webui import (
    PanelError,
    PanelRouter,
    PanelServer,
    SecretError,
    SecretRefs,
    SetupView,
    action,
    badge,
    external,
    initial_options,
    notice,
    row,
    stat,
    toggle,
    uploaded_files,
    wait_for_valid_config,
)

_LOGGER = logging.getLogger("ssl_cert_manager")

VERSION = os.environ.get("SSL_CERT_MANAGER_VERSION", "dev")
LOOP_SECONDS = 5
MIN_SLEEP_SECONDS = 1
OPTIONS_CHECK_SECONDS = 30
PHONE_CHECK_SECONDS = 300
EXPIRY_CHECK_SECONDS = 6 * 3600
RENEW_RETRY_SECONDS = 12 * 3600  # after a failed renewal
STORE_SAVE_SECONDS = 60
WEB_DIR = Path(__file__).resolve().parent / "web"
NOTIFICATION_ID = "ssl_cert_manager"
TUNNEL_SLUG = "cloudflare_tunnel"
TUNNEL_STATUS = Path("/ssl/cloudflare-tunnel/status.json")
TUNNEL_SETTLE_SECONDS = 45  # the tunnel app re-checks its origin when status.json changes; give it time
TUNNEL_PROBES = 3
TUNNEL_PROBE_GAP_SECONDS = 10
UPLOAD_ACCEPT = ".pem,.crt,.cer,.key,.txt"

SOURCE_LE = "letsencrypt"
SOURCE_ORIGIN = "origin_ca"
SOURCE_IMPORTED = "imported"
SOURCE_ISSUED = "issued"
SOURCE_LABEL = {
    SOURCE_LE: "Let's Encrypt",
    SOURCE_ORIGIN: "Cloudflare Origin CA",
    SOURCE_IMPORTED: "Imported",
    SOURCE_ISSUED: "Issued by your CA",
}
RENEWABLE = {SOURCE_LE, SOURCE_ISSUED}
REAL_IP_LABEL = {"off": "Not passed on", "apps": "From apps (tunnel)", "cloudflare": "From Cloudflare"}


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _iso(moment: dt.datetime) -> str:
    return moment.replace(microsecond=0).isoformat()


def _parse_iso(value: object) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(str(value))
    except ValueError:
        return None


def make_id(name: str, taken: set[str], prefix: str = "") -> str:
    """A folder-safe id from a host name, e.g. ``home-example-com`` (``-2`` when taken)."""

    base = prefix + (re.sub(r"[^a-z0-9]+", "-", name.lower().replace("*", "wildcard")).strip("-") or "cert")
    base = base[:60]
    if base in files_mod.RESERVED:
        base += "-cert"
    candidate, n = base, 2
    while candidate in taken:
        candidate, n = f"{base}-{n}", n + 1
    return candidate


def describe_reach(code: int | None) -> tuple[str, str]:  # noqa: PLR0911 - one answer per status
    """(text, tone) for a request to the public hostname through the tunnel."""

    if code in (200, 401):  # 401: Home Assistant itself answered and wants a login
        return "Reachable through the tunnel", "ok"
    if code == 400:  # noqa: PLR2004 - HTTP status
        return "Home Assistant rejects the proxy (HTTP 400): it must trust the proxy in configuration.yaml", "error"
    if code == 502:  # noqa: PLR2004 - HTTP status
        return "Tunnel up, but its connection to Home Assistant failed (HTTP 502): usually origin TLS", "error"
    if code == 526:  # noqa: PLR2004 - HTTP status
        return "Cloudflare rejected the origin certificate (HTTP 526)", "error"
    if code == 530:  # noqa: PLR2004 - HTTP status
        return "The hostname is not routed to a running tunnel (HTTP 530)", "error"
    if code is None:
        return "No answer from the public hostname", "warn"
    if code >= 500:  # noqa: PLR2004 - HTTP status
        return f"Cloudflare cannot reach Home Assistant (HTTP {code})", "error"
    return f"HTTP {code}", "warn"


def sibling_slug(own_slug: str, name: str) -> str:
    """Another app of this repository: same store prefix (``a1b2c3d4_`` or ``local_``), different name."""

    prefix, sep, _rest = own_slug.partition("_ssl_cert_manager")
    return f"{prefix}_{name}" if sep and prefix else name


class App:
    """Holds the certificates, publishes them, runs the proxy and serves the panel."""

    def __init__(  # noqa: PLR0913 - the tests inject the seams
        self,
        config: Config,
        supervisor: Supervisor,
        store: Store,
        raw_options: dict[str, object] | None = None,
        *,
        files: files_mod.Files | None = None,
        acme: acme_mod.Acme | None = None,
        origin: origin_mod.OriginCa | None = None,
        proxy: proxy_mod.Proxy | None = None,
        ha_path: Path = ha_config.CONFIG_PATH,
        probe: Callable[[str], reach.Result] = reach.probe,
        tunnel_status: Path = TUNNEL_STATUS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.raw_options = raw_options or {}  # as stored: !secret references, never their values
        self.secrets = SecretRefs()
        self.supervisor = supervisor
        self.store = store
        self.files = files or files_mod.Files()
        self.acme = acme or acme_mod.Acme()
        self.origin = origin or origin_mod.OriginCa()
        self.proxy = proxy or proxy_mod.Proxy()
        self.ha_path = ha_path
        self.probe = probe
        self.tunnel_status_path = tunnel_status
        self.sleep = sleep
        self.version = VERSION
        self.discovered: list[str] = []
        self.lock = threading.RLock()
        self.options_lock = threading.Lock()
        self.pending: tuple[dict[str, object], Config] | None = None
        self.stopping = threading.Event()
        self.wake = threading.Event()
        self.jobs: queue.Queue[tuple[str, Callable[[], str | None]]] = queue.Queue()
        self.busy: str | None = None
        self.last_job: tuple[str, str] | None = None  # (message, tone) of the last finished job
        self.need_sync = True
        self.root_note: str | None = None
        self.sync_errors: list[str] = []
        self.next_options_check = time.monotonic() + OPTIONS_CHECK_SECONDS
        self.next_phone_check = 0.0
        self.next_expiry_check = 0.0
        self.self_slug = ""
        self.self_host: str | None = None
        self.upstream = "http://homeassistant:8123"
        self._proxy_key: tuple[object, ...] | None = None

    # -- store sections ---------------------------------------------------------------
    @property
    def certs(self) -> dict[str, dict[str, object]]:
        return self.store.section("certs")  # type: ignore[return-value]

    @property
    def cas(self) -> dict[str, dict[str, object]]:
        return self.store.section("cas")  # type: ignore[return-value]

    @property
    def phones(self) -> dict[str, object]:
        return self.store.section("phones")

    def _state(self, name: str) -> dict[str, object]:
        return self.store.section(name)

    def _set(self, section: str, **values: object) -> None:
        with self.lock:
            target = self._state(section)
            for key, value in values.items():
                if value is None:
                    target.pop(key, None)
                else:
                    target[key] = value
            self.store.dirty = True

    @property
    def default_id(self) -> str | None:
        with self.lock:
            current = self.store.data.get("default")
            if isinstance(current, str) and current in self.certs:
                return current
            return next(iter(self.certs), None)

    # -- options ------------------------------------------------------------------------
    def view_config(self) -> Config:
        pending = self.pending
        return pending[1] if pending else self.config

    def apply_config(self, raw: dict[str, object], config: Config) -> None:
        old = self.config
        self.config = config
        self.raw_options = raw
        if old.log_level != config.log_level:
            _LOGGER.info("Log level set to %s", config.log_level)
            apply_log_level(config.log_level)
        proxy_options = (config.local_proxy, config.write_ssl_root, config.proxy_hsts, config.proxy_real_ip)
        if (old.local_proxy, old.write_ssl_root, old.proxy_hsts, old.proxy_real_ip) != proxy_options:
            self.need_sync = True
        if config.local_proxy and not old.local_proxy:
            self.enqueue("Checking Home Assistant's proxy settings", self._trust_proxy)
        if old.renew_days_before != config.renew_days_before:
            self.next_expiry_check = 0.0
        _LOGGER.info("Applied updated options")

    def update_options(self, mutate: Callable[[dict[str, object]], None]) -> None:
        """Change the saved options from the panel; applied on the next loop pass (any thread)."""

        with self.options_lock:
            try:
                base = self.supervisor.fetch_options()
            except SupervisorError as err:
                raise PanelError("Home Assistant is not reachable; try again") from err
            raw = copy.deepcopy(base)
            mutate(raw)
            try:
                config = load_config(self.secrets.resolve(raw))  # type: ignore[arg-type]
            except (ConfigError, SecretError) as err:
                raise PanelError(str(err)) from err
            try:
                self.supervisor.write_options(raw)
            except SupervisorError as err:
                raise PanelError("Home Assistant rejected the change; see the app log") from err
            self.pending = (raw, config)
        self.wake.set()

    def check_options(self) -> None:
        try:
            raw = self.supervisor.fetch_options()
        except SupervisorError:
            return
        if raw == self.raw_options:
            return
        try:
            config = load_config(self.secrets.resolve(raw))  # type: ignore[arg-type]
        except (ConfigError, SecretError) as err:
            _LOGGER.error("Ignoring updated options until they are fixed: %s", err)  # noqa: TRY400 - no traceback needed
            self.raw_options = raw
            return
        self.apply_config(raw, config)

    # -- phones ---------------------------------------------------------------------------
    def refresh_phones(self) -> None:
        found = self.supervisor.discover_mobile_targets()
        if found is None:
            return
        with self.lock:
            merged = merge_discovered(self.phones, found)
            self.discovered = found
            if merged != self.phones:
                self.store.data["phones"] = merged
                self.store.dirty = True

    def set_phone(self, name: str, value: bool) -> None:
        with self.lock:
            if name not in self.discovered:
                raise PanelError("That phone is no longer linked to Home Assistant; reload the page")
            self.phones[name] = value
            self.store.dirty = True

    def notify(self, title: str, message: str, suffix: str = "") -> None:
        if self.config.notify_on_failure:
            targets = effective_targets(self.discovered, self.phones)
            self.supervisor.notify(targets, title, message, f"{NOTIFICATION_ID}{suffix}")

    # -- jobs (one worker thread: certbot and the Cloudflare API can take a minute) --------
    def enqueue(self, label: str, job: Callable[[], str | None]) -> None:
        self.jobs.put((label, job))

    def _worker(self) -> None:
        while not self.stopping.is_set():
            try:
                label, job = self.jobs.get(timeout=1)
            except queue.Empty:
                continue
            self.run_job(label, job)

    def run_job(self, label: str, job: Callable[[], str | None]) -> None:
        """Run one queued job; its outcome goes to the panel's Activity card."""

        self.busy = label
        try:
            message = job()
            if message:
                self.last_job = (message, "ok")
        except (certs.CertError, acme_mod.AcmeError, origin_mod.OriginCaError, OSError) as err:
            _LOGGER.error("%s: %s", label, err)  # noqa: TRY400 - the message says it all
            self.last_job = (str(err), "error")
        except Exception:
            _LOGGER.exception("%s failed", label)
            self.last_job = (f"{label} failed; see the app log", "error")
        finally:
            self.busy = None
            self.need_sync = True
            self.wake.set()

    # -- adding certificates (worker thread) ------------------------------------------------
    def _taken(self) -> set[str]:
        return set(self.certs) | set(self.cas)

    def _record(self, cert_id: str, chain: list[x509.Certificate], source: str, **extra: object) -> None:
        summary = certs.info(chain[0])
        now = _iso(_now())
        with self.lock:
            meta = self.certs.get(cert_id, {"created": now})
            meta.update(
                {
                    "source": source,
                    "names": summary.names,
                    "info": summary.as_dict(),
                    "renewed": now,
                    "error": None,
                    "retry_at": None,
                    "expiry_notified": None,
                }
            )
            meta.update(extra)
            self.certs[cert_id] = {k: v for k, v in meta.items() if v is not None}
            if not isinstance(self.store.data.get("default"), str) or self.store.data["default"] not in self.certs:
                self.store.data["default"] = cert_id
            self.store.dirty = True

    def _letsencrypt(self, names: list[str], cert_id: str | None = None) -> str:
        cert_id = cert_id or make_id(names[0], self._taken())
        staging = self.config.acme_staging
        issued = self.acme.obtain(
            cert_id, names, self.config.cloudflare_api_token, self.config.acme_email, staging=staging
        )
        chain = self.files.save_cert_files(cert_id, issued.fullchain.read_bytes(), issued.privkey.read_bytes())
        self._record(cert_id, chain, SOURCE_LE, staging=True if staging else None)
        return f"Let's Encrypt certificate for {', '.join(names)} is ready"

    def _origin_cert(self, names: list[str]) -> str:
        cert_id = make_id(names[0], self._taken())
        key = certs.new_key()
        pem, cf_id = self.origin.create(self.config.cloudflare_api_token, names, certs.csr_pem(key, names))
        bundle = certs.parse_bundle(pem)
        chain = certs.order_chain(bundle.certs, key)
        self.files.save_cert(cert_id, chain, key)
        self._record(cert_id, chain, SOURCE_ORIGIN, origin_id=cf_id or None)
        return f"Cloudflare Origin certificate for {', '.join(names)} is ready (trusted by Cloudflare only)"

    def _import_cert(self, uploads: list[tuple[str, str]]) -> str:
        bundle = certs.parse_bundle("\n".join(text for _name, text in uploads))
        if bundle.key is None:
            raise certs.CertError("Add the private key to the upload (select the certificate and key files together)")
        chain = certs.order_chain(bundle.certs, bundle.key)
        certs.check_server_cert(chain)
        cert_id = make_id(certs.names(chain[0])[0], self._taken())
        self.files.save_cert(cert_id, chain, bundle.key)
        self._record(cert_id, chain, SOURCE_IMPORTED)
        return f"Imported the certificate for {', '.join(certs.names(chain[0]))}"

    def _import_ca(self, uploads: list[tuple[str, str]]) -> str:
        bundle = certs.parse_bundle("\n".join(text for _name, text in uploads))
        if not bundle.certs:
            raise certs.CertError("No certificate in the upload")
        if bundle.key is not None:
            ca_cert = next((c for c in bundle.certs if certs.same_key(c, bundle.key)), None)
            if ca_cert is None:
                raise certs.CertError("The private key does not belong to the CA certificate")
            certs.check_issuer(ca_cert, bundle.key)
            todo = [(ca_cert, bundle.key)]
        else:
            todo = [(c, None) for c in bundle.certs]
        added = []
        known = {str(meta.get("fingerprint")): ca_id for ca_id, meta in self.cas.items()}
        for cert, key in todo:
            summary = certs.info(cert)
            ca_id = known.get(summary.fingerprint)
            if ca_id and key is None:
                continue  # already trusted
            subject_cn = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
            label = str(subject_cn[0].value) if subject_cn else summary.subject
            ca_id = ca_id or make_id(label, self._taken(), "ca-")
            self.files.save_ca(ca_id, cert, key)
            with self.lock:
                self.cas[ca_id] = {
                    "label": label,
                    "fingerprint": summary.fingerprint,
                    "info": summary.as_dict(),
                    "can_issue": key is not None or bool(self.cas.get(ca_id, {}).get("can_issue")),
                    "created": _iso(_now()),
                }
                self.store.dirty = True
            added.append(label)
        if not added:
            return "These CA certificates are already trusted"
        how = " It can issue certificates now." if bundle.key is not None else ""
        return f"Trusted {', '.join(added)}.{how}"

    def _issue(self, ca_id: str, names: list[str], cert_id: str | None = None) -> str:
        ca_cert, ca_key = self.files.load_ca(ca_id)
        if ca_key is None:
            raise certs.CertError("This CA was imported without its private key, so it cannot issue certificates")
        leaf, key = certs.issue(ca_cert, ca_key, names)
        chain = [leaf, ca_cert]
        cert_id = cert_id or make_id(names[0], self._taken())
        self.files.save_cert(cert_id, chain, key)
        self._record(cert_id, chain, SOURCE_ISSUED, ca_id=ca_id)
        return f"Issued a certificate for {', '.join(names)}"

    def _renew(self, cert_id: str) -> str:
        with self.lock:
            meta = dict(self.certs.get(cert_id) or {})
        if not meta:
            raise certs.CertError("That certificate is gone; reload the page")
        names = [str(n) for n in meta.get("names") or []]
        try:
            if meta.get("source") == SOURCE_LE:
                return self._letsencrypt(names, cert_id)
            if meta.get("source") == SOURCE_ISSUED:
                return self._issue(str(meta.get("ca_id")), names, cert_id)
        except (certs.CertError, acme_mod.AcmeError, OSError) as err:
            retry = _now() + dt.timedelta(seconds=RENEW_RETRY_SECONDS)
            self._set_cert(cert_id, error=str(err), retry_at=_iso(retry))
            self.notify("Certificate renewal failed", f"{names[0]}: {err}", f"_{cert_id}")
            raise
        raise certs.CertError("This certificate cannot be renewed by the app; import or request a new one")

    def _set_cert(self, cert_id: str, **values: object) -> None:
        with self.lock:
            meta = self.certs.get(cert_id)
            if meta is None:
                return
            for key, value in values.items():
                if value is None:
                    meta.pop(key, None)
                else:
                    meta[key] = value
            self.store.dirty = True

    def _delete(self, cert_id: str) -> str:
        with self.lock:
            meta = self.certs.pop(cert_id, None)
            if self.store.data.get("default") == cert_id:
                self.store.data.pop("default", None)
            self.store.dirty = True
        if meta is None:
            return "Already deleted"
        self.files.delete_cert(cert_id)
        note = ""
        if meta.get("source") == SOURCE_ORIGIN and meta.get("origin_id"):
            try:
                self.origin.revoke(self.config.cloudflare_api_token, str(meta["origin_id"]))
                note = " and revoked it at Cloudflare"
            except origin_mod.OriginCaError as err:
                note = f"; revoking it at Cloudflare failed ({err}), revoke it in the dashboard"
        return f"Deleted the certificate for {', '.join(str(n) for n in meta.get('names') or [])}{note}"

    def _delete_ca(self, ca_id: str) -> str:
        with self.lock:
            meta = self.cas.pop(ca_id, None)
            self.store.dirty = True
        self.files.delete_ca(ca_id)
        return f"Removed {meta.get('label') if meta else 'the CA'} from the trusted CAs"

    # -- Home Assistant configuration (worker thread) -------------------------------------
    def _trust_proxy(self) -> str | None:
        try:
            ip = self.supervisor.self_ip()
        except SupervisorError:
            ip = None
        network = ha_config.network_for(ip)
        result = ha_config.apply(
            self.supervisor,
            lambda text: ha_config.ensure_trusted_proxy(text, network),
            ha_config.proxy_snippet(network),
            self.ha_path,
        )
        self._set("proxy_trust", status=result.status, message=result.message)
        if result.status == ha_config.STATUS_ADDED:
            self._set("ha_restart", pending=True, reason="proxy settings")
        return None if result.status == ha_config.STATUS_PRESENT else result.message

    def _ha_https(self, *, enable: bool) -> str:
        cert, key = (str(p) for p in self.files.published(files_mod.DEFAULT_DIR))
        if enable:
            if self.default_id is None:
                raise certs.CertError("Add a certificate first")
            edit = lambda text: ha_config.ensure_ssl(text, cert, key)  # noqa: E731 - short adapter
        else:
            edit = lambda text: ha_config.remove_ssl(text, cert, key)  # noqa: E731 - short adapter
        result = ha_config.apply(self.supervisor, edit, ha_config.ssl_snippet(cert, key), self.ha_path)
        done = result.status in (ha_config.STATUS_ADDED, ha_config.STATUS_REMOVED, ha_config.STATUS_PRESENT)
        self._set(
            "ha_https",
            status=result.status,
            message=result.message,
            enabled=enable if done else None,
            fingerprint=self._default_fingerprint() if enable and done else None,
        )
        if result.status in (ha_config.STATUS_ADDED, ha_config.STATUS_REMOVED):
            self._set("ha_restart", pending=True, reason="HTTPS settings")
        if not done:
            raise certs.CertError(result.message)
        return result.message

    def _restart_ha(self) -> str:
        try:
            self.supervisor.restart_core()
        except SupervisorError as err:
            raise certs.CertError(f"Home Assistant did not restart: {err}") from err
        self._set("ha_restart", pending=None, reason=None)
        if self._state("ha_https").get("enabled"):
            self._set("ha_https", fingerprint=self._default_fingerprint())
        return "Restarting Home Assistant..."

    def _default_fingerprint(self) -> str | None:
        default = self.default_id
        if default is None:
            return None
        info = self.certs.get(default, {}).get("info")
        return str(info.get("fingerprint")) if isinstance(info, dict) else None

    # -- the Cloudflare Tunnel app ---------------------------------------------------------
    def tunnel_status(self) -> dict[str, object] | None:
        """What the Cloudflare Tunnel app last wrote to /ssl (hostname, origin check)."""

        try:
            data = json.loads(self.tunnel_status_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _tunnel_check(self, *, settle: bool) -> str | None:
        """Is the Cloudflare Tunnel app running, and does Home Assistant answer through it?"""

        slug = sibling_slug(self.self_slug, TUNNEL_SLUG)
        try:
            state = self.supervisor.addon_state(slug)
        except SupervisorError as err:
            self._set("tunnel_check", state="error", message=f"Supervisor: {err}", checked=_iso(_now()))
            return None
        if state is None:
            self._set(
                "tunnel_check", state="absent", message="Cloudflare Tunnel is not installed", checked=_iso(_now())
            )
            return None
        if state != "started":
            self._set("tunnel_check", state="stopped", message=f"Cloudflare Tunnel is {state}", checked=_iso(_now()))
            return None
        if settle:
            self.sleep(TUNNEL_SETTLE_SECONDS)
        status = self.tunnel_status() or {}
        hostname = str(status.get("hostname") or "")
        if not hostname:
            self._set(
                "tunnel_check",
                state="no_hostname",
                message="Cloudflare Tunnel has no public hostname yet",
                checked=_iso(_now()),
            )
            return None
        text, tone = "No answer", "warn"
        for attempt in range(TUNNEL_PROBES):
            result = self.probe(hostname)
            text, tone = reach.explain(result, describe_reach)
            if tone == "ok":
                break
            if attempt < TUNNEL_PROBES - 1:
                self.sleep(TUNNEL_PROBE_GAP_SECONDS)
        self._set("tunnel_check", state=tone, message=text, hostname=hostname, checked=_iso(_now()))
        if tone == "error":
            self.notify("Cloudflare Tunnel check failed", f"{hostname}: {text}", "_tunnel")
        return f"{hostname}: {text}"

    # -- publishing (loop thread) ---------------------------------------------------------
    def trust_pem(self) -> str:
        """The trust bundle: Cloudflare's Origin CA roots plus every imported CA."""

        parts = [origin_mod.roots_pem()]
        for ca_id in list(self.cas):
            try:
                cert, _key = self.files.load_ca(ca_id)
            except (OSError, certs.CertError):
                continue
            parts.append(certs.cert_pem(cert).decode("ascii"))
        return "".join(parts)

    def status_document(self) -> dict[str, object]:
        """``/ssl/ssl-cert-manager/status.json``: what other apps (Cloudflare Tunnel) need to know."""

        default = self.default_id
        listed = []
        for cert_id, meta in self.certs.items():
            fullchain, privkey = self.files.published(cert_id)
            info = meta.get("info") if isinstance(meta.get("info"), dict) else {}
            listed.append(
                {
                    "id": cert_id,
                    "names": list(meta.get("names") or []),  # type: ignore[call-overload]
                    "source": meta.get("source"),
                    "not_after": info.get("not_after"),  # type: ignore[union-attr]
                    "fullchain": str(fullchain),
                    "privkey": str(privkey),
                }
            )
        default_names = list(self.certs[default].get("names") or []) if default else []  # type: ignore[call-overload]
        fullchain, privkey = self.files.published(files_mod.DEFAULT_DIR)
        return {
            "version": 1,
            "app_version": self.version,
            "trust_bundle": str(self.files.trust_bundle),
            "default": {"id": default, "names": default_names, "fullchain": str(fullchain), "privkey": str(privkey)}
            if default
            else None,
            "certificates": listed,
            "proxy": {
                "enabled": bool(self.config.local_proxy and default),
                "host": self.self_host,
                "port": proxy_mod.PORT,
                "upstream": self.upstream,
            },
            "ha_https": bool(self._state("ha_https").get("enabled")),
        }

    def sync(self) -> None:
        """Publish /ssl, (re)configure the proxy, and check the tunnel when something it uses changed."""

        self.need_sync = False
        with self.lock:
            cert_ids = list(self.certs)
            default = self.default_id
            status = self.status_document()
            trust = self.trust_pem()
        result = self.files.sync(cert_ids, default, trust, status, write_root=self.config.write_ssl_root)
        self.root_note = result.root_note
        self.sync_errors = result.errors
        proxy_changed = self._configure_proxy(default)
        https = self._state("ha_https")
        if https.get("enabled") and https.get("fingerprint") != self._default_fingerprint():
            self._set("ha_restart", pending=True, reason="the renewed certificate")
            self._set("ha_https", fingerprint=self._default_fingerprint())
        if result.changed or proxy_changed:
            self.enqueue("Checking the Cloudflare Tunnel", lambda: self._tunnel_check(settle=True))

    def _configure_proxy(self, default: str | None) -> bool:
        """Start, reload or stop nginx; True when what it serves changed."""

        if not self.config.local_proxy or default is None:
            changed = self._proxy_key is not None
            self._proxy_key = None
            self.proxy.stop()
            return changed
        with contextlib.suppress(SupervisorError, ValueError):
            self.upstream = proxy_mod.upstream_for(self.supervisor.core_info())
        fullchain, privkey = self.files.published(files_mod.DEFAULT_DIR)
        default_site = proxy_mod.Site(list(self.certs[default].get("names") or []), str(fullchain), str(privkey))  # type: ignore[call-overload]
        others = []
        for cert_id, meta in self.certs.items():
            if cert_id == default:
                continue
            chain_path, key_path = self.files.published(cert_id)
            others.append(proxy_mod.Site(list(meta.get("names") or []), str(chain_path), str(key_path)))  # type: ignore[call-overload]
        fingerprints = tuple(str((m.get("info") or {}).get("fingerprint")) for m in self.certs.values())  # type: ignore[union-attr]
        key = (default, fingerprints, self.upstream)
        ranges: list[str] = []
        if self.config.proxy_real_ip == proxy_mod.REAL_IP_CLOUDFLARE:
            try:
                ranges = proxy_mod.cloudflare_ranges()
            except OSError as err:
                _LOGGER.warning("Cloudflare's IP ranges are unreadable (%s); trusting the apps network only", err)
        text = proxy_mod.render(
            default_site,
            others,
            self.upstream,
            hsts=self.config.proxy_hsts,
            real_ip=self.config.proxy_real_ip,
            ranges=ranges,
        )
        if self.proxy.configure(text):
            changed = key != self._proxy_key
            self._proxy_key = key
            return changed
        return False

    # -- expiry (loop thread) ---------------------------------------------------------------
    def check_expiry(self, now: dt.datetime | None = None) -> None:
        now = now or _now()
        limit = self.config.renew_days_before
        with self.lock:
            items = [(cert_id, dict(meta)) for cert_id, meta in self.certs.items()]
        for cert_id, meta in items:
            info = meta.get("info") if isinstance(meta.get("info"), dict) else {}
            not_after = _parse_iso(info.get("not_after"))  # type: ignore[union-attr]
            if not_after is None:
                continue
            days = int((not_after - now).total_seconds() // 86400)
            name = str((meta.get("names") or [cert_id])[0])  # type: ignore[index]
            if days > limit:
                continue
            if meta.get("source") in RENEWABLE:
                retry = _parse_iso(meta.get("retry_at"))
                if retry is None or retry <= now:
                    self.enqueue(f"Renewing {name}", lambda cert_id=cert_id: self._renew(cert_id))
                continue
            stage = "expired" if days < 0 else "soon"
            if meta.get("expiry_notified") != stage:
                when = "has expired" if days < 0 else f"expires in {days} day{'s' if days != 1 else ''}"
                self.notify(
                    "Certificate expiring",
                    f"{name} {when}. The app cannot renew this one; import or request a new certificate.",
                    f"_{cert_id}",
                )
                self._set_cert(cert_id, expiry_notified=stage)

    # -- panel callbacks (HTTP thread) ---------------------------------------------------------
    def panel_toggle(self, toggle_id: str, value: bool) -> None:
        kind, _, name = toggle_id.partition(":")
        if kind == "phone" and name:
            self.set_phone(name, value)
            return
        if toggle_id in ("local_proxy", "write_ssl_root", "acme_staging", "notify_on_failure"):
            self.update_options(lambda raw: raw.__setitem__(toggle_id, value))
            return
        raise PanelError("Unknown setting")

    def _names(self, args: dict[str, object], *, public: bool) -> list[str]:
        try:
            names = certs.parse_names(args.get("text"))
            return certs.public_names(names) if public else names
        except certs.CertError as err:
            raise PanelError(str(err)) from err

    def _need_token(self) -> None:
        if not self.config.cloudflare_api_token:
            raise PanelError("Set cloudflare_api_token in the Configuration tab first")

    def _known(self, table: dict[str, dict[str, object]], item_id: str) -> str:
        if item_id not in table:
            raise PanelError("That item is gone; reload the page")
        return item_id

    def panel_action(self, action_id: str, args: dict[str, object]) -> str | None:  # noqa: PLR0911, PLR0912 - one branch per button
        kind, _, item = action_id.partition(":")  # jobs queue up; the panel shows the one running now
        if kind == "letsencrypt":
            self._need_token()
            names = self._names(args, public=True)
            self.enqueue(f"Requesting a Let's Encrypt certificate for {names[0]}", lambda: self._letsencrypt(names))
            return "Requesting the certificate (about a minute)..."
        if kind == "origin":
            self._need_token()
            names = self._names(args, public=True)
            self.enqueue(f"Requesting a Cloudflare Origin certificate for {names[0]}", lambda: self._origin_cert(names))
            return "Requesting the certificate..."
        if kind == "import_cert":
            uploads = uploaded_files(args)
            self.enqueue("Importing the certificate", lambda: self._import_cert(uploads))
            return "Importing..."
        if kind == "import_ca":
            uploads = uploaded_files(args)
            self.enqueue("Importing the CA", lambda: self._import_ca(uploads))
            return "Importing..."
        if kind == "issue":
            ca_id = self._known(self.cas, item)
            if not self.cas[ca_id].get("can_issue"):
                raise PanelError("This CA was imported without its private key")
            names = self._names(args, public=False)
            self.enqueue(f"Issuing a certificate for {names[0]}", lambda: self._issue(ca_id, names))
            return "Issuing..."
        if kind == "renew":
            cert_id = self._known(self.certs, item)
            self.enqueue(f"Renewing {cert_id}", lambda: self._renew(cert_id))
            return "Renewing..."
        if kind == "default":
            cert_id = self._known(self.certs, item)
            with self.lock:
                self.store.data["default"] = cert_id
                self.store.dirty = True
            self.need_sync = True
            self.wake.set()
            return "Default certificate changed"
        if kind == "delete":
            cert_id = self._known(self.certs, item)
            self.enqueue(f"Deleting {cert_id}", lambda: self._delete(cert_id))
            return "Deleting..."
        if kind == "delete_ca":
            ca_id = self._known(self.cas, item)
            self.enqueue(f"Removing {ca_id}", lambda: self._delete_ca(ca_id))
            return "Removing..."
        if kind == "ha_https_on":
            self.enqueue("Turning on HTTPS in Home Assistant", lambda: self._ha_https(enable=True))
            return "Updating configuration.yaml..."
        if kind == "ha_https_off":
            self.enqueue("Turning off HTTPS in Home Assistant", lambda: self._ha_https(enable=False))
            return "Updating configuration.yaml..."
        if kind == "trust_proxy":
            self.enqueue("Checking Home Assistant's proxy settings", self._trust_proxy)
            return "Checking..."
        if kind == "restart_ha":
            self.enqueue("Restarting Home Assistant", self._restart_ha)
            return "Restarting Home Assistant..."
        if kind == "tunnel_check":
            self.enqueue("Checking the Cloudflare Tunnel", lambda: self._tunnel_check(settle=False))
            return "Checking the tunnel..."
        raise PanelError("Unknown action")

    # -- panel view model -------------------------------------------------------------------------
    def _cert_card(
        self, cert_id: str, meta: dict[str, object], default: str | None, now: dt.datetime
    ) -> dict[str, object]:
        info = meta.get("info") if isinstance(meta.get("info"), dict) else {}
        not_after = _parse_iso(info.get("not_after"))  # type: ignore[union-attr]
        days = int((not_after - now).total_seconds() // 86400) if not_after else None
        names = [str(n) for n in meta.get("names") or []]  # type: ignore[union-attr]
        source = str(meta.get("source") or "")
        if days is None:
            chip = badge("Unknown", "warn")
        elif days < 0:
            chip = badge("Expired", "error")
        elif days <= self.config.renew_days_before:
            chip = badge(f"{days} days left", "warn")
        else:
            chip = badge(f"{days} days left", "ok")
        label = SOURCE_LABEL.get(source, source)
        if meta.get("staging"):
            label += " (staging: not trusted)"
        fullchain, privkey = self.files.published(cert_id)
        actions = []
        if cert_id != default:
            actions.append(action(f"default:{cert_id}", "Make default", icon="mdi:star"))
        if source in RENEWABLE:
            actions.append(action(f"renew:{cert_id}", "Renew now", icon="mdi:refresh"))
        revoke = " It is also revoked at Cloudflare." if source == SOURCE_ORIGIN else ""
        actions.append(
            action(
                f"delete:{cert_id}",
                "Delete",
                confirm=f"Delete the certificate for {', '.join(names)}?{revoke} Apps using its files stop working.",
                danger=True,
                icon="mdi:delete",
            )
        )
        card: dict[str, object] = {
            "title": names[0] if names else cert_id,
            "icon": "mdi:star-circle" if cert_id == default else "mdi:certificate",
            "subtitle": label + (" · default" if cert_id == default else ""),
            "badge": chip,
            "stats": [
                stat("Expires", f"{not_after:%Y-%m-%d}" if not_after else "-"),
                stat("Issuer", _short_dn(str(info.get("issuer") or "-"))),  # type: ignore[union-attr]
                stat("Renewal", "Automatic" if source in RENEWABLE else "Manual"),
            ],
            "links": [
                row(", ".join(names), "Names it covers", icon="mdi:web"),
                row(str(fullchain), f"Certificate · key: {privkey.name} (same folder)", icon="mdi:file-certificate"),
            ],
            "actions": actions,
        }
        if meta.get("error"):
            card["notice"] = notice(f"Last renewal failed: {meta['error']}", "error")
        return card

    def _add_card(self, config: Config) -> dict[str, object]:
        token = bool(config.cloudflare_api_token)
        reason = "Set cloudflare_api_token in the Configuration tab first"
        card: dict[str, object] = {
            "title": "Add a certificate",
            "icon": "mdi:plus-circle",
            "subtitle": "Let's Encrypt and Cloudflare Origin use your Cloudflare API token; imports are PEM files",
            "links": [
                row(
                    "Let's Encrypt",
                    "Trusted by every browser. Needs a name under your Cloudflare domain (wildcards work)",
                    icon="mdi:certificate",
                ),
                row(
                    "Cloudflare Origin",
                    "15 years, trusted by Cloudflare only: for the tunnel's connection to Home Assistant",
                    icon="mdi:cloud-lock",
                ),
                row(
                    "Import", "A certificate with its private key (one or more .pem/.crt/.key files)", icon="mdi:upload"
                ),
            ],
            "actions": [
                action(
                    "letsencrypt",
                    "Let's Encrypt",
                    prompt="Names for the certificate, comma separated (e.g. home.example.com)",
                    disabled=not token,
                    reason=reason,
                    icon="mdi:certificate",
                ),
                action(
                    "origin",
                    "Cloudflare Origin",
                    prompt="Names for the Origin certificate, comma separated (e.g. home.example.com)",
                    disabled=not token,
                    reason=reason,
                    icon="mdi:cloud-lock",
                ),
                action("import_cert", "Import certificate", upload=UPLOAD_ACCEPT, icon="mdi:upload"),
            ],
        }
        if not token:
            card["notice"] = notice(
                "To request certificates, create a Cloudflare API token (My Profile → API Tokens) with Zone → Zone → Read, "
                "Zone → DNS → Edit and Zone → SSL and Certificates → Edit for your domain, and paste it into "
                "cloudflare_api_token in the Configuration tab. Imports work without it.",
                "neutral",
            )
        elif config.acme_staging:
            card["notice"] = notice(
                "Let's Encrypt staging is on: new certificates are for testing and not trusted.", "warn"
            )
        return card

    def _ca_cards(self, now: dt.datetime) -> list[dict[str, object]]:
        cards = []
        for ca_id, meta in self.cas.items():
            info = meta.get("info") if isinstance(meta.get("info"), dict) else {}
            not_after = _parse_iso(info.get("not_after"))  # type: ignore[union-attr]
            expired = not_after is not None and not_after <= now
            actions = []
            if meta.get("can_issue"):
                actions.append(
                    action(
                        f"issue:{ca_id}",
                        "Issue certificate",
                        prompt="Names and IP addresses, comma separated (e.g. homeassistant.local, 192.168.1.10)",
                        prompt_value="homeassistant.local",
                        disabled=expired,
                        reason="The CA has expired",
                        icon="mdi:certificate",
                    )
                )
            actions.append(
                action(
                    f"delete_ca:{ca_id}",
                    "Remove",
                    confirm="Remove this CA from the trust bundle? Certificates it issued stay, but are no longer renewed.",
                    danger=True,
                    icon="mdi:delete",
                )
            )
            cards.append(
                {
                    "title": str(meta.get("label") or ca_id),
                    "icon": "mdi:shield-key" if meta.get("can_issue") else "mdi:shield-check",
                    "subtitle": "Can issue certificates" if meta.get("can_issue") else "Trusted (certificate only)",
                    "badge": badge("Expired", "error") if expired else badge("Valid", "ok"),
                    "stats": [
                        stat("Expires", f"{not_after:%Y-%m-%d}" if not_after else "-"),
                        stat("Fingerprint", str(meta.get("fingerprint") or "")[:23] + "…"),
                    ],
                    "actions": actions,
                }
            )
        cards.append(
            {
                "title": "Trust bundle",
                "icon": "mdi:shield-lock",
                "subtitle": "Cloudflare's Origin CA roots plus the CAs above, for apps that check certificates",
                "links": [
                    row(
                        str(self.files.trust_bundle),
                        f"{len(self.cas) + 2} CA certificates",
                        icon="mdi:file-certificate",
                    )
                ],
                "actions": [action("import_ca", "Import CA", upload=UPLOAD_ACCEPT, icon="mdi:upload")],
                "notice": notice(
                    "Upload a CA certificate to trust it. Upload it together with its private key to issue "
                    "certificates for local names and IP addresses from it.",
                    "neutral",
                ),
            }
        )
        return cards

    def _proxy_card(self, config: Config, default: str | None) -> dict[str, object]:
        running = self.proxy.running
        if not config.local_proxy:
            chip = badge("Off", "off")
        elif default is None:
            chip = badge("Needs a certificate", "warn")
        elif self.proxy.error:
            chip = badge("Error", "error")
        else:
            chip = badge("Running" if running else "Starting", "ok" if running else "neutral")
        names = []
        for meta in self.certs.values():
            names += [str(n) for n in meta.get("names") or [] if "*" not in str(n)]  # type: ignore[union-attr]
        card: dict[str, object] = {
            "title": "HTTPS proxy",
            "icon": "mdi:lock",
            "subtitle": f"https://<name>:{proxy_mod.PORT} in front of Home Assistant; http:// keeps working",
            "badge": chip,
            "stats": [
                stat("Port", proxy_mod.PORT),
                stat("Forwards to", self.upstream.removeprefix("http://")),
                stat("HSTS", "On" if config.proxy_hsts else "Off"),
                stat("Client IP", REAL_IP_LABEL.get(config.proxy_real_ip, config.proxy_real_ip)),
            ],
            "toggles": [
                toggle(
                    "local_proxy",
                    "HTTPS proxy",
                    config.local_proxy,
                    "Serve every certificate here on port 8443 (picked by name), forwarding to Home Assistant",
                    icon="mdi:lock",
                )
            ],
        }
        if config.local_proxy and running:
            card["links"] = [
                external(f"https://{name}:{proxy_mod.PORT}", f"https://{name}:{proxy_mod.PORT}", "Open", icon="mdi:web")
                for name in dict.fromkeys(names)
            ][:6]
        trust = self._state("proxy_trust")
        if self.proxy.error and config.local_proxy:
            card["notice"] = notice(self.proxy.error, "error")
        elif config.local_proxy and trust.get("status") in (
            ha_config.STATUS_MANUAL,
            ha_config.STATUS_INVALID,
            ha_config.STATUS_ERROR,
        ):
            card["notice"] = notice(str(trust.get("message")), "warn")
            card["actions"] = [action("trust_proxy", "Check again", icon="mdi:refresh")]
        elif config.local_proxy:
            card["notice"] = notice(
                "Point the names at this Home Assistant in your local DNS (router or Pi-hole) to use them at home.",
                "neutral",
            )
        return card

    def _ha_https_card(self, default: str | None) -> dict[str, object]:
        https = self._state("ha_https")
        enabled = bool(https.get("enabled"))
        restart = self._state("ha_restart")
        fullchain, privkey = self.files.published(files_mod.DEFAULT_DIR)
        card: dict[str, object] = {
            "title": "Home Assistant itself on HTTPS",
            "icon": "mdi:home-lock",
            "subtitle": "Optional: ssl_certificate in configuration.yaml. Most setups only need the proxy",
            "badge": badge("On", "ok") if enabled else badge("Off", "off"),
            "links": [row(str(fullchain), f"Certificate · key: {privkey}", icon="mdi:file-certificate")],
        }
        if enabled:
            card["actions"] = [
                action(
                    "ha_https_off",
                    "Turn off",
                    confirm="Remove ssl_certificate and ssl_key from configuration.yaml? Home Assistant serves http:// again after a restart.",
                    icon="mdi:lock-open",
                )
            ]
        else:
            card["actions"] = [
                action(
                    "ha_https_on",
                    "Turn on",
                    confirm=(
                        "Make Home Assistant serve HTTPS only? After the restart every http:// address stops working, "
                        "including Companion apps and integrations set to http://. The default certificate must cover "
                        "the name you use. The file is backed up and checked first."
                    ),
                    danger=True,
                    disabled=default is None,
                    reason="Add a certificate first",
                    icon="mdi:lock",
                )
            ]
        message = https.get("message")
        if restart.get("pending"):
            card["notice"] = notice(
                f"Restart Home Assistant to apply the {restart.get('reason') or 'changes'}.", "warn"
            )
            card["actions"].append(  # type: ignore[union-attr]
                action(
                    "restart_ha",
                    "Restart Home Assistant",
                    confirm="Restart Home Assistant now? It is unavailable for a minute or two.",
                    icon="mdi:restart",
                )
            )
        elif message and https.get("status") not in (
            ha_config.STATUS_ADDED,
            ha_config.STATUS_REMOVED,
            ha_config.STATUS_PRESENT,
        ):
            card["notice"] = notice(str(message), "error")
        return card

    def _tunnel_card(self) -> dict[str, object]:
        check = self._state("tunnel_check")
        status = self.tunnel_status() or {}
        verify = status.get("verify") if isinstance(status.get("verify"), dict) else {}
        state = str(check.get("state") or "")
        chip = {
            "ok": badge("Reachable", "ok"),
            "error": badge("Failing", "error"),
            "warn": badge("No answer", "warn"),
            "absent": badge("Not installed", "off"),
            "stopped": badge("Stopped", "warn"),
            "no_hostname": badge("Not set up", "neutral"),
        }.get(state, badge("Not checked", "neutral"))
        links = []
        if check.get("message"):
            checked = _parse_iso(check.get("checked"))
            when = f" · {checked.astimezone():%H:%M}" if checked else ""
            links.append(row(str(check["message"]), f"This app's check{when}", icon="mdi:web"))
        if verify:
            links.append(
                row(
                    str(verify.get("message") or verify.get("state")),  # type: ignore[union-attr]
                    "Cloudflare Tunnel's own origin check",
                    icon="mdi:cloud-check-outline",
                )
            )
        if status.get("origin"):
            links.append(row(str(status["origin"]), "Where the tunnel sends traffic", icon="mdi:lan"))
        return {
            "title": "Cloudflare Tunnel",
            "icon": "mdi:cloud-lock",
            "subtitle": "Checked after every certificate change: does Home Assistant still answer through the tunnel?",
            "badge": chip,
            "links": links,
            "actions": [action("tunnel_check", "Check now", icon="mdi:refresh")],
        }

    def panel_state(self) -> dict[str, object]:
        config = self.view_config()
        now = _now()
        with self.lock:
            default = self.default_id
            cert_cards = [self._cert_card(cid, meta, default, now) for cid, meta in self.certs.items()]
            ca_cards = self._ca_cards(now)
        status_card: dict[str, object] | None = None
        if self.busy or self.last_job or self.root_note or self.sync_errors:
            status_card = {"title": "Activity", "icon": "mdi:progress-clock"}
            if self.busy:
                status_card["badge"] = badge("Working", "neutral")
                status_card["notice"] = notice(f"{self.busy}...", "neutral")
            elif self.last_job:
                status_card["notice"] = notice(*self.last_job)
            extra = [row(note, "", icon="mdi:alert-circle") for note in [self.root_note, *self.sync_errors] if note]
            if extra:
                status_card["links"] = extra
        settings = {
            "title": "Settings",
            "icon": "mdi:cog",
            "subtitle": "The Cloudflare API token and e-mail live in the Configuration tab",
            "stats": [
                stat("Cloudflare token", "Set" if config.cloudflare_api_token else "Not set"),
                stat("Renew before", config.renew_days_before, "days"),
                stat("Log level", config.log_level),
            ],
            "toggles": [
                toggle(
                    "notify_on_failure",
                    "Notifications",
                    config.notify_on_failure,
                    "Renewal failures, expiring certificates and tunnel check failures",
                    icon="mdi:bell",
                ),
                toggle(
                    "write_ssl_root",
                    "Also write /ssl/fullchain.pem",
                    config.write_ssl_root,
                    "The default certificate under the names most apps expect (never replaces files from elsewhere)",
                    icon="mdi:file-certificate",
                ),
                toggle(
                    "acme_staging",
                    "Let's Encrypt staging",
                    config.acme_staging,
                    "For testing: untrusted certificates, much higher rate limits",
                    icon="mdi:flask",
                ),
            ],
        }
        phones = {
            "title": "Phones",
            "icon": "mdi:cellphone",
            "subtitle": "Companion apps linked to this Home Assistant (found automatically)",
            "toggles": [
                toggle(
                    f"phone:{name}",
                    name.removeprefix("mobile_app_").replace("_", " "),
                    self.phones.get(name, True) is not False,
                    name,
                    disabled=not config.notify_on_failure,
                    icon="mdi:cellphone",
                )
                for name in sorted(self.discovered)
            ],
            "empty": "No mobile apps found. Without phones a persistent notification is used.",
        }
        top = [status_card] if status_card else []
        sections = [
            {"title": "Certificates", "icon": "mdi:certificate", "cards": [*top, *cert_cards, self._add_card(config)]},
            {
                "title": "Local secure access",
                "icon": "mdi:lock",
                "cards": [self._proxy_card(config, default), self._ha_https_card(default)],
            },
            {"title": "Cloudflare Tunnel", "icon": "mdi:cloud-lock", "cards": [self._tunnel_card()]},
            {"title": "Certificate authorities", "icon": "mdi:shield-lock", "cards": ca_cards},
            {"title": "Settings", "icon": "mdi:cog", "cards": [settings, phones]},
        ]
        return {
            "title": "SSL Cert Manager",
            "subtitle": f"Version {self.version} · {len(cert_cards)} certificate{'s' if len(cert_cards) != 1 else ''}",
            "sections": sections,
        }

    # -- loop -----------------------------------------------------------------------------------
    def _identify(self) -> None:
        try:
            info = self.supervisor.self_info()
        except SupervisorError:
            return
        self.self_slug = str(info.get("slug") or "")
        host = info.get("hostname")
        self.self_host = str(host) if host else None

    def _tick(self) -> None:
        with self.options_lock:
            pending, self.pending = self.pending, None
        if pending is not None:
            self.apply_config(*pending)
        now = time.monotonic()
        if now >= self.next_options_check:
            self.next_options_check = now + OPTIONS_CHECK_SECONDS
            self.check_options()
        if now >= self.next_phone_check:
            self.next_phone_check = now + PHONE_CHECK_SECONDS
            self.refresh_phones()
        if self.need_sync and self.busy is None:
            self.sync()
        self.proxy.poll()
        if now >= self.next_expiry_check and self.busy is None and self.jobs.empty():
            self.next_expiry_check = now + EXPIRY_CHECK_SECONDS
            self.check_expiry()

    def run(self) -> None:
        self._identify()
        if self.config.local_proxy and "status" not in self._state("proxy_trust"):
            self.enqueue("Checking Home Assistant's proxy settings", self._trust_proxy)
        worker = threading.Thread(target=self._worker, daemon=True, name="jobs")
        worker.start()
        last_save = time.monotonic()
        try:
            while not self.stopping.is_set():
                self.wake.clear()
                self._tick()
                if time.monotonic() - last_save >= STORE_SAVE_SECONDS or self.store.dirty:
                    self.store.save()
                    last_save = time.monotonic()
                self.wake.wait(LOOP_SECONDS)
        finally:
            self.proxy.stop()
            self.store.save()
            _LOGGER.info("Stopped")


def _short_dn(dn: str) -> str:
    """``CN=R11,O=Let's Encrypt,C=US`` -> ``R11 (Let's Encrypt)``."""

    parts = dict(item.split("=", 1) for item in re.split(r",(?=[A-Z]+=)", dn) if "=" in item)
    cn, org = parts.get("CN"), parts.get("O")
    if cn and org and org != cn:
        return f"{cn} ({org})"
    return cn or org or dn


SETUP_HELP = (
    "Open Settings → Apps → SSL Cert Manager → Configuration, fix the option named above and save. "
    "This page picks up the change within 15 seconds; no restart needed."
)
SETUP_EXAMPLE = """# Everything is optional; a Cloudflare API token enables Let's Encrypt and Origin certificates:
cloudflare_api_token: "!secret cloudflare_token"
acme_email: you@example.com
renew_days_before: 30"""


def _load_config(
    supervisor: Supervisor, secrets: SecretRefs, router: PanelRouter, stop: threading.Event
) -> tuple[dict[str, object], Config] | None:
    raw_options = initial_options(load_options, supervisor.fetch_options, secrets)

    def validate(raw: dict[str, object]) -> Config:
        return load_config(secrets.resolve(raw))  # type: ignore[arg-type]

    try:
        return raw_options, validate(raw_options)
    except (ConfigError, SecretError) as err:
        _LOGGER.error("Configuration problem: %s", err)  # noqa: TRY400 - no traceback needed
        view = SetupView("SSL Cert Manager", str(err), SETUP_HELP, SETUP_EXAMPLE)
        router.target = view
        result = wait_for_valid_config(supervisor.fetch_options, validate, view, stop)
        if result is None:
            return None
        _LOGGER.info("Configuration is valid now; starting")
        return result  # type: ignore[return-value]


def main() -> None:
    merge_s6_environment()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    _LOGGER.info("Starting SSL Cert Manager %s", VERSION)
    token = load_supervisor_token()
    if not token:
        _LOGGER.error("No supervisor token; cannot reach Home Assistant")
        raise SystemExit(1)
    supervisor = Supervisor(token)
    stop = threading.Event()
    router = PanelRouter()

    def _stop(signum: int, _frame: FrameType | None) -> None:
        _LOGGER.info("Received signal %d; shutting down", signum)
        stop.set()
        if isinstance(router.target, App):
            router.target.stopping.set()
            router.target.wake.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    server = PanelServer(WEB_DIR, router.get_state, router.on_toggle, router.on_action)
    server.start()
    try:
        secrets = SecretRefs()
        loaded = _load_config(supervisor, secrets, router, stop)
        if loaded is None:
            return
        raw_options, config = loaded
        apply_log_level(config.log_level)
        store = Store()
        store.load()
        app = App(config, supervisor, store, raw_options)
        app.secrets = secrets
        router.target = app
        if stop.is_set():
            return
        app.run()
    finally:
        server.stop()


if __name__ == "__main__":
    main()
