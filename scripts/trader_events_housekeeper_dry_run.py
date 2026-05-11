"""Plan 0049 operator helper — preview the next housekeeper sweep.

Prints ``rows_to_delete``, ``oldest_row``, and ``bytes_estimate`` for
each retention tier (firehose / other) WITHOUT issuing any DELETE.
Useful before tuning ``trader_events_firehose_retention_days`` or
``trader_events_other_retention_days`` further down — call this
first to check how much volume the new horizon would prune in the
next 6 h cycle.

Usage (from the live backend container on ``polyhome-1``):

    docker compose exec -T backend python -m \
        scripts.trader_events_housekeeper_dry_run

Add ``--json`` for machine-readable output suitable for piping into
``jq``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path


def _ensure_backend_on_path() -> None:
    """Make ``backend/`` importable regardless of the cwd we're invoked from."""
    repo_root = Path(__file__).resolve().parents[1]
    backend_root = repo_root / "backend"
    for entry in (backend_root, repo_root):
        if str(entry) not in sys.path:
            sys.path.insert(0, str(entry))


_ensure_backend_on_path()


from services.trader_events_retention_service import dry_run_summary  # noqa: E402


def _format_bytes(n: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(n)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:,.1f} {unit}"
        value /= 1024
    return f"{n} B"


def _render_human(summary: dict) -> str:
    lines = [
        "trader_events housekeeper — dry-run preview",
        "------------------------------------------",
        f"firehose retention: {summary['firehose_retention_days']} day(s)",
        f"other    retention: {summary['other_retention_days']} day(s)",
        "",
        "tier=firehose_evaluation",
        f"  rows_to_delete : {summary['firehose']['rows_to_delete']:,}",
        f"  oldest_row     : {summary['firehose']['oldest_row_iso'] or '<none>'}",
        f"  bytes_estimate : {_format_bytes(int(summary['firehose']['bytes_estimate']))}",
        "",
        "tier=other",
        f"  rows_to_delete : {summary['other']['rows_to_delete']:,}",
        f"  oldest_row     : {summary['other']['oldest_row_iso'] or '<none>'}",
        f"  bytes_estimate : {_format_bytes(int(summary['other']['bytes_estimate']))}",
        "",
        "(no DELETE issued)",
    ]
    return "\n".join(lines)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON instead of the human-readable table",
    )
    return parser.parse_args()


async def _amain() -> int:
    args = _parse_args()
    summary = await dry_run_summary()
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(_render_human(summary))
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
