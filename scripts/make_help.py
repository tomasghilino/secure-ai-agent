"""Print Makefile targets annotated with `## description` (cross-platform `make help`)."""

from __future__ import annotations

import re
import sys
from pathlib import Path

TARGET = re.compile(r"^([a-zA-Z0-9_-]+):.*?## (.+)$")


def main(makefile: str = "Makefile") -> None:
    for line in Path(makefile).read_text(encoding="utf-8").splitlines():
        if match := TARGET.match(line):
            print(f"  make {match.group(1):<20} {match.group(2)}")


if __name__ == "__main__":
    main(*sys.argv[1:])
