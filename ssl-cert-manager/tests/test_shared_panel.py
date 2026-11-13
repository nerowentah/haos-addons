"""The shared ingress panel (webui.py, panel.js, panel.css, index.html) must be identical in every app."""

from __future__ import annotations

import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SHARED = ("webui.py", "web/panel.js", "web/panel.css", "web/index.html")


def _app_dirs() -> list[Path]:
    return sorted(p.parent for p in REPO.glob("*/config.yaml"))


def _locate(app: Path, name: str) -> Path | None:
    for base in (app / "rootfs" / "app", app / "rootfs" / "app" / "core"):
        path = base / name
        if path.is_file():
            return path
    if name.startswith("web/"):  # malaysia-utility-bills keeps webui.py in core/ but web/ next to it
        path = app / "rootfs" / "app" / name
        return path if path.is_file() else None
    return None


class SharedPanelTest(unittest.TestCase):
    def test_every_copy_is_identical(self) -> None:
        reference = REPO / "ssl-cert-manager" / "rootfs" / "app"
        for name in SHARED:
            expected = (reference / name).read_bytes().replace(b"\r\n", b"\n")
            for app in _app_dirs():
                path = _locate(app, name)
                with self.subTest(app=app.name, file=name):
                    self.assertIsNotNone(path, f"{app.name} has no {name}")
                    assert path is not None
                    self.assertEqual(path.read_bytes().replace(b"\r\n", b"\n"), expected, f"{path} differs")

    def test_reach_check_is_the_same_in_both_apps(self) -> None:
        for name in ("rootfs/app/reach.py", "tests/test_reach.py"):
            ours = (REPO / "ssl-cert-manager" / name).read_bytes().replace(b"\r\n", b"\n")
            theirs = (REPO / "cloudflare-tunnel" / name).read_bytes().replace(b"\r\n", b"\n")
            with self.subTest(file=name):
                self.assertEqual(ours, theirs)

    def test_upload_support_is_present(self) -> None:
        webui = (REPO / "ssl-cert-manager" / "rootfs" / "app" / "webui.py").read_text(encoding="utf-8")
        panel = (REPO / "ssl-cert-manager" / "rootfs" / "app" / "web" / "panel.js").read_text(encoding="utf-8")
        self.assertIn("def uploaded_files(", webui)
        self.assertIn("function pickFiles(", panel)


if __name__ == "__main__":
    unittest.main()
