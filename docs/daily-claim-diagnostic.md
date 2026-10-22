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
