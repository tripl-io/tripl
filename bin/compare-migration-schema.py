"""Compare two pg_dump schemas, tolerating one PostgreSQL cast rewrite.

After a dump/restore, PostgreSQL can print a CHECK (... = ANY (...)) array
with a text[] cast on the whole varchar array or with text casts on each
element. The literals and their order are preserved by this normalization.
"""

import difflib
import re
import sys
from pathlib import Path

_ITEM = r"'(?:''|[^'])*'::character varying"
_ITEM_RE = re.compile(_ITEM)
_ARRAY_RE = re.compile(r"\(\(ARRAY\[(?P<items>" + _ITEM + r"(?:, " + _ITEM + r")*)\]\)::text\[\]\)")
_CHECK_RE = re.compile(r"\s+CONSTRAINT ck_[A-Za-z0-9_]+ CHECK ")


def normalize(line: str) -> str:
    if not _CHECK_RE.match(line) or " ANY " not in line:
        return line

    def cast_elements(match: re.Match[str]) -> str:
        items = match.group("items")
        literals = [part.group(0) for part in _ITEM_RE.finditer(items)]
        if ", ".join(literals) != items:
            return match.group(0)
        return "(ARRAY[" + ", ".join(f"({literal})::text" for literal in literals) + "])"

    return _ARRAY_RE.sub(cast_elements, line)


def main() -> int:
    if len(sys.argv) != 3:
        sys.exit("usage: compare-migration-schema.py OLD_DUMP NEW_DUMP")

    old_path, new_path = (Path(arg) for arg in sys.argv[1:])
    old_lines = [normalize(line) for line in old_path.read_text().splitlines(keepends=True)]
    new_lines = [normalize(line) for line in new_path.read_text().splitlines(keepends=True)]
    differences = list(
        difflib.unified_diff(old_lines, new_lines, fromfile=str(old_path), tofile=str(new_path))
    )
    if differences:
        sys.stdout.writelines(differences)
        return 1
    print("pre-squash and baseline PostgreSQL schemas match")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
