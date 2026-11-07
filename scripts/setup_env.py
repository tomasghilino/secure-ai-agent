"""Create a local `.env` from `.env.example` if it does not exist. Never overwrites."""

from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    env_file, example = ROOT / ".env", ROOT / ".env.example"
    if env_file.exists():
        print(".env already exists - left untouched")
        return
    shutil.copyfile(example, env_file)
    print("Created .env from .env.example (fake providers: no AWS calls, no cost)")


if __name__ == "__main__":
    main()
