"""``python -m repopilot`` entrypoint running the API with uvicorn."""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    host = os.environ.get("REPOPILOT_HOST", "127.0.0.1")
    port = int(os.environ.get("REPOPILOT_PORT", "8000"))
    uvicorn.run("repopilot.app:create_app", factory=True, host=host, port=port)


if __name__ == "__main__":
    main()
