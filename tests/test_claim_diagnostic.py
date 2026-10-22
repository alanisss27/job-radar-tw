import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from job_monitor import claim_diagnostic as diagnostic

NOW = datetime(2026, 10, 22, 16, tzinfo=UTC)
KEY = "daily-2026-10-22"


def row(status, minutes=30):
    return {
        "status": status,
        "started_at": NOW - timedelta(minutes=minutes),
        "finished_at": None if status == "running" else NOW,
        "checked_at": NOW,
        "errors": "sensitive server data",
        "stats": {"private": "do not disclose"},
    }


class ReadOnlyConnection:
    def __init__(self, result=None, mode="on", fail=False):
        self.result = result
        self.mode = mode
        self.fail = fail
        self.read_only = False
        self.statements = []
        self.rolled_back = False
        self.closed = False

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, query, params=None):
        assert self.read_only
        self.statements.append((query, params))
        if self.fail:
            raise RuntimeError("secret DSN")
        assert query == "SHOW transaction_read_only" or query == diagnostic.QUERY

    def fetchone(self):
        if self.statements[-1][0].startswith("SHOW"):
            return {"transaction_read_only": self.mode}
        return self.result

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


@pytest.mark.parametrize("status", [None, "success", "running", "failed", "partial"])
def test_read_only_query_and_allowlisted_report(monkeypatch, status):
    conn = ReadOnlyConnection(row(status) if status else None)

    def connect(url, **kwargs):
        assert url == "postgresql://example.invalid/db"
        assert "default_transaction_read_only=on" in kwargs["options"]
        assert "statement_timeout=10000" in kwargs["options"]
        assert kwargs["connect_timeout"] == 10
        return conn

    monkeypatch.setattr(diagnostic.psycopg, "connect", connect)
    result = diagnostic.inspect_claim("postgresql+psycopg://example.invalid/db", KEY)
    assert result["status"] == (status or "absent")
    assert result["exists"] == (status is not None)
    assert result["started_at"] == (row(status)["started_at"].isoformat() if status else None)
    assert result["finished_at"] == (
        NOW.isoformat() if status in {"success", "failed", "partial"} else None
    )
    if status:
        assert result["success_blocks_retry"] == (status == "success")
        assert result["failed_or_partial_retryable"] == (status in {"failed", "partial"})
        assert result["owner_liveness"] == "unknown"
    assert "sensitive" not in json.dumps(result)
    assert "private" not in json.dumps(result)
    assert conn.statements[-1] == (diagnostic.QUERY, (KEY,))
    assert "FOR UPDATE" not in diagnostic.QUERY
    assert "stats" not in diagnostic.QUERY and "errors" not in diagnostic.QUERY
    assert conn.rolled_back and conn.closed


@pytest.mark.parametrize(
    "minutes,manual,scheduled",
    [(54.99, False, False), (55, True, False), (119.99, True, False), (120, True, True)],
)
def test_retry_thresholds_and_no_liveness_inference(minutes, manual, scheduled):
    result = diagnostic.claim_report(KEY, row("running", minutes))
    assert result["manual_stale_running_claim"] is manual
    assert result["scheduled_stale_running_claim"] is scheduled
    assert result["owner_liveness"] == "unknown"


@pytest.mark.parametrize("mode,fail", [("off", False), ("on", True)])
def test_fail_closed_and_rollback(monkeypatch, mode, fail):
    conn = ReadOnlyConnection(row("running"), mode=mode, fail=fail)
    monkeypatch.setattr(diagnostic.psycopg, "connect", lambda *a, **kw: conn)
    with pytest.raises(RuntimeError):
        diagnostic.inspect_claim("postgresql://example.invalid/db", KEY)
    assert len(conn.statements) == 1
    assert conn.rolled_back and conn.closed


@pytest.mark.parametrize(
    "key", ["", "manual-123", "daily-2026-02-30", "daily-2026-10-22'; DELETE FROM source_runs;--"]
)
def test_invalid_input_never_connects(monkeypatch, key):
    def forbidden(*args, **kwargs):
        pytest.fail("Invalid input must not open a connection")

    monkeypatch.setattr(diagnostic.psycopg, "connect", forbidden)
    with pytest.raises(ValueError):
        diagnostic.inspect_claim("postgresql://example.invalid/db", key)


def test_cli_redacts_driver_errors(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["diagnostic", "--run-key", KEY])
    monkeypatch.setenv("DATABASE_URL", "postgresql://private:password@private-host/db")

    def fail(*args, **kwargs):
        raise RuntimeError("postgresql://private:password@private-host/db")

    monkeypatch.setattr(diagnostic.psycopg, "connect", fail)
    assert diagnostic.main() == 1
    captured = capsys.readouterr()
    assert not captured.out
    assert captured.err == "Claim diagnostic failed; no claim state verified.\n"


def test_workflow_is_isolated_and_uses_env_input():
    workflow = yaml.safe_load(Path(".github/workflows/claim-diagnostic.yml").read_text())
    assert set(workflow[True]) == {"workflow_dispatch"}
    assert workflow[True]["workflow_dispatch"]["inputs"]["run_key"]["required"] is True
    assert workflow["permissions"] == {"contents": "read"}
    job = workflow["jobs"]["inspect"]
    assert job["if"] == "github.ref == 'refs/heads/main'"
    secret_steps = [s for s in job["steps"] if "env" in s]
    assert len(secret_steps) == 1
    step = secret_steps[0]
    assert step["env"] == {
        "DATABASE_URL": "${{ secrets.DATABASE_URL }}",
        "INPUT_RUN_KEY": "${{ inputs.run_key }}",
    }
    assert step["run"] == (
        'uv run --no-sync python -m job_monitor.claim_diagnostic --run-key "$INPUT_RUN_KEY"'
    )


def test_unknown_database_status_is_not_disclosed():
    with pytest.raises(ValueError, match="Unrecognized claim status"):
        diagnostic.claim_report(KEY, row("sensitive unexpected value"))
