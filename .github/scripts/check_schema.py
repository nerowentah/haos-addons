"""Check every app's config.yaml ``schema`` against the Supervisor's element syntax.

The Supervisor silently drops an app from the store when one schema value does
not parse, so a typo hides the whole app. The regex below is copied from the
Supervisor (``supervisor/apps/options.py``: ``RE_SCHEMA_ELEMENT``).

Usage: python .github/scripts/check_schema.py [app_dir ...]  (default: every app)
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterator
from pathlib import Path

import yaml

RE_SCHEMA_ELEMENT = re.compile(
    r"^(?:"
    r"|bool"
    r"|email"
    r"|url"
    r"|port"
    r"|device(?:\((?P<filter>subsystem=[a-z]+)\))?"
    r"|str(?:\((?P<s_min>\d+)?,(?P<s_max>\d+)?\))?"
    r"|password(?:\((?P<p_min>\d+)?,(?P<p_max>\d+)?\))?"
    r"|int(?:\((?P<i_min>-?\d+)?,(?P<i_max>-?\d+)?\))?"
    r"|float(?:\((?P<f_min>-?\d*\.?\d+)?,(?P<f_max>-?\d*\.?\d+)?\))?"
    r"|match\((?P<match>.*)\)"
    r"|list\((?P<list>.+)\)"
    r")\??$"
)


def _leaves(node: object, path: str) -> Iterator[tuple[str, object]]:
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _leaves(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _leaves(value, f"{path}[{index}]")
    else:
        yield path, node


def check(config_file: Path) -> list[str]:
    schema = (yaml.safe_load(config_file.read_text(encoding="utf-8")) or {}).get("schema")
    if not schema:
        return []
    errors = []
    for path, value in _leaves(schema, "schema"):
        found = RE_SCHEMA_ELEMENT.match(str(value)) if isinstance(value, str) else None
        if found is None:
            errors.append(f"{config_file}: {path} = {value!r} is not a valid Supervisor schema element")
            continue
        if found.group("match") is not None:
            try:
                re.compile(found.group("match"))
            except re.error as err:
                errors.append(f"{config_file}: {path} has an invalid match() regex: {err}")
    return errors


def main(argv: list[str]) -> int:
    dirs = [Path(a) for a in argv] or sorted(p.parent for p in Path().glob("*/config.yaml"))
    errors = [e for d in dirs for e in check(d / "config.yaml")]
    for error in errors:
        sys.stdout.write(f"::error::{error}\n")
    sys.stdout.write(f"Checked {len(dirs)} app(s): {'FAIL' if errors else 'OK'}\n")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
