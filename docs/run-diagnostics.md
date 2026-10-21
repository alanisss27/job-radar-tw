Run diagnostics are stored in the existing `source_runs.stats.decision_diagnostics`
JSON object, linked to the run UUID and UTC start/recording times. The CLI run
report also includes this object; the daily summary shows a compact breakdown.
No schema migration or separate per-job diagnostic storage is needed.

Inventory new/changed counts and final matches reuse the existing run report.
Observed/evaluated/matched unique-job counts use company plus external job ID;
profile decisions count final matching evaluations (not preliminary hydration
checks or intermediate LLM evaluations). Unchanged jobs normally are observed
but not evaluated, except during backfills. Discovery passes can still fail
candidate eligibility. Final matches retain the existing profile-result count;
unique matched jobs are also reported separately.

Discovery rejection codes and candidate hard/review reason codes count once per
profile decision per code. Different codes can overlap; their sums are not job
totals. Candidate assessments are counted even when discovery fails. An absent
assessment is `unknown`, not eligible or hard-excluded. Only machine-code prefixes
are retained: no candidate facts, evidence text, job titles, URLs, or resumes.
Each counter has at most 64 distinct codes plus an overflow bucket, so persisted
size does not grow with inventory. The object lives with the existing run record
and follows its retention; no extra history or unbounded per-job artifacts are
created. Unique-ID sets exist only in memory during the run.

Source warnings are separate observations, including Workday location-validation,
detail-request, HTTP, and unavailable-posting failures. Total source exclusions
remain null/unknown because not all adapters expose every exclusion. Unrecognized
warnings are counted separately and are not assumed to be exclusions. Empty
reason maps mean no observed occurrences, not proof of complete source coverage.
Null means unavailable; summaries label it `unknown`.

`complete=false` indicates partial source processing or an interrupted pipeline;
decision counts may then include evaluations not successfully persisted. Existing
inventory/final-match counts reflect completed persistence bookkeeping. Diagnostics
are saved by the existing run-finalization path, including handled exceptions;
an abrupt process termination or database outage can prevent finalization. Dry
runs return diagnostics without persisting them, and duplicate run claims do not
overwrite the original artifact. No historical run counts are reconstructed.
