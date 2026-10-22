"""Standalone PostgreSQL claim inspection; no monitor/storage initialization."""

import argparse
import json
import os
import re
import sys
from datetime import UTC, date

import psycopg
from psycopg.rows import dict_row

QUERY = """
SELECT status, started_at, finished_at, CURRENT_TIMESTAMP AS checked_at
FROM public.source_runs
WHERE run_key = %s
"""
OPTIONS = (
    "-c default_transaction_read_only=on -c statement_timeout=10000 "
    "-c lock_timeout=3000 -c idle_in_transaction_session_timeout=15000 "
    "-c search_path=pg_catalog"
)


def validate_run_key(value: str) -> str:
    if not re.fullmatch(r"daily-\d{4}-\d{2}-\d{2}", value):
        raise ValueError("Expected daily-YYYY-MM-DD")
    date.fromisoformat(value.removeprefix("daily-"))
    return value


def inspect_claim(database_url: str, run_key: str) -> dict:
    validate_run_key(run_key)
    database_url = database_url.replace("postgresql+psycopg://", "postgresql://", 1)
    if not database_url.startswith(("postgresql://", "postgres://")):
        raise ValueError("PostgreSQL connection required")
    # Startup options protect even the first statement. Explicit read_only and rollback
    # provide additional safeguards; fail closed if the server rejects these options.
    conn = psycopg.connect(database_url, options=OPTIONS, connect_timeout=10, row_factory=dict_row)
    try:
        conn.read_only = True
        with conn.cursor() as cursor:
            cursor.execute("SHOW transaction_read_only")
            if cursor.fetchone()["transaction_read_only"] != "on":
                raise RuntimeError("Read-only transaction required")
            cursor.execute(QUERY, (run_key,))
            row = cursor.fetchone()
        return claim_report(run_key, row)
    finally:
        try:
            conn.rollback()
        finally:
            conn.close()


def claim_report(run_key: str, row: dict | None) -> dict:
    report = {"run_key": run_key, "exists": row is not None}
    if row is None:
        return {**report, "status": "absent", "started_at": None, "finished_at": None}
    status = row["status"]
    if status not in {"running", "success", "failed", "partial"}:
        raise ValueError("Unrecognized claim status")
    started = row["started_at"].astimezone(UTC)
    finished = row["finished_at"]
    checked = row["checked_at"].astimezone(UTC)
    age = (checked - started).total_seconds() / 60
    return {
        **report,
        "status": status,
        "started_at": started.isoformat(),
        "finished_at": finished.astimezone(UTC).isoformat() if finished else None,
        "checked_at": checked.isoformat(),
        "claim_age_minutes": round(age, 2),
        "success_blocks_retry": status == "success",
        "failed_or_partial_retryable": status in {"failed", "partial"},
        "manual_stale_running_claim": status == "running" and age >= 55,
        "scheduled_stale_running_claim": status == "running" and age >= 120,
        "owner_liveness": "unknown",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-key", required=True)
    args = parser.parse_args()
    try:
        report = inspect_claim(os.environ.get("DATABASE_URL", ""), args.run_key)
    except Exception:
        # Driver exceptions can contain hosts, usernames, DSNs or server data.
        print("Claim diagnostic failed; no claim state verified.", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
