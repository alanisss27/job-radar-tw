# Transferable Operations: offline inventory replay

**Result: 0 newly discovered jobs and 0 newly eligible matches in a fixed 300-job sample.**
This sample does not establish the profile's recall or precision: every new-profile
decision stopped at seniority/title scope, before its duty and qualification gates.

Usable evidence was verified before replay: `.tmp/wave1-live.json` contains 93
actual saved postings and `.tmp/regeneron-live.json` contains 350. All 443 have
JD text longer than 100 characters and nonempty locations; respectively 31 and 63
also have structured eligibility metadata. These are September source-audit
snapshots, **not the latest production run**. New/changed flags and run linkage
are unavailable, so a recent-new/changed cohort cannot be reconstructed.
Amgen's summary lacks JD text; raw global-control and synthetic test inventories
were not used.

Forty archived Mayo postings were excluded because Mayo has no current company
mapping; no old-profile assignment was invented. Of 403 currently mapped,
enabled records, the sample is the newest 300 by stored `fetched_at`, descending,
with company/job-ID tie breaks. It contains Regeneron 247, Sumitomo Pharma
America 28, and Beacon Biosignals 25, fetched September 19–24, 2026. Both arms
use these exact records. The old assigned profile is `clinical-discovery` for
all three companies; the expanded arm adds `transferable-operations` once.

Current local preferences were reused unchanged. No career candidate or resume
was available. No LLM, hydration, active-status check, adapter, pipeline, notifier,
or database was invoked by the replay. Network/database access was blocked by an
audit hook. Only replay/report artifacts were written.

| Count | Old assigned profiles | Old + transferable operations |
|---|---:|---:|
| Unique jobs evaluated | 300 | 300 |
| Profile decisions | 300 | 600 |
| Discovery passes (profile decisions) | 5 | 5 |
| Unique jobs with a discovery pass | 5 | 5 |
| Discovery passes held for missing eligibility evidence | 4 | 4 |
| Discovery passes with hard candidate exclusions | 1 | 1 |
| Final eligible matches | 0 | 0 |

The supplemental profile contributes **0 discovery passes, 0 qualification-review
passes, 0 hard-excluded passes, and 0 missing-evidence review passes**. Its 300
discovery exclusions are `operations_seniority` **190** and
`operations_title_scope` **110**. No record reached the insufficient-duty or APM
transferability gate. These discovery exclusions are separate from candidate
eligibility: they are not 300 proven qualification/geography failures.

Across all 300 jobs, the independent location/license assessment reports **43
unsuitable**, **231 review-needed**, and **26 passing that assessment only**.
Those 26 are not qualification-confirmed or matched. Hard-reason counts are
`remote_residence_incompatible` 22, `onsite_geography_incompatible` 19, and
`required_license_not_held` 2. Review-reason counts are `commute_unresolved` 235,
`conflicting_work_arrangement` 18, and `remote_residence_unknown` 1. Reason counts
are once per job/code, may overlap, and include reasons on hard-excluded jobs;
they must not be summed into a review/job total. Generic qualification-review
counts for old profiles are **unknown**: absent candidate facts are not proof
of qualification. Source-stage exclusion/validation totals are also **unknown**
because retained postings do not cover omitted inventory.

Discovery overlap and supplemental daily-summary duplicate suppression are both
**0**. The new profile produces **0 alert-eligible results**. No notification was
queued or sent; deduplication is not exercised by positive replay results.

There are **no representative new candidates** to list, and no new positives
whose false-positive rate can be assessed. Actual boundary observations follow;
these are not additions to the candidate count:

| Company / stored title / job ID | Stored JD evidence | Decision and interpretation |
|---|---|---|
| Regeneron — Associate Manager Business Operations — `/job/Armonk/Associate-Manager-Business-Operations_R49982` | Data analysis, dashboards/reporting, SOP drafting, vendor coordination, and tracking action items are assigned duties. JD also requires an MBA (or expected within 12 months), a bachelor's degree, and four years' industry experience; work is onsite in Armonk/Tarrytown, NY. | Old discovery fails; supplemental `operations_title_scope`; candidate `unsuitable`, `onsite_geography_incompatible`. A transferable-duty-rich title boundary, but the manager title, qualifications, and NY attendance prevent treating it as a missed eligible entry-level opportunity. |
| Beacon Biosignals — Clinical Study Operations Associate — `4418079009` | “Collaborate with project teams to support study startup activities.” “Monitor project progress, ensuring adherence to timelines, protocols, and quality standards.” JD requires clinical operations/project experience and limits remote residence to Pacific/Mountain time zones. | Already discovered by the old profile; supplemental `operations_title_scope`; candidate `unsuitable`, `remote_residence_incompatible`. Likely adjacent-title/duty-language recall gap in the supplemental profile, **not an incremental or Florida-eligible miss**. |

No verified Vetcove/APM posting is present in the replay inputs, and there is no
in-scope non-senior title on which to validate real-JD duty detection. Campus
labels without resolved state evidence also leave many Regeneron records at
commute review; this replay does not approve their location or qualifications.
No threshold/profile/location changes were made.

The legacy checks remain baseline-identical: **73 failing node IDs match exactly**
between current code and HEAD matching/profile configuration loaded in memory;
each has 333 passes. Two previously passing tests were deselected to avoid their
mock pipeline/temporary database paths during this read-only check. No suites
were fixed.

Reproducibility artifacts (local, ignored):
`.tmp/replay_transferable_operations.py`,
`.tmp/transferable-operations-replay-2026-10-20.json` (input/config/code hashes,
ordered 300-job IDs/content hashes, and both arms' decisions),
`.tmp/check_transferable_legacy_baseline.py`, and
`.tmp/transferable-operations-legacy-baseline-2026-10-20.json` (exact failing nodes).
