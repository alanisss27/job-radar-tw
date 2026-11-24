import base64
import errno
import hashlib
import json
import os
import stat
import subprocess
import socket
import ssl
import sys
import time
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
    choice = workflow["on"]["workflow_dispatch"]["inputs"]["diagnostic_mode"]
    assert choice["type"] == "choice"
    assert choice["default"] == "catalog"
    assert choice["options"] == ["catalog", "ca_compare"]
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
        "DIAGNOSTIC_MODE": "${{ inputs.diagnostic_mode }}",
        "PROJECT_CA_PEM_B64": (
            "${{ inputs.diagnostic_mode == 'ca_compare' && secrets.SUPABASE_PROJECT_CA_PEM_B64 || '' }}"
        ),
    }
    assert "2>/dev/null" in diagnostic["run"].splitlines()[0]


@pytest.fixture
def run_preflight(workflow, monkeypatch, capsys, tmp_path):
    script = workflow["jobs"]["inspect"]["steps"][-1]["run"]
    code = compile("\n".join(script.splitlines()[1:-1]), str(WORKFLOW), "exec")
    monkeypatch.setattr(sys, "path", sys.path.copy())
    monkeypatch.setattr("logging.disable", MagicMock())

    def run(
        url,
        fingerprint=FINGERPRINT,
        *,
        connection_error=None,
        read_only="on",
        catalog=None,
        mode="catalog",
        ca="",
        resolver=None,
        interrupted=False,
    ):
        monkeypatch.setenv("DATABASE_URL", url)
        monkeypatch.setenv("PROJECT_FINGERPRINT", fingerprint)
        monkeypatch.setenv("DIAGNOSTIC_MODE", mode)
        monkeypatch.setenv("PROJECT_CA_PEM_B64", ca)
        monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
        monkeypatch.setattr("signal.signal", MagicMock())
        if resolver is None:
            resolver = MagicMock(side_effect=AssertionError("unexpected DNS lookup"))
        monkeypatch.setattr(socket, "getaddrinfo", resolver)
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
            "-----BEGIN CERTIFICATE-----",
            ca,
        ):
            if value:
                assert value not in output.out
        if interrupted:
            assert output.out == ""
            return exit_status.value.code, None, connect, conn
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


@pytest.mark.parametrize(
    ("reason", "category"),
    [
        (
            "SSL error: certificate verify failed: unable to get local issuer certificate",
            "tls_verification_failed",
        ),
        (
            f'server certificate for "{DIRECT}" does not match host name "{SHARED}"',
            "tls_verification_failed",
        ),
        (
            f'server certificate for "{DIRECT}" (and 2 other names)'
            f' does not match host name "{SHARED}"',
            "tls_verification_failed",
        ),
        (f'FATAL:  password authentication failed for user "{USERNAME}"', "authentication_failed"),
        ("FATAL: startup refused", "unknown_connection_error"),
        (
            f'FATAL:  password authentication failed for user "{USERNAME}" extra',
            "unknown_connection_error",
        ),
    ],
)
def test_libpq_startup_patterns(run_preflight, reason, category):
    error = psycopg.OperationalError(f'connection to server at "{DIRECT}" failed: {reason}')
    assert error.sqlstate is None
    _, result, _, _ = run_preflight(dsn(), connection_error=error)
    assert result["connection_error"] == category


def test_psycopg_wrapped_dns_failure(run_preflight, monkeypatch):
    from psycopg import _conninfo_attempts

    resolver = MagicMock(side_effect=socket.gaierror(socket.EAI_NONAME, "synthetic error"))
    monkeypatch.setattr(_conninfo_attempts, "_resolve_hostnames", resolver)
    with pytest.raises(psycopg.OperationalError) as error:
        _conninfo_attempts.conninfo_attempts({"host": DIRECT})
    _, result, _, _ = run_preflight(dsn(), connection_error=error.value)
    assert result["connection_error"] == "network_unreachable"


@pytest.mark.parametrize(
    ("first_reason", "last_error", "category"),
    [
        ("Network is unreachable", "No route to host", "network_unreachable"),
        ("Network is unreachable", "timeout expired", "unknown_connection_error"),
        ("unrecognized", "Network is unreachable", "unknown_connection_error"),
        ("Network is unreachable", psycopg.errors.ConnectionTimeout, "unknown_connection_error"),
        ("timeout expired", psycopg.errors.ConnectionTimeout, "connect_timeout"),
        ("Network is unreachable", psycopg.errors.InvalidPassword, "unknown_connection_error"),
    ],
)
def test_actual_psycopg_aggregate_requires_every_attempt_to_agree(
    run_preflight, monkeypatch, first_reason, last_error, category
):
    def server_error(reason):
        return psycopg.OperationalError(f'connection to server at "{DIRECT}" failed: {reason}')

    if last_error is psycopg.errors.ConnectionTimeout:
        last = last_error("connection timeout expired")
    elif last_error is psycopg.errors.InvalidPassword:
        last = last_error(f'synthetic error: rejected "{USERNAME}"')
    else:
        last = server_error(last_error)
    connector = MagicMock(side_effect=[server_error(first_reason), last])
    with monkeypatch.context() as scoped:
        scoped.setattr(psycopg.Connection, "_connect_gen", connector)
        with pytest.raises(psycopg.OperationalError) as error:
            psycopg.connect(
                host=f"{DIRECT},{DIRECT}",
                hostaddr="192.0.2.1,192.0.2.2",
                port="5432",
                sslmode="verify-full",
            )
    assert connector.call_count == 2
    _, result, _, _ = run_preflight(dsn(), connection_error=error.value)
    assert result["connection_error"] == category


def test_pgconn_metadata_alone_is_not_an_authentication_failure(run_preflight):
    pgconn = MagicMock(needs_password=True, used_password=True)
    error = psycopg.OperationalError("synthetic error", pgconn=pgconn)
    _, result, _, _ = run_preflight(dsn(), connection_error=error)
    assert result["connection_error"] == "unknown_connection_error"


def test_unparsed_attempt_and_actions_commands_stay_out_of_output(
    run_preflight, monkeypatch, tmp_path
):
    files = ("GITHUB_OUTPUT", "GITHUB_ENV", "GITHUB_STATE", "GITHUB_STEP_SUMMARY")
    for name in files:
        path = tmp_path / name
        path.touch()
        monkeypatch.setenv(name, str(path))
    url = dsn(user=USERNAME)
    error = psycopg.OperationalError(
        f'connection to server at "{DIRECT}" failed: Network is unreachable\n'
        "Multiple connection attempts failed. All failures were:\n"
        f"- host: '{DIRECT}', port: '5432', hostaddr: '192.0.2.1': "
        f'connection to server at "{DIRECT}" failed: Network is unreachable\n'
        f"::error::synthetic error: {url} {FINGERPRINT}\n"
        f"::add-mask::{PASSWORD}"
    )
    status, result, _, _ = run_preflight(url, connection_error=error)
    assert status == 1
    assert result["connection_error"] == "unknown_connection_error"
    assert all((tmp_path / name).read_bytes() == b"" for name in files)


@pytest.fixture(scope="module")
def certificates(tmp_path_factory):
    directory = tmp_path_factory.mktemp("synthetic-certs")
    result = {}
    for name, constraint in (("project", "TRUE"), ("leaf", "FALSE")):
        config = directory / f"{name}.conf"
        config.write_text(
            "[req]\ndistinguished_name=dn\nx509_extensions=ext\nprompt=no\n"
            f"[dn]\nCN=synthetic-{name}\n[ext]\nbasicConstraints=critical,CA:{constraint}\n",
            encoding="ascii",
        )
        path = directory / f"{name}.pem"
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-noenc",
                "-days",
                "2",
                "-config",
                str(config),
                "-keyout",
                str(directory / f"{name}.key"),
                "-out",
                str(path),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        result[name] = path.read_bytes()
    return result


def encode_ca(pem):
    return base64.b64encode(pem).decode("ascii")


def fake_dns():
    return MagicMock(
        return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("192.0.2.1", 6543)),
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("192.0.2.2", 6543)),
        ]
    )


@pytest.mark.parametrize(
    "kind", ["missing", "bad_base64", "bad_pem", "bad_der", "leaf", "extra_pem", "trailing_der"]
)
def test_invalid_project_ca_fails_before_dns_or_connection(run_preflight, certificates, kind):
    pem = certificates["project"]
    der = base64.b64decode(b"".join(pem.splitlines()[1:-1]))
    ca = {
        "missing": "",
        "bad_base64": "not+base64!",
        "bad_pem": encode_ca(b"synthetic error"),
        "bad_der": encode_ca(b"-----BEGIN CERTIFICATE-----\nYQ==\n-----END CERTIFICATE-----\n"),
        "leaf": encode_ca(certificates["leaf"]),
        "extra_pem": encode_ca(pem + pem),
        "trailing_der": encode_ca(
            b"-----BEGIN CERTIFICATE-----\n"
            + base64.b64encode(der + b"x")
            + b"\n-----END CERTIFICATE-----\n"
        ),
    }[kind]
    resolver = fake_dns()
    code, result, connect, _ = run_preflight(dsn(), mode="ca_compare", ca=ca, resolver=resolver)
    assert code == 1
    assert result["policy_review_status"] == "invalid_project_ca"
    assert all(row["status"] == "not_attempted" for row in result["ca_comparison"].values())
    resolver.assert_not_called()
    connect.assert_not_called()


def test_comparison_fingerprint_mismatch_precedes_ca_validation(run_preflight, monkeypatch):
    validator = MagicMock()
    monkeypatch.setattr(subprocess, "run", validator)
    resolver = fake_dns()
    code, result, connect, _ = run_preflight(
        dsn(), "f" * 64, mode="ca_compare", ca="invalid", resolver=resolver
    )
    assert code == 1
    assert result["policy_review_status"] == "project_mismatch"
    validator.assert_not_called()
    resolver.assert_not_called()
    connect.assert_not_called()


@pytest.mark.parametrize("system_ok", [False, True])
@pytest.mark.parametrize("project_ok", [False, True])
def test_ca_comparison_is_single_address_read_free_and_cleans_up(
    run_preflight, certificates, tmp_path, system_ok, project_ok
):
    pem = certificates["project"]
    resolver = fake_dns()
    system, project = MagicMock(), MagicMock()
    paths = []

    def connect(**kwargs):
        if kwargs["sslrootcert"] == "system":
            if not system_ok:
                raise psycopg.OperationalError("synthetic error")
            return system
        if system_ok:
            system.close.assert_called_once()
        path = Path(kwargs["sslrootcert"])
        paths.append(path)
        assert path.parent.parent == tmp_path
        assert path.read_bytes() == pem
        if os.name == "posix":
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
            assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        if not project_ok:
            raise psycopg.errors.InvalidPassword("synthetic error")
        return project

    code, result, mocked_connect, _ = run_preflight(
        dsn(SHARED, f"{USERNAME}.{PROJECT}", "6543", "?sslmode=disable"),
        mode="ca_compare",
        ca=encode_ca(pem),
        resolver=resolver,
        connection_error=connect,
    )
    assert code == (0 if system_ok and project_ok else 1)
    assert result["policy_review_status"] == (
        "ca_comparison_complete" if system_ok and project_ok else "ca_comparison_failed"
    )
    assert result["connection_ok"] is False
    assert result["transaction_read_only"] == "unknown"
    assert result["table_exists"] == "unknown"
    assert result["ca_comparison"] == {
        "system": {
            "status": "connected" if system_ok else "connection_failed",
            "connection_error": None if system_ok else "unknown_connection_error",
        },
        "project": {
            "status": "connected" if project_ok else "connection_failed",
            "connection_error": None if project_ok else "authentication_failed",
        },
    }
    resolver.assert_called_once()
    assert mocked_connect.call_count == 2
    system_params, project_params = [call.kwargs.copy() for call in mocked_connect.call_args_list]
    assert system_params.pop("sslrootcert") == "system"
    assert project_params.pop("sslrootcert") == str(paths[0])
    assert system_params == project_params
    assert system_params == {
        "host": SHARED,
        "hostaddr": "192.0.2.1",
        "port": "6543",
        "user": f"{USERNAME}.{PROJECT}",
        "password": PASSWORD,
        "dbname": "postgres",
        "sslmode": "verify-full",
        "gssencmode": "disable",
        "connect_timeout": 5,
        "prepare_threshold": None,
        "autocommit": True,
        "target_session_attrs": "any",
    }
    assert system.close.call_count == int(system_ok)
    assert project.close.call_count == int(project_ok)
    system.execute.assert_not_called()
    project.execute.assert_not_called()
    assert not paths[0].exists()
    assert not paths[0].parent.exists()


def test_unclosed_system_connection_stops_comparison_and_cleans_ca(
    run_preflight, certificates, tmp_path
):
    conn = MagicMock()
    conn.close.side_effect = RuntimeError("synthetic error")
    code, result, connect, _ = run_preflight(
        dsn(),
        mode="ca_compare",
        ca=encode_ca(certificates["project"]),
        resolver=fake_dns(),
        connection_error=lambda **_: conn,
    )
    assert code == 1
    assert result["policy_review_status"] == "cleanup_failed"
    assert result["ca_comparison"]["project"]["status"] == "not_attempted"
    connect.assert_called_once()
    conn.close.assert_called_once()
    assert not list(tmp_path.glob("preflight-ca-*"))


def test_ca_file_is_cleaned_on_termination(run_preflight, certificates, tmp_path):
    paths = []

    def stop(**kwargs):
        if kwargs["sslrootcert"] == "system":
            return MagicMock()
        paths.append(Path(kwargs["sslrootcert"]))
        assert paths[0].exists()
        raise SystemExit(1)

    code, _, connect, _ = run_preflight(
        dsn(),
        mode="ca_compare",
        ca=encode_ca(certificates["project"]),
        resolver=fake_dns(),
        connection_error=stop,
        interrupted=True,
    )
    assert code == 1
    assert connect.call_count == 2
    assert not paths[0].exists()
    assert not list(tmp_path.glob("preflight-ca-*"))


def test_ca_comparison_does_not_leak_to_runner_outputs(
    run_preflight, certificates, monkeypatch, tmp_path
):
    files = ("GITHUB_OUTPUT", "GITHUB_ENV", "GITHUB_STATE", "GITHUB_STEP_SUMMARY")
    for name in files:
        path = tmp_path / name
        path.touch()
        monkeypatch.setenv(name, str(path))
    pem = certificates["project"]
    url = dsn(user=USERNAME)
    secret = encode_ca(pem)

    def fail(**kwargs):
        raise psycopg.OperationalError(
            f"::error::synthetic error {url} {FINGERPRINT} {secret} {kwargs['sslrootcert']}\n"
            f"{pem.decode('ascii')}\n::add-mask::{PASSWORD}"
        )

    _, result, connect, _ = run_preflight(
        url,
        mode="ca_compare",
        ca=secret,
        resolver=fake_dns(),
        connection_error=fail,
    )
    assert result["policy_review_status"] == "ca_comparison_failed"
    assert connect.call_count == 2
    assert all((tmp_path / name).read_bytes() == b"" for name in files)
    assert not list(tmp_path.glob("preflight-ca-*"))


def test_catalog_ignores_ca_and_preserves_single_readonly_connection(run_preflight):
    code, result, connect, conn = run_preflight(dsn(), ca="invalid")
    assert code == 0
    assert "ca_comparison" not in result
    assert result["transaction_read_only"] is True
    assert connect.call_args.kwargs["sslrootcert"] == "system"
    assert conn.execute.call_args_list[0].args == ("BEGIN READ ONLY",)
    assert conn.execute.call_args_list[-1].args == ("ROLLBACK",)


@pytest.mark.parametrize("offset", [-3600, 259200])
def test_project_ca_outside_validity_period_fails_closed(
    run_preflight,
    certificates,
    monkeypatch,
    offset,
):
    now = time.time() + offset
    monkeypatch.setattr(time, "time", lambda: now)
    resolver = fake_dns()
    code, result, connect, _ = run_preflight(
        dsn(),
        mode="ca_compare",
        ca=encode_ca(certificates["project"]),
        resolver=resolver,
    )
    assert code == 1
    assert result["policy_review_status"] == "invalid_project_ca"
    resolver.assert_not_called()
    connect.assert_not_called()


def test_comparison_dns_failure_does_not_connect_or_create_ca_file(
    run_preflight, certificates, tmp_path
):
    resolver = MagicMock(side_effect=socket.gaierror("synthetic error"))
    code, result, connect, _ = run_preflight(
        dsn(),
        mode="ca_compare",
        ca=encode_ca(certificates["project"]),
        resolver=resolver,
    )
    assert code == 1
    assert result["policy_review_status"] == "ca_resolution_failed"
    connect.assert_not_called()
    assert not list(tmp_path.glob("preflight-ca-*"))


def test_invalid_mode_fails_closed(run_preflight):
    code, result, connect, _ = run_preflight(dsn(), mode="unrecognized")
    assert code == 1
    assert result["policy_review_status"] == "invalid_diagnostic_mode"
    connect.assert_not_called()
