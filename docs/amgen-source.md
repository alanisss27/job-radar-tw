# Amgen official source

Amgen is enabled for broad U.S. monitoring under `clinical-discovery`.
No discovery, eligibility, attendance, or notification policy was changed.

## Authority and adapter decision

The [official careers site](https://careers.amgen.com/en/search-jobs) is a
TalentBrew frontend (TalentBrew assets and paginated HTML search results).
Its returning-applicant link targets Amgen's Workday `Careers` board.
The [Associate Scientist - Biology page](https://careers.amgen.com/en/job/thousand-oaks/associate-scientist-biology/87/100835499456)
also linked its Apply Now button directly to requisition `R-255768` on that board.
Eightfold is linked for the talent network, not used here for ingestion.

Both public search and Workday returned current listings on 2026-09-19.
The initial unfiltered snapshots reported 1,586 TalentBrew jobs and 1,771
Workday jobs worldwide. These are separate snapshots, not a reconciliation
of individual requisitions; no equivalence between the inventories is assumed.

Use the existing `WorkdaySource`: its structured listing and detail endpoints
provide pagination, descriptions, additional locations, and remote/requirement
metadata without implementing a new TalentBrew HTML crawler. The existing
`JsonLdSource` reads a single page and does not traverse search results.
Talemetry and Jibe are different protocols and do not fit this frontend.

- Board: <https://amgen.wd1.myworkdayjobs.com/en-US/Careers>
- Listing: `https://amgen.wd1.myworkdayjobs.com/wday/cxs/amgen/Careers/jobs`
- Details: `https://amgen.wd1.myworkdayjobs.com/wday/cxs/amgen/Careers{externalPath}`
- U.S. facet: `LocationCountry: [bc33aa3152ec42d4995f4791a106ed09]`
- Page size: 20; `searchText` is empty on every page.

The case-sensitive country facet was observed in the unfiltered response and
verified with a scoped request returning 636 postings. No title/category filter
or client-side title exception is applied. Full descriptions feed the existing
clinical, project, scientific, and GxP discovery rules.

## Limitations

- Scope follows Workday's United States of America facet; it does not imply
  all territories or every job represented on the public frontend is included.
- The shared eligibility parser may not resolve Amgen's state-before-city
  labels such as `US - Florida - Tampa`. Physical attendance remains
  `review_needed` when unresolved, rather than being assumed compatible.
  Existing Florida attendance and remote-residency rules are unchanged.
- Workday detail failures retain listing fields under the existing adapter's
  fallback. Missing evidence must not be treated as proof of candidate eligibility.
- Relative listing dates such as `Posted Yesterday` are not parsed into
  `posted_at` by the existing adapter; detail `startDate` is not substituted.
- Workday is a public careers interface, not a versioned integration contract.
  Counts can change while pagination is in progress.

## Validation

Focused offline validation: 343 tests passed across `test_amgen.py`,
`test_sources.py`, `test_clinical_discovery.py`, and
`test_candidate_eligibility.py`. Ruff and `git diff --check` passed.
Tests cover official configuration, the exact country facet, empty search text,
pagination, broad ingestion including nonmatching sales roles, detail metadata,
positive and negative discovery, and all three candidate eligibility states.

Live verification uses only Amgen's configured adapter, without running the
pipeline, writing the application database, or sending notifications. Its
request budget is 750 and overall timeout is 10 minutes.

Completed 2026-09-19 at 18:03:40 UTC: **636 unique U.S. postings**, from 32
listing POSTs and 636 detail GETs (668 requests). All descriptions were populated;
the shortest normalized description was 3,749 characters. No detail failure
warnings were emitted. The fetched count matched the observed U.S. facet total.

Using the checked-in profile and preferences, with candidate eligibility disabled
only for a separate discovery measurement, **zero jobs passed discovery**. The
highest discovery score was 0.30. No threshold or title rule was adjusted.
Representative ingested results (all below the existing discovery threshold):

| Requisition | Title | Discovery score |
| --- | --- | --- |
| R-255827 | Senior Project Coordinator | 0.00 |
| R-254934 | Project Manager | 0.20 |
| R-253014 | Clinical Development Director, Early Development Oncology | 0.20 |
| R-253104 | Sr Scientist, Molecular Biology Scientific Data | 0.30 |
| R-250442 | Principal Scientist - Clinical Pharmacology Modeling and Simulation | 0.30 |

The separate candidate assessment using checked-in preferences returned 513
`review_needed` and 123 `eligible` assessments; these are policy assessments,
not discovery matches or personal qualification endorsements. Notification
eligibility was zero. Private candidate/preferences overrides were not loaded.
One detail (`R-245178`) combined a U.S. country/remote location with an `ES`
requisition country code, illustrating that ATS location metadata can conflict.

The local verification script and detailed result snapshot are ignored artifacts
at `.tmp/verify_amgen.py` and `.tmp/amgen-live.json`. No production run, database
update, notification, commit, or push was performed.
