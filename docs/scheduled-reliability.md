# Scheduled scan reliability

Production keeps the existing 07:30, 08:30 and 09:30 America/New_York cron.
Scheduled invocation admission is 07:00 inclusive to 15:00 exclusive on the
current local day (`MONITOR_HOUR=7`, `SCHEDULE_GRACE_HOURS=8`). Admission is
checked once; a scan admitted before 15:00 may finish later. Other callers retain
their existing grace-period defaults and explicit run-key override behavior.

The existing daily database key prevents another scan after a successful run or
while a fresh claim is running. Failed/partial runs and stale scheduled claims
(120 minutes) remain retryable. Claim rejection does not prove a previous success.

CLI JSON and persisted run stats expose `scan_outcome`:

- `scan_completed`: every attempted source finished processing successfully.
- `scan_partial`: some sources succeeded, with unfinished/failed processing.
- `scan_failed`: no source completed successfully, including failure before scanning.
- `skipped_outside_window`: rejected before pipeline construction.
- `skipped_daily_claim_unavailable`: the existing daily claim could not be acquired.

`scan_outcome` describes scanning, not Telegram delivery or overall database run
status. A completed scan with a Telegram error can still have a retryable partial
database run. No-source invocations are not reported as completed scans.

Scheduled commands write a compact status file for an always-run GitHub job
summary: local decision time, exclusive cutoff, intended daily key and current
invocation source/individual-alert counters. Individual-alert counts exclude
summaries and failure messages. A blocked claim's zero counters describe only
the skipped invocation, not the prior scan. Missing reports are explicitly
unknown (for example, configuration failure or forced termination); no completed
scan is inferred from a green workflow. Existing CLI skip exit codes are preserved.

Delivery still waits until 09:00 locally when early and proceeds immediately when
late. Existing outbox, limits, eligibility and matching behavior are unchanged.
Retries can repeat summaries, and Telegram acceptance followed by a crash before
database acknowledgement can repeat an individual message. This patch does not
provide exactly-once delivery or recover a trigger GitHub never creates.
