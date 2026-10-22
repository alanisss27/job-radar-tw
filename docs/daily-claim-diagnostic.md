The manually dispatched `Read-only daily claim diagnostic` workflow accepts one
`run_key` in `daily-YYYY-MM-DD` format and runs only from `main`. It uses the
existing `DATABASE_URL` repository secret. It does not load private matching
preferences or Telegram credentials, initialize schemas, claim runs, or invoke
the monitor. No production workflow needs to be changed.

The standalone entry point is `python -m job_monitor.claim_diagnostic --run-key
daily-2026-10-22`. PostgreSQL startup options enforce read-only transactions and
bounded connection/query/lock waits. The command also requests and verifies
transaction read-only mode before a parameterized SELECT against
`public.source_runs`, then rolls back and closes on both success and failure.
Unsupported database URLs, invalid dates, unrecognized statuses, and connection
or read-only enforcement failures produce a generic error with no driver details.

Output contains only the requested key, existence, allowlisted claim status,
timestamps (UTC), age, and retry indicators. It excludes UUIDs, stats, errors,
job details, and credentials. A successful status blocks retries. Failed/partial
statuses are retryable under existing application rules. Running claims become
reclaimable at 55 minutes for manual invocations and 120 minutes for scheduled
invocations. These indicators describe current code behavior, not permission to
retry: elapsed claim age cannot prove owner liveness. Recheck Actions before any
separately approved recovery, and conservatively protect running claims for 120
minutes. No observation reserves a claim or eliminates races with later events.

The existing production credential is not a SELECT-only database role. PostgreSQL
enforces read-only mode for this command, but a future code change could disable
it. Standard review and protection of `main` and repository Actions permissions
remain essential. A dedicated SELECT-only role would strengthen isolation but
requires separately approved credential/configuration work. No workflow has been
dispatched as part of adding this diagnostic.

Isolated PostgreSQL verification
-------------------------------

`tests/test_claim_diagnostic_postgres.py` is an opt-in live integration suite.
Provision a disposable local PostgreSQL cluster listening only on `127.0.0.1`
on port `55435`, with a non-superuser login role `claim_test_owner` owning the
database `claim_diagnostic_test`. Use local test authentication only; never copy
production credentials into this cluster. The suite does not read `DATABASE_URL`
for its connection. Its hostname, database name, and role are fixed test values;
the optional `CLAIM_DIAGNOSTIC_TEST_PORT` must be a non-default, unprivileged port.

Set `CLAIM_DIAGNOSTIC_POSTGRES_TEST=1` and run
`python -m pytest tests/test_claim_diagnostic_postgres.py -q`. Without explicit
opt-in the live tests skip. The fixture creates/drops `public.source_runs` in
that test database, so never use a database containing valuable data.

The live suite checks absent/success/running/failed/partial rows, parameter binding,
read-only startup defaults, explicit transaction read-only mode, rollback/close
on success and query/result errors, unchanged row snapshots, and PostgreSQL
rejection of deliberately substituted INSERT/UPDATE/DELETE/DDL statements using
the write-capable table owner. A subprocess executes the standalone entry point
with monitor/storage/pipeline/source/notifier imports forbidden. The unit suite
also verifies closing the connection if rollback itself raises.

Read-only mode prevents ordinary changes to permanent tables; it is not a
sandbox against malicious code that intentionally resets transaction settings,
uses separate connections, or invokes privileged external side effects. The
diagnostic contains only its fixed SELECT and read-only setting check. Future
hardening should give a dedicated diagnostic login only CONNECT, public schema
USAGE, and SELECT on the required `source_runs` columns, without table ownership,
write/DDL grants, role inheritance permitting writes, or privileged function
execution. That credential change requires separate approval.

Pre-merge verification on October 22, 2026 used PostgreSQL 17.11 in a disposable
loopback-only cluster with a non-superuser table owner holding write privileges.
All 14 live tests passed, including server-enforced rejection of writes and
unchanged data on error paths. The combined diagnostic, storage, and scheduling
suite passed 94 tests with live tests explicitly enabled. Repository-wide Ruff
lint/formatting and Actionlint for the diagnostic workflow also passed. Production
credentials and databases were not used; no production workflow was dispatched.
