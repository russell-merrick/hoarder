"""python -m hoarder"""

from __future__ import annotations

import sys

import uvicorn

from hoarder.roots import ROOTS_FILE


def main() -> None:
    if not ROOTS_FILE.is_file():
        sys.exit(f"No {ROOTS_FILE.name}. Copy roots.example.json to {ROOTS_FILE} and list your sample folders.")
    uvicorn.run("hoarder.app:app", host="127.0.0.1", port=8770, reload=False)


if __name__ == "__main__":
    main()
