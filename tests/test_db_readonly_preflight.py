import errno
import hashlib
import json
import socket
import ssl
import sys
from pathlib import Path
from unittest.mock import MagicMock

import psycopg
import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/db-readonly-preflight.yml"
PROJECT = "a" * 20
FINGERPRINT = hashlib.sha256(PROJECT.encode("ascii")).hexdigest()
PASSWORD = "synthetic-password"
DIRECT = f"db.{PROJECT}.supabase.co"
SHARED = "aws-0-us-east-1.pooler.supabase.com"
USERNAME = "synthetic_user"


@pytest.fixture
def workflow():
    return yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def test_trigger_permissions_and_secret_scope(workflow):
    assert workflow["on"] == {"workflow_dispatch": ""}
    assert workflow["permissions"] == {"contents": "read"}
    job = workflow["jobs"]["inspect"]
    assert job["if"] == "github.ref == 'refs/heads/main' && github.ref_protected"
    assert job["environment"] == "db-readonly-preflight"
    assert job["timeout-minutes"] == "5"
    *setup, diagnostic = job["steps"]
    assert "secrets." not in repr(setup)
    assert diagnostic["env"] == {
        "DATABASE_URL": "${{ secrets.DATABASE_URL }}",
        "PROJECT_FINGERPRINT": "${{ secrets.SUPABASE_PROJECT_FINGERPRINT }}",
    }
    assert "2>/dev/null" in diagnostic["run"].splitlines()[0]


@pytest.fixture
def run_preflight(workflow, monkeypatch, capsys):
    script = workflow["jobs"]["inspect"]["steps"][-1]["run"]
    code = compile("\n".join(script.splitlines()[1:-1]), str(WORKFLOW), "exec")
    monkeypatch.setattr(sys, "path", sys.path.copy())
    monkeypatch.setattr("logging.disable", MagicMock())

    def run(url, fingerprint=FINGERPRINT, *, connection_error=None, read_only="on", catalog=None):
        monkeypatch.setenv("DATABASE_URL", url)
        monkeypatch.setenv("PROJECT_FINGERPRINT", fingerprint)
        conn = MagicMock()
        conn.execute.return_value.fetchone.side_effect = [(read_only,), catalog]
        connect = MagicMock(side_effect=connection_error)
        connect.return_value.__enter__.return_value = conn
        monkeypatch.setattr(psycopg, "connect", connect)
        with pytest.raises(SystemExit) as exit_status:
            exec(code, {})
        output = capsys.readouterr()
        assert output.err == ""
        for value in (
            url,
            fingerprint,
            PROJECT,
            PASSWORD,
            DIRECT,
            SHARED,
            USERNAME,
            "synthetic error",
        ):
            if value:
                assert value not in output.out
        return exit_status.value.code, json.loads(output.out), connect, conn

    return run


def dsn(host=DIRECT, user="postgres", port="5432", suffix=""):
    return f"postgresql://{user}:{PASSWORD}@{host}:{port}/postgres{suffix}"


@pytest.mark.parametrize(
    ("url", "fingerprint", "status"),
    [
        (dsn(), "", "invalid_fingerprint"),
        (dsn(), "bad", "invalid_fingerprint"),
        ("", FINGERPRINT, "invalid_endpoint"),
        ("postgresql://", FINGERPRINT, "invalid_endpoint"),
        (dsn().replace(PASSWORD, ""), FINGERPRINT, "invalid_endpoint"),
        (dsn(host="other.example.test"), FINGERPRINT, "invalid_endpoint"),
        (dsn(host=f"{DIRECT}.example.test"), FINGERPRINT, "invalid_endpoint"),
        (dsn(host=f"{DIRECT},other.example.test"), FINGERPRINT, "invalid_endpoint"),
        (dsn(SHARED), FINGERPRINT, "invalid_endpoint"),
        (dsn(SHARED, "postgres.short"), FINGERPRINT, "invalid_endpoint"),
        (dsn(port="1234"), FINGERPRINT, "invalid_endpoint"),
        (dsn(suffix="?hostaddr=127.0.0.1"), FINGERPRINT, "invalid_endpoint"),
        (dsn(suffix="?options=-c%20statement_timeout%3D0"), FINGERPRINT, "invalid_endpoint"),
        (dsn(), "f" * 64, "project_mismatch"),
        (dsn(SHARED, "postgres." + "b" * 20), FINGERPRINT, "project_mismatch"),
    ],
)
def test_rejects_before_connect(run_preflight, url, fingerprint, status):
    exit_code, result, connect, conn = run_preflight(url, fingerprint)
    assert exit_code == 1
    assert result["policy_review_status"] == status
    assert result["connection_ok"] is False
    assert result["connection_error"] is None
    assert result["endpoint_type"] == "unknown"
    assert result["libpq_version"] is None
    connect.assert_not_called()
    conn.execute.assert_not_called()


@pytest.mark.parametrize(
    ("host", "user", "port", "endpoint_type"),
    [
        (DIRECT, "postgres", "5432", "direct"),
        (DIRECT, "postgres", "6543", "dedicated_transaction_pooler"),
        (SHARED, f"postgres.{PROJECT}", "5432", "shared_session_pooler"),
        (SHARED, f"custom_role.{PROJECT}", "6543", "shared_transaction_pooler"),
    ],
)
def test_verified_endpoint_connects_read_only(run_preflight, host, user, port, endpoint_type):
    status, result, connect, conn = run_preflight(dsn(host, user, port, "?sslmode=disable"))
    assert status == 0
    assert result["project_match"] is True
    assert result["connection_error"] is None
    assert result["endpoint_type"] == endpoint_type
    assert result["libpq_version"] == psycopg.pq.version()
    assert result["transaction_read_only"] is True
    assert result["table_exists"] is False
    connect.assert_called_once_with(
        host=host,
        user=user,
        port=port,
        password=PASSWORD,
        dbname="postgres",
        sslmode="verify-full",
        sslrootcert="system",
        gssencmode="disable",
        connect_timeout=5,
        autocommit=True,
        prepare_threshold=None,
    )
    statements = [call.args[0].strip() for call in conn.execute.call_args_list]
    assert statements[:4] == [
        "BEGIN READ ONLY",
        "SET LOCAL statement_timeout = '3s'",
        "SET LOCAL lock_timeout = '1s'",
        "SHOW transaction_read_only",
    ]
    assert len(statements) == 6
    assert statements[4].startswith("SELECT") and "FROM pg_class" in statements[4]
    assert statements[5] == "ROLLBACK"


def test_connection_error_masks_sensitive_details(run_preflight):
    url = dsn()
    error = RuntimeError(f"synthetic error: {url} {FINGERPRINT}")
    status, result, connect, conn = run_preflight(url, connection_error=error)
    assert status == 1
    assert result["policy_review_status"] == "connection_failed"
    assert result["connection_error"] == "unknown_connection_error"
    connect.assert_called_once()
    conn.execute.assert_not_called()


def test_non_readonly_session_rolls_back_without_catalog_query(run_preflight):
    status, result, _, conn = run_preflight(dsn(), read_only="off")
    assert status == 1
    assert result["policy_review_status"] == "readonly_check_failed"
    statements = [call.args[0].strip() for call in conn.execute.call_args_list]
    assert not any(sql.startswith("SELECT") for sql in statements)
    assert statements[-1] == "ROLLBACK"


def test_catalog_error_is_masked_and_rolls_back(run_preflight):
    url = dsn()
    status, result, _, conn = run_preflight(
        url, catalog=RuntimeError(f"synthetic error: {url} {FINGERPRINT}")
    )
    assert status == 1
    assert result["policy_review_status"] == "catalog_check_failed"
    conn.execute.assert_called_with("ROLLBACK")


@pytest.mark.parametrize(
    ("make_error", "category"),
    [
        (ssl.SSLCertVerificationError, "tls_verification_failed"),
        (lambda detail: socket.gaierror(socket.EAI_NONAME, detail), "network_unreachable"),
        (lambda detail: OSError(errno.ENETUNREACH, detail), "network_unreachable"),
        (psycopg.errors.InvalidPassword, "authentication_failed"),
        (psycopg.errors.InvalidAuthorizationSpecification, "authentication_failed"),
        (psycopg.errors.ConnectionTimeout, "connect_timeout"),
        (TimeoutError, "connect_timeout"),
        (psycopg.OperationalError, "unknown_connection_error"),
    ],
)
def test_structured_connection_errors_are_classified_without_disclosing_details(
    run_preflight, make_error, category
):
    url = dsn(user=USERNAME)
    error = make_error(f"synthetic error: {url} {FINGERPRINT}")
    status, result, connect, conn = run_preflight(url, connection_error=error)
    assert status == 1
    assert result["connection_error"] == category
    assert result["policy_review_status"] == "connection_failed"
    connect.assert_called_once()
    conn.execute.assert_not_called()


@pytest.mark.parametrize(
    ("reason", "category"),
    [
        ("SSL error: certificate verify failed", "tls_verification_failed"),
        ("Network is unreachable", "network_unreachable"),
        ("timeout expired", "connect_timeout"),
        ("unexpected certificate verify failed text", "unknown_connection_error"),
        ("Connection refused", "unknown_connection_error"),
    ],
)
def test_libpq_requires_an_exact_connection_reason(run_preflight, reason, category):
    error = psycopg.OperationalError(f'connection to server at "{DIRECT}" failed: {reason}')
    status, result, _, _ = run_preflight(dsn(), connection_error=error)
    assert status == 1
    assert result["connection_error"] == category


def test_conflicting_libpq_attempts_are_unknown(run_preflight):
    error = psycopg.OperationalError(
        f'connection to server at "{DIRECT}" failed: Network is unreachable\n'
        f'connection to server at "{DIRECT}" failed: timeout expired'
    )
    status, result, _, _ = run_preflight(dsn(), connection_error=error)
    assert status == 1
    assert result["connection_error"] == "unknown_connection_error"
