"""Connectivity probe for the configured model endpoint.

Usage::

    python -m repopilot.llm.health

Exit codes: ``0`` reachable, ``2`` unreachable, ``3`` misconfigured provider.
The probe prints only the endpoint metadata, never the API key.
"""

from __future__ import annotations

import json
import sys

from repopilot.config import load_settings
from repopilot.errors import RepoPilotError
from repopilot.llm.registry import build_model_client


def main(argv: list[str] | None = None) -> int:
    """Probe ``GET {base_url}/models`` and report reachability."""

    del argv  # no arguments are needed; configuration comes from the environment
    settings = load_settings()
    try:
        client = build_model_client(settings)
    except RepoPilotError as exc:
        print(json.dumps({"ok": False, "error": exc.failure_reason()}, ensure_ascii=False))
        return 3
    health = client.health_check()
    print(
        json.dumps(
            {
                "ok": health.reachable,
                "provider": health.provider,
                "model": health.model,
                "base_url": settings.model_base_url,
                "detail": health.detail,
                "latency_ms": health.latency_ms,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    close = getattr(client, "close", None)
    if callable(close):
        close()
    return 0 if health.reachable else 2


if __name__ == "__main__":
    sys.exit(main())
