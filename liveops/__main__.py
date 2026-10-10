from __future__ import annotations

import os

import uvicorn


def main() -> None:
    uvicorn.run(
        "liveops.app:app",
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "8765")),
        reload=os.getenv("RELOAD", "0") == "1",
    )


if __name__ == "__main__":
    main()
