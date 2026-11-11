"""python -m hoarder"""

from __future__ import annotations

import uvicorn


def main() -> None:
    uvicorn.run("hoarder.app:app", host="127.0.0.1", port=8770, reload=False)


if __name__ == "__main__":
    main()
