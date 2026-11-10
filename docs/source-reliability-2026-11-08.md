# November 8 source reliability investigation

Investigated November 9, 2026, using the current source configuration, adapters,
focused tests, downloaded Actions logs, and read-only requests to official career
sites. No production scans, notifications, database writes, or deployment were
triggered. Existing uncommitted Workday pagination and scheduling changes were
preserved; they are not fixes made by this investigation.

## Findings and changes

| Source / symptom | Evidence and classification | Action |
| --- | --- | --- |
| Eli Lilly: `LILLUSR113187EXTERNALENUS` | November 6–8 logs report the same missing-location exception. On November 9 the exact listing is present at offset 440, with `jobId=R-113187`, title “Senior Director, European Regulatory Strategy Lead,” and `BASINGSTOKE, Hampshire, United Kingdom` in `location`, `cityStateCountry`, `multi_location`, and `multi_location_array`. The existing parser recognizes this evidence. | No adapter change. Regression verifies the exact identity with current location evidence and verifies that absent evidence still fails the entire inventory. |
| 29 Workday location warnings | All 29 November 8 exclusions are Otsuka: 28 Princeton, NJ paths and one Rockville, MD path. The configured pattern recognized remote labels and spelled-out US country labels, but neither city/state label. Live R13232 and R11168 detail responses explicitly pair those labels with `country.descriptor=United States of America`; R13232 also has requisition country `US`. Confirmed source configuration defect. | Add only the two observed labels to Otsuka’s pattern. Existing per-posting country validation remains mandatory. Tests accept explicit US country and reject missing/foreign country, without inferring remote work. |
| Charter Research: zero | The HTML contains `data-load-careers="fetch_careers_jobs_markup"` and “Loading careers...”, not the job inventory. The site's theme JS submits that action to its same-domain WordPress AJAX endpoint. That endpoint returns eight job rows. Confirmed incomplete retrieval. | When that exact loader is present, POST the published action to the same-origin endpoint and parse the existing table format. HTTP errors or a missing returned table fail the source. Live smoke check retrieved eight jobs. |
| Alexion: zero | Configured TalentBrew results endpoint returns HTTP 200 with `{"filters":"","results":"","hasJobs":true,"hasContent":false}`. The adapter parsed this JSON as HTML and returned zero. Confirmed false-success defect; no complete Alexion inventory was retrieved. | Source-specific guard rejects the unsupported/incomplete results envelope instead of claiming an empty scan. Retrieval remains unresolved and visibly failed. |
| Apogee: zero | Configured Greenhouse API returns `{"jobs":[],"meta":{"total":0}}`. The official careers page's JS calls the same board API. Static role cards on that page are templates replaced by the API response, not independent proof of current vacancies. | No change: verified empty configured public inventory at inspection time. |
| Cullinan: zero | Configured Lever API returns `[]`; the corresponding public Lever board says “No job postings currently open. Check back later!” The corporate careers page returned HTTP 403. | No change: verified empty configured public board; no claim about inaccessible corporate content or unpublished jobs. |

## Lilly reproduction limits

The historical exception originates in `PhenomSource.fetch()` after `_locations()`
returns no evidence. That raises `SourceError` and discards the whole fetch; it
does not return a successful partial inventory. This is deliberate fail-closed
handling, not a scan-level swallowing of the failure.

The original November 6–8 listing payloads were not retained in the available
logs. Therefore the historical missing-data condition cannot now be reproduced
against unchanged upstream content, nor attributed conclusively to missing
upstream fields versus a formerly unrecognized field representation. The current
payload has `postedDate=2026-11-09`, consistent with an upstream update, but that
alone does not prove the historical cause. The exact current ID parses correctly;
no location fallback, invented location, silent skip, or source-success downgrade
was introduced. A preliminary URL using `R113187` returned HTTP 410; the actual
payload's job ID is `R-113187`, so that response is not evidence the listing closed.
The search `keywords` parameter did not narrow the embedded inventory, so the
exact listing was located by normal offset pagination instead.

## Alexion limits

The full AstraZeneca search page returns parent-company results even with the
configured Alexion query parameter; the first parsed result was an unrelated
CVRM sales role in Brazil. Substituting that page would risk labeling parent
company vacancies as Alexion. No such fallback was implemented. A future repair
needs a verified brand-scoped endpoint/filter and complete pagination evidence.
Until then this source must remain visibly incomplete rather than contribute a
successful zero-job scan. The fix does not claim to restore Alexion coverage.

## Validation

Focused Phenom, Otsuka, Charter, TalentBrew, Workday completeness, and four-Workday
tests: 67 passed, two failed. Both failures are pre-existing TalentBrew tests
whose fixed October 1, 2026 closing date is now expired; running those same two
tests against `HEAD`'s source reproduced identical failures. They were not changed
to alter active-status behavior. All new regression cases passed. Ruff lint,
format checks for changed source/tests, and whitespace checks passed. Another 137
existing source, wave-one, diagnostics, and Premier Research tests passed.

Live smoke verification retrieved eight Charter jobs, observed Alexion raising
`SourceError`, and confirmed Otsuka Princeton/Rockville US country evidence.
No claim is made that a full production run has succeeded with these edits.

## Evidence

- [November 8 Actions run](https://github.com/alanisss27/job-radar-tw/actions/runs/42507664309)
- [November 7 Actions run](https://github.com/alanisss27/job-radar-tw/actions/runs/42398066632)
- [November 6 Actions run](https://github.com/alanisss27/job-radar-tw/actions/runs/42280966800)
- [Lilly inventory](https://careers.lilly.com/us/en/search-results/?from=440&s=1)
- [Charter roles](https://www.charterresearch.com/open-roles/)
- [Apogee careers](https://www.apogeetherapeutics.com/careers)
- [Cullinan public Lever board](https://jobs.lever.co/cullinanoncology)

Downloaded logs, raw API/page responses, and read-only probe scripts are retained
locally under ignored `.tmp/source-reliability-20261108/` and
`.tmp/source_reliability_*.py` / `.tmp/lilly_inventory_probe.py`.

## Deployment preparation review

Readiness: **Ready for a source-reliability-only commit**, with the remaining
Lilly/Alexion limitations above. No commit, push, deployment, or backfill has
been performed. No additional live searches were needed for this review.

The reviewed release copy is based on `HEAD` and contains only these seven files:

- `config/companies.yml`: only Otsuka's two exact city/state alternatives.
- `src/job_monitor/sources.py`: only the Charter `fetch()` loader hunk and the
  Alexion-specific JSON-envelope rejection in `TalentBrewSource.fetch()`.
- `tests/test_charter_research.py`, `tests/test_otsuka.py`,
  `tests/test_phenom.py`, `tests/test_talentbrew.py`: the 14 new regression cases
  and their required imports.
- This report.

The large `WorkdaySource` hunk in the working copy is excluded; the isolated
release's entire Workday class matches `HEAD` exactly. Also excluded are
`tests/test_workday_completeness.py`, `.github/workflows/monitor.yml`,
`tests/test_schedule_reliability.py`, `docs/scheduled-reliability.md`, and
`docs/notification-incident-2026-11-06.md`. Existing edits remain in place.
Do not stage the whole working-copy `sources.py`; select only its two
source-specific hunks or use the prepared isolated patch.

Review found no changes to matching, scoring, remote eligibility, scheduling, or
shared location-validation code. Otsuka still requires existing detail/country
evidence; the added labels do not independently establish eligibility. Charter's
loader uses a fixed same-origin path and exact published action, with HTTP and
table-presence checks. The Alexion guard deliberately rejects dictionary JSON
envelopes even if they later contain results: parsing and proving completeness
of that response format remains unsupported. It does not restore coverage.

Isolated validation: **194 passed, two known baseline failures, no new test
failures** across the ten relevant suites. All 14 new regressions passed. The
earlier 204-pass working-copy result included ten pending Workday tests excluded
from this release. Both TalentBrew failures were rerun against `HEAD`'s source
and reproduced the same expired-date assertions. Ruff lint/format and patch
checks passed. The initial external-network guard also blocked Windows asyncio's
internal loopback sockets; allowing loopback corrected that validation harness
error before the successful rerun. Product code was not changed for the harness.

Bounded validation replayed saved official responses through the isolated
adapters with external networking blocked: Charter returned eight jobs; both
Otsuka US detail payloads were accepted without warnings; Alexion raised
`SourceError`; and Lilly's exact captured listing retained its UK location.
No current production availability or full production scan is inferred from
this offline replay.

The index remains empty. `.tmp/` is ignored, has no tracked files, and has no
staged diagnostic artifacts. The review copy, seven-file patch, manifest, JUnit
results, and bounded-replay results are local artifacts under
`.tmp/source-reliability-release-review/`; they are excluded from the commit.

Recommended commit message:
`fix(sources): correct Otsuka locations and Charter/Alexion inventory handling`
