"""Print a CycloneDX SBOM of the Python distributions installed where this runs.

Run inside an app image by .github/workflows/scan-images.yaml. Trivy's image
scan leaves out Python modules installed by ``apk`` (it reports the Alpine
package instead), so PyPI advisories would never be matched against them; the
SBOM gives Trivy the PyPI names and versions. Standard library only.

Usage: python3 -I - <name> < .github/scripts/pylibs_sbom.py
"""

from __future__ import annotations

import json
import re
import sys
from importlib import metadata


def _components() -> list[dict[str, str]]:
    versions: dict[str, str] = {}
    for dist in metadata.distributions():
        name = dist.metadata["Name"]
        if name and dist.version:
            # PEP 503 name, the spelling a pypi purl uses.
            versions[re.sub(r"[-_.]+", "-", name).lower()] = dist.version
    return [
        {
            "type": "library",
            "bom-ref": f"pkg:pypi/{name}@{version}",
            "name": name,
            "version": version,
            "purl": f"pkg:pypi/{name}@{version}",
        }
        for name, version in sorted(versions.items())
    ]


def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else "image"
    sbom = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "metadata": {"component": {"type": "container", "bom-ref": name, "name": name}},
        "components": _components(),
    }
    json.dump(sbom, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
