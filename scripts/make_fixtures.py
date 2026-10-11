"""Materialise the controlled fixture repositories as real git repositories.

Usage::

    python scripts/make_fixtures.py --destination .fixtures-repos

The printed paths can be used directly as ``repo_url`` values in
``POST /api/tasks`` when ``REPOPILOT_TRUSTED_SOURCE_ROOTS`` includes the
destination directory.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from repopilot.fixtures import available_fixtures, materialise_fixture  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--destination",
        default=str(REPO_ROOT / ".fixtures-repos"),
        help="Directory that receives the materialised repositories.",
    )
    parser.add_argument("--no-followup-commit", action="store_true")
    args = parser.parse_args()

    repos = [
        materialise_fixture(
            name,
            args.destination,
            include_followup_commit=not args.no_followup_commit,
        )
        for name in available_fixtures()
    ]
    print(
        json.dumps(
            [
                {
                    "name": repo.name,
                    "path": str(repo.path),
                    "baseline_commit": repo.baseline_commit,
                    "head_commit": repo.head_commit,
                }
                for repo in repos
            ],
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
