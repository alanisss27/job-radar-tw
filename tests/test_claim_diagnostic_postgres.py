"""Opt-in live tests against a disposable loopback-only PostgreSQL database.

Never use DATABASE_URL: the database, host and role below are fixed test values.
See docs/daily-claim-diagnostic.md for setup and invocation.
"""

import os
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg.pq import TransactionStatus

from job_monitor import claim_diagnostic as diagnostic

pytestmark = pytest.mark.skipif(
    os.environ.get("CLAIM_DIAGNOSTIC_POSTGRES_TEST") != "1",
    reason="Requires explicitly provisioned isolated local PostgreSQL",
)
KEY = "daily-2026-10-22"


@pytest.fixture
def database():
    port = int(os.environ.get("CLAIM_DIAGNOSTIC_TEST_PORT", "55435"))
    assert 1024 <= port <= 65535 and port != 5432
    url = f"postgresql://claim_test_owner@127.0.0.1:{port}/claim_diagnostic_test"
    # This fixture intentionally mutates only the explicitly opted-in local test DB.
    with psycopg.Connection.connect(url, autocommit=True) as setup:
        assert setup.execute("SELECT current_database()").fetchone()[0] == "claim_diagnostic_test"
        assert not setup.execute(
            "SELECT rolsuper FROM pg_roles WHERE rolname = current_user"
        ).fetchone()[0]
        setup.execute("DROP TABLE IF EXISTS public.source_runs")
        setup.execute(
            "CREATE TABLE public.source_runs ("
            "run_key text PRIMARY KEY, status text NOT NULL, "
            "started_at timestamptz NOT NULL, finished_at timestamptz, "
            "stats jsonb, errors jsonb)"
        )
        yield url, setup
        setup.execute("DROP TABLE public.source_runs")


@pytest.fixture
def connections(monkeypatch):
    opened = []

    class TrackingCursor(psycopg.Cursor):
        def execute(self, query, params=None, **kwargs):
            self.connection.statements.append((query, params))
            return super().execute(query, params, **kwargs)

    class TrackingConnection(psycopg.Connection):
        def rollback(self):
            self.before_rollback = self.info.transaction_status
            self.rollback_count += 1
            return super().rollback()

        def commit(self):
            pytest.fail("Diagnostic must never commit")

    def connect(*args, **kwargs):
        conn = TrackingConnection.connect(*args, cursor_factory=TrackingCursor, **kwargs)
        conn.statements = []
        conn.rollback_count = 0
        conn.before_rollback = None
        opened.append(conn)
        return conn

    monkeypatch.setattr(diagnostic.psycopg, "connect", connect)
    return opened


def snapshot(setup):
    return setup.execute("SELECT * FROM public.source_runs ORDER BY run_key").fetchall()


def assert_closed_with_rollback(connections, transaction_status=TransactionStatus.INTRANS):
    assert len(connections) == 1
    conn = connections[0]
    assert conn.rollback_count == 1
    assert conn.before_rollback == transaction_status
    assert conn.closed
    assert conn.read_only is True


@pytest.mark.parametrize("status", [None, "success", "running", "failed", "partial"])
def test_live_claim_states(database, connections, status):
    url, setup = database
    now = datetime.now(UTC)
    started = now - timedelta(minutes=75)
    finished = None if status == "running" else now
    if status:
        setup.execute(
            "INSERT INTO public.source_runs VALUES (%s, %s, %s, %s, %s, %s)",
            (KEY, status, started, finished, '{"private":true}', '["private error"]'),
        )
    before = snapshot(setup)
    result = diagnostic.inspect_claim(url, KEY)
    assert result["status"] == (status or "absent")
    assert result["exists"] is (status is not None)
    assert result["started_at"] == (started.isoformat() if status else None)
    assert result["finished_at"] == (finished.isoformat() if status and finished else None)
    if status:
        assert result["failed_or_partial_retryable"] is (status in {"failed", "partial"})
        assert result["success_blocks_retry"] is (status == "success")
        assert result["manual_stale_running_claim"] is (status == "running")
        assert result["scheduled_stale_running_claim"] is False
    assert snapshot(setup) == before
    assert_closed_with_rollback(connections)
    assert connections[0].statements == [
        ("SHOW transaction_read_only", None),
        (diagnostic.QUERY, (KEY,)),
    ]


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO public.source_runs (run_key,status,started_at) "
        "VALUES (%s,'running',CURRENT_TIMESTAMP)",
        "UPDATE public.source_runs SET status='failed' WHERE run_key=%s",
        "DELETE FROM public.source_runs WHERE run_key=%s",
        "CREATE TABLE public.should_not_exist AS SELECT %s::text AS value",
    ],
)
def test_server_rejects_accidental_writes(database, connections, monkeypatch, statement):
    url, setup = database
    setup.execute(
        "INSERT INTO public.source_runs (run_key,status,started_at) "
        "VALUES (%s,'running',CURRENT_TIMESTAMP)",
        (KEY,),
    )
    before = snapshot(setup)
    # Deliberately substitute a future accidental write for the diagnostic's SELECT.
    # The connection uses a real owner role with write privileges.
    monkeypatch.setattr(diagnostic, "QUERY", statement)
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        diagnostic.inspect_claim(url, KEY)
    assert_closed_with_rollback(connections, TransactionStatus.INERROR)
    assert snapshot(setup) == before
    assert setup.execute("SELECT to_regclass('public.should_not_exist')").fetchone()[0] is None


def test_live_query_error_redacted_and_rolled_back(database, connections, monkeypatch, capsys):
    url, setup = database
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setattr(sys, "argv", ["diagnostic", "--run-key", KEY])
    monkeypatch.setattr(
        diagnostic,
        "QUERY",
        "SELECT private_missing_column FROM public.source_runs WHERE run_key=%s",
    )
    before = snapshot(setup)
    assert diagnostic.main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "Claim diagnostic failed; no claim state verified.\n"
    assert_closed_with_rollback(connections, TransactionStatus.INERROR)
    assert snapshot(setup) == before


def test_live_result_error_redacted_and_rolled_back(database, connections, monkeypatch, capsys):
    url, setup = database
    setup.execute(
        "INSERT INTO public.source_runs (run_key,status,started_at) "
        "VALUES (%s,'private-unexpected-status',CURRENT_TIMESTAMP)",
        (KEY,),
    )
    before = snapshot(setup)
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setattr(sys, "argv", ["diagnostic", "--run-key", KEY])
    assert diagnostic.main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "Claim diagnostic failed; no claim state verified.\n"
    assert_closed_with_rollback(connections)
    assert snapshot(setup) == before


def test_live_sql_parameter_binding(database, connections):
    url, setup = database
    setup.execute(
        "INSERT INTO public.source_runs (run_key,status,started_at) "
        "VALUES (%s,'running',CURRENT_TIMESTAMP)",
        (KEY,),
    )
    before = snapshot(setup)
    # Exercise the same SELECT with hostile text directly; CLI validation rejects it
    # before connecting, while parameter binding also treats it solely as data.
    hostile_key = "daily-2026-10-22'; DELETE FROM public.source_runs; --"
    with psycopg.Connection.connect(url, options=diagnostic.OPTIONS) as conn:
        with conn.cursor() as cursor:
            cursor.execute(diagnostic.QUERY, (hostile_key,))
            assert cursor.fetchone() is None
        conn.rollback()
    assert snapshot(setup) == before
    with pytest.raises(ValueError):
        diagnostic.inspect_claim(url, hostile_key)
    assert connections == []


def test_startup_defaults_are_read_only(database):
    url, _ = database
    with psycopg.Connection.connect(url, options=diagnostic.OPTIONS) as conn:
        # No explicit conn.read_only assignment: startup protection alone applies.
        assert conn.execute("SHOW default_transaction_read_only").fetchone()[0] == "on"
        assert conn.execute("SHOW transaction_read_only").fetchone()[0] == "on"
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute("DELETE FROM public.source_runs")
        conn.rollback()


def test_standalone_module_has_no_monitor_imports(database):
    url, setup = database
    before = snapshot(setup)
    script = """
import builtins, runpy, sys
original_import = builtins.__import__
forbidden = {'job_monitor.cli', 'job_monitor.storage', 'job_monitor.pipeline',
             'job_monitor.sources', 'job_monitor.notifier'}
def guarded_import(name, *args, **kwargs):
    if any(name == item or name.startswith(item + '.') for item in forbidden):
        raise AssertionError('Monitor import forbidden')
    return original_import(name, *args, **kwargs)
builtins.__import__ = guarded_import
sys.argv = ['claim_diagnostic', '--run-key', 'daily-2026-10-22']
runpy.run_module('job_monitor.claim_diagnostic', run_name='__main__')
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        env={**os.environ, "DATABASE_URL": url, "PYTHONPATH": "src"},
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert json.loads(result.stdout)["status"] == "absent"
    assert snapshot(setup) == before
