"""Write the third-party license notices an image ships in /app/licenses.

Run it inside the image, with the interpreter of the venv it describes, after
everything is installed::

    /app/.venv/bin/python third_party_licenses.py --out /app/licenses

It writes:

- ``python/<name>-<version>/``: the license files each installed distribution
  ships (PEP 639 ``License-File`` entries, or LICENSE/COPYING/NOTICE files
  in older wheels);
- ``os-packages.tsv``: every Debian package in the image with its source
  package and version, so the source of the GPL utilities in the base image
  can be found (the copyright files stay in /usr/share/doc/<package>/);
- ``README.md``: the index, which also lists ``frontend/`` when the frontend
  script (frontend/scripts/third-party-licenses.mjs) has written it.

Standard library only: it runs with whatever the image's venv has, and the
image has no dev dependencies.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path, PurePosixPath

# Our own distributions: their license is the image's own, not a third party's.
_OWN = frozenset({"tripl-server", "tripl-enterprise", "tripl", "tripl-mcp"})
_LICENSE_NAME = re.compile(r"^(licen[cs]e|copying|notice|authors)([.-].*)?$", re.IGNORECASE)
# The LGPL (psycopg, in the server image) lets a proprietary work use the
# library as long as the user can swap it; this says they can.
_LGPL_NOTE = (
    "LGPL-licensed libraries ({names}) are installed unmodified, as separate "
    "packages in the venv. You may replace them with a compatible build of "
    "your own; their license texts are in the directories listed below."
)


@dataclass(frozen=True)
class Entry:
    name: str
    version: str
    license: str
    files: tuple[str, ...]


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _license_of(dist: metadata.Distribution) -> str:
    md = dist.metadata
    expression = md.get("License-Expression")
    if expression:
        return str(expression)
    classifiers = [
        c.split("::")[-1].strip()
        for c in md.get_all("Classifier") or []
        if c.startswith("License ::") and c.split("::")[-1].strip() != "OSI Approved"
    ]
    if classifiers:
        return "; ".join(classifiers)
    text = (md.get("License") or "").strip()
    # Some projects paste the whole license text into this field.
    return text.splitlines()[0][:120] if text else "UNKNOWN"


def _license_files(dist: metadata.Distribution) -> list[tuple[str, str]]:
    """(file name, text) for every license file the distribution ships."""
    found: list[tuple[str, str]] = []
    for rel in dist.metadata.get_all("License-File") or []:
        # PEP 639 puts them under .dist-info/licenses/; older tools at the root.
        for candidate in (f"licenses/{rel}", rel):
            text = dist.read_text(candidate)
            if text is not None:
                found.append((PurePosixPath(rel).name, text))
                break
    if found:
        return found
    for path in dist.files or []:
        parts = PurePosixPath(str(path)).parts
        if len(parts) >= 2 and parts[0].endswith(".dist-info") and _LICENSE_NAME.match(parts[-1]):
            text = dist.read_text("/".join(parts[1:]))
            if text is not None:
                found.append((parts[-1], text))
    return found


def collect_python(out: Path) -> list[Entry]:
    entries: dict[str, Entry] = {}
    for dist in metadata.distributions():
        name = dist.metadata["Name"]
        if not name or _normalize(name) in _OWN or _normalize(name) in entries:
            continue
        target = out / "python" / f"{_normalize(name)}-{dist.version}"
        written: list[str] = []
        for file_name, text in _license_files(dist):
            target.mkdir(parents=True, exist_ok=True)
            dest = target / file_name
            # Two declared files can share a base name (LICENSE in two subdirs).
            index = 1
            while dest.exists():
                index += 1
                dest = target / f"{file_name}.{index}"
            dest.write_text(text, encoding="utf-8")
            written.append(dest.relative_to(out).as_posix())
        entries[_normalize(name)] = Entry(name, dist.version, _license_of(dist), tuple(written))
    return sorted(entries.values(), key=lambda e: e.name.lower())


def collect_os(out: Path) -> int:
    """Write os-packages.tsv and return the package count; 0 without dpkg."""
    dpkg = shutil.which("dpkg-query")
    if dpkg is None:
        return 0
    listing = subprocess.run(
        [dpkg, "-W", "-f=${Package}\t${Version}\t${source:Package}\t${source:Version}\n"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    rows = sorted(line for line in listing.splitlines() if line.strip())
    header = "package\tversion\tsource_package\tsource_version\n"
    (out / "os-packages.tsv").write_text(header + "\n".join(rows) + "\n", encoding="utf-8")
    return len(rows)


def _frontend_entries(out: Path) -> list[Entry]:
    index = out / "frontend" / "index.json"
    if not index.is_file():
        return []
    raw = json.loads(index.read_text(encoding="utf-8"))
    return [Entry(e["name"], e["version"], e["license"], tuple(e["files"])) for e in raw]


def _table(entries: list[Entry]) -> str:
    lines = ["| Package | Version | License | Files |", "|---|---|---|---|"]
    for e in entries:
        files = ", ".join(f"`{f}`" for f in e.files) or "none shipped"
        lines.append(f"| {e.name} | {e.version} | {e.license.replace('|', '/')} | {files} |")
    return "\n".join(lines)


def write_readme(out: Path, python: list[Entry], frontend: list[Entry], os_count: int) -> None:
    sections = [
        "# Third-party software in this image",
        "",
        "This image contains software written by others, under their own "
        "licenses. Each license file is copied below, next to the package it "
        "belongs to. Generated when the image was built.",
    ]
    if python:
        sections += ["", "## Python packages (`/app/.venv`)", ""]
        lgpl = sorted({e.name for e in python if "LGPL" in e.license.upper()})
        if lgpl:
            sections += [_LGPL_NOTE.format(names=", ".join(lgpl)), ""]
        sections.append(_table(python))
    if frontend:
        sections += [
            "",
            "## Web application",
            "",
            "Packages the web application depends on at run time; the parts it "
            "uses are compiled into its JavaScript bundle.",
            "",
            _table(frontend),
        ]
    if os_count:
        sections += [
            "",
            "## Operating system",
            "",
            f"{os_count} Debian packages, listed with their source packages in "
            "`os-packages.tsv`. Each package's copyright and license is in "
            "`/usr/share/doc/<package>/copyright`. Their source code is "
            "available from https://sources.debian.org/ and "
            "https://snapshot.debian.org/ (by source package and version), and, "
            "for packages from the PostgreSQL repository (versions containing "
            "`pgdg`), from https://apt.postgresql.org/pub/repos/apt/pool/main/.",
            "",
            "The Python interpreter in `/usr/local` is CPython, under the Python "
            "Software Foundation License: https://docs.python.org/3/license.html.",
        ]
    (out / "README.md").write_text("\n".join(sections) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Write the image's third-party license notices.")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--no-os", action="store_true", help="skip the Debian package list")
    args = parser.parse_args()
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    # A rerun (the Enterprise image adds packages on top) replaces the list.
    shutil.rmtree(out / "python", ignore_errors=True)
    python = collect_python(out)
    os_count = 0 if args.no_os else collect_os(out)
    write_readme(out, python, _frontend_entries(out), os_count)
    missing = [e.name for e in python if not e.files]
    print(
        f"{len(python)} Python packages ({len(missing)} without a license file), "
        f"{os_count} OS packages"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
