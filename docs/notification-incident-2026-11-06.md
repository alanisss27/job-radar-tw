# November 4–6 notification investigation

Read-only GitHub API inspection found four repository Actions runs since
November 3 00:00 UTC, all scheduled monitor runs on `main` at commit
`ebc59abb38773535c0ca62e3f662d3df490ee096`. No failed or cancelled workflow
runs appear in that interval. A green workflow is not proof of a completed scan.

The deployed workflow is identical to the local workflow before this fix. It
expects 07:30, 08:30 and 09:30 America/New_York invocations each day. Only one
run was recorded each day; GitHub does not expose evidence explaining the missing
attempts or which intended cron occurrence each delayed run represents.

| Date | Actual run creation (New York, EST) | Scan result | Jobs / final matches | Delivery |
| --- | --- | --- | --- | --- |
| Nov 3 | 14:40:13 | Completed, 92/92 sources | 11,513 / 7 | 5 individual alerts; 8 total Telegram HTTP 200 responses |
| Nov 4 | 14:55:52 | Partial, 91/92 sources | 10,517 / 1 | 1 individual alert; 3 total Telegram HTTP 200 responses |
| Nov 5 | 15:09:10 | Skipped outside window at 15:09:22 | No collection or matching performed | No attempt |
| Nov 6 | 14:35:26 | Partial, 90/92 sources; workflow succeeded | 9,884 / 5 | 4 individual alerts; 6 total Telegram HTTP 200 responses |

Run links:
- https://github.com/alanisss27/job-radar-tw/actions/runs/41705413279
- https://github.com/alanisss27/job-radar-tw/actions/runs/41901684334
- https://github.com/alanisss27/job-radar-tw/actions/runs/42100674828
- https://github.com/alanisss27/job-radar-tw/actions/runs/42280966800

November 3 is the last confirmed fully completed scheduled scan, finishing
15:22:58 EST. November 4's workflow finished successfully at 15:30:40 EST,
but the underlying scan was partial. It evaluated 528 new/changed unique jobs
(343 new, 185 changed); one final match qualified for immediate notification.
Its notification counters were sent=1, suppressed=0, pending=0. There were
27 Workday location-validation warnings. Thermo Fisher/PPD failed its strict
pagination coverage check at offset 160 (duplicate/stalled page; initial total
501). No complete Thermo inventory was claimed. This is a source coverage
failure, not evidence that the entire notification path failed. Existing local
uncommitted Workday changes address listing/detail interleaving; this investigation
did not edit those files or establish that they fix the live board behavior.

November 6 finished at 15:15:17 EST. Collection persisted 567 new and 199
changed jobs; 766 unique jobs were evaluated. Five final matches were found,
four qualified for immediate alerts, and all four were sent (suppressed=0,
pending=0). Telegram accepted six requests at 15:15:05–15:15:10 EST, including
the summary chunks. The scan remained partial: Eli Lilly failed because listing
`LILLUSR113187EXTERNALENUS` had no location data; Thermo Fisher/PPD failed a
posting-identity check in query 2/3 at offset 140. There were 28 Workday
location-validation warnings. These source failures are separate from schedule
admission and remain outside this fix. Provider acceptance does not establish
client display or a read receipt.

November 5 is a confirmed admission failure: the run arrived after the exclusive
15:00 cutoff. Its zero job/match counters describe a skipped invocation, not a
successful scan finding zero qualifying jobs. Deduplication and filters were
never reached. The smallest configuration adjustment extends scheduled grace
from eight to ten hours (17:00 exclusive), also covering November 2's observed
16:49 arrival. It preserves the existing cron, daily claim, filters, outbox,
limits, and delivery timing. Later arrivals and absent triggers remain external
limitations; no trigger repair is claimed.

Actions is enabled, the monitor workflow is active, and `main` is the default
branch. Required database, private-preferences, and Telegram secret names exist.
Private preferences provisioning and configuration validation succeeded. Telegram
credentials are scoped to monitor steps; the earlier configuration step's
`Telegram configured: no` output is expected and does not show a missing secret.
No credentials were printed. November 4's HTTP 200 responses establish provider
acceptance, not client display/read receipt or correctness of the intended chat.

Validation: 159 existing and regression tests passed across schedule reliability,
schedule/notifier, pipeline, storage, scheduled preferences, and Workday
completeness suites. The existing isolated validation harness blocked external
networking and restricted databases to test storage. This includes synthetic
pipeline/delivery, claim/outbox recovery, and mocked provider tests; no live
notification was sent. Added regressions use the actual workflow configuration
and observed arrival timestamps, plus the new cutoff and midnight boundary.
Ruff lint/format and `git diff --check` passed.

Activation review used an isolated worktree based on production `main`
(`ebc59ab`), excluding the pending Workday edits. All 153 focused tests passed,
including four additional daylight-time/DST cutoff cases. Strict configuration
validation and Ruff lint/format passed. The full suite yielded 1,548 passed,
146 failed, 14 skipped; clean `main` yielded 1,537 passed, 146 failed, 14 skipped.
The sets of 146 failing test nodes were identical, with no added failures.
The eleven additional passing cases are the schedule admission regressions.

Files changed by this investigation: `.github/workflows/monitor.yml`,
`tests/test_schedule_reliability.py`, `docs/scheduled-reliability.md`, and this
report. Ignored diagnostic helpers/evidence are under `.tmp/`. Pre-existing
changes in `src/job_monitor/sources.py` and `tests/test_workday_completeness.py`
were preserved. Career Ops/application records and production data were not
modified. No workflows were dispatched, secrets changed, or live test messages
sent. Following user authorization, the four reviewed files are prepared on the
existing `fix/scheduled-same-day-catch-up` branch, based on current `main`.
Pushing this branch does not activate the scheduled workflow; activation requires
a merge to `main`. No manual secret/configuration change is indicated.
Production recovery/backfill would require separate approval and is not part of
this fix.
