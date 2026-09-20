# Regeneron official source

Regeneron is enabled for broad U.S.-available monitoring under
`clinical-discovery`. Discovery rules, thresholds, compensation, candidate
eligibility, and other source configurations are unchanged.

## Authority and architecture

The [official careers frontend](https://careers.regeneron.com/en/jobs/) links
applications to Regeneron's Workday `Careers` board. The
[Clinical Study Specialist R50053 page](https://careers.regeneron.com/en/jobs/r50053/clinical-study-specialist/)
confirms the Workday Apply link and lists UK and U.S. locations.

- Board: <https://regeneron.wd1.myworkdayjobs.com/Careers>
- Listing: `https://regeneron.wd1.myworkdayjobs.com/wday/cxs/regeneron/Careers/jobs`
- Detail: `https://regeneron.wd1.myworkdayjobs.com/wday/cxs/regeneron/Careers{externalPath}`
- Adapter: existing `WorkdaySource`, with opt-in location validation and evidence
  preservation. No new ATS adapter or frontend scraper is needed.
- Empty `searchText`, page size 20, no title/category filters. Descriptions are
  enriched from the official detail endpoint before discovery.

## U.S. scope

Each run reads the unfiltered nested `locationMainGroup` / `locations` facets,
resolves the current IDs using the configured anchored label pattern, and sends
all matching IDs together under `appliedFacets.locations`. This is an OR union
of locations, including additional locations, deduplicated by `externalPath`.
Facet counts overlap and must not be summed to count jobs.

The pattern covers nationwide city/state-abbreviation labels, `Remote - United
States`, `Remote - <U.S. state or DC>`, and these audited campus labels:

`Armonk`, `Cambridge`, `Hawthorne`, `Los Angeles`, `Saratoga Springs`, `Seattle`,
`SLEEPY HOLLOW`, `TARRYTOWN`, `Warren`, `Washington DC`, `RENSSELAER`,
`RENSS - BLD17 Filling`, `RENSS - GLOBAL VIEW`, `RENSS - MENANDS`, `RENSS - SUNY`,
`RENSS - TECH VALLEY`, and `RENSS - TEMPEL LN`.

This is source inventory scoping, not a commute allowlist. It uses no geocoding,
distance, routing, polygons, or Florida city allowlist. The exact regex is in
`config/companies.yml`; unknown campus names need a new official-source audit.

Official detail country fields validated the named primary campuses, including
potentially ambiguous `Cambridge`, `Warren`, and `Hawthorne`. `Los Angeles` and
`Seattle` occur as additional locations on U.S. regional medical-affairs roles
(R50518/R50243 and R50437), alongside their state-qualified labels and region
descriptions. All U.S.-primary posting locations in the global detail snapshot
matched the pattern. The separate unlocated requisition caveat is below.

Reliability checks on 2026-09-19:

- Global API total: **529**; global pagination retrieved **529 unique postings**.
- `locationCountry: [bc33aa3152ec42d4995f4791a106ed09]` also returned **529**.
  This broken filter is absent from the production configuration and is never
  used as proof of U.S. scope.
- Resolved **113 U.S. location facets**. Their displayed counts sum to **593**,
  because postings can have several locations.
- The combined location query reported **350**, and all 18 pages returned
  **350 unique postings**. A foreign `Uxbridge1`-only control returned **11**.
- The combined result set exactly matched the global detail snapshot's set of
  postings with a validated U.S. primary or additional location: no missing or
  extra paths. **349** had a U.S. primary country, **one** a UK primary country.

The Regeneron configuration also enables `validate_location_facets` and sets
`location_facet_country` to `United States of America`. Every detail must confirm
a matching primary or additional location. When only the primary location
confirms scope, its actual detail country must agree. Missing facets, malformed
locations, failed detail requests, or unsupported scope raise `SourceError`;
there is no silent global or listing-only fallback for this configuration.
Existing sources retain their previous behavior because these options are opt-in.

## Primary and additional locations

`metadata.workday_locations` retains the original primary location, primary
country object, additional location list, requisition-location object, listing
summary, configured facet country, and validated additional labels separately.
The original listing remains in `metadata.workday`.

`location_raw` starts with the actual primary location and country/code, followed
by alternatives. Only validated U.S. additional labels receive a U.S. country
annotation, allowing unchanged discovery to recognize U.S. availability without
overwriting a foreign primary country. No state or city is inferred for campus
labels. Full descriptions retain attendance restrictions and other requirements.

R50053 remains primary `Uxbridge1`, country `United Kingdom`, code `GB`, with
additional `Armonk` and `Warren` identified as U.S. alternatives. The description
emphasizes Uxbridge hybrid work despite the advertised alternatives. Existing
tri-state eligibility returns **review_needed**, not an automatic notification.
This is an availability lead requiring recruiter/attendance clarification, not
proof of candidate suitability or permission to work from any U.S. location.

## Live discovery results

Final verification completed **2026-09-19 22:35:46 UTC**. All 350 scoped
descriptions were populated (minimum normalized length: 4,263 characters).
Checked-in profile and preferences were used, with candidate eligibility
disabled only for the separate discovery measurement; no private overrides,
LLM calls, pipeline run, database writes, or notifications were used.

| Requisition | Retrieved role | Discovery outcome |
| --- | --- | --- |
| R50053 | Clinical Study Specialist | **Pass, 0.90**; existing evidence-gated clinical support path |
| R48805 | Clinical Study Associate Manager | **No, 0.00**; `discovery_responsibility_evidence` |
| R50249 | Senior Manager, Clinical Study Lead | **No, 0.10**; below threshold |
| R47182 / R49337 | Manager, Clinical Study Lead / Manager Clinical Study Lead | **No, 0.10** each |
| R49309 | Senior Medical Director, Clinical Development, Cell Therapy | **No, 0.10** |
| R49635 | Associate Medical Director, Clinical Development, Ophthalmology | **No, 0.20** |
| R49601 | Senior Project Engineer, CMO Support | **No, 0.30** |
| R48600 | Senior Project Manager - Engineering & Automation | **No, 0.00**; responsibility gate |
| R49085 | Sr. Process Controls & Validation Engineer | **Pass, 1.00**; existing responsibility path |

R50053 supplies study-reporting, meeting-coordination, and trial-document
support evidence. R48805 is not promoted into the narrower transition-role path
just because its title begins with Clinical Study Associate. No standalone
Clinical Study Associate posting appeared in the global snapshot.

There were **two discovery matches**, both **review_needed**, and **zero
notification-eligible postings**. Across all 350 jobs, candidate assessment
returned 310 `review_needed`, 38 `eligible`, and two `unsuitable`; these counts
are separate from discovery and do not establish personal qualifications.

The bounded global audit used 556 API requests (27 listings, 529 details;
600-request/10-minute cap). A second scoped adapter run completed retrieval
with 372 requests but its report failed on an audit-script field-name error.
After correcting that reporting error, final verification of the completed
implementation used **39 live requests** (22 listing/control POSTs and 17
clinical/project detail GETs), replaying the other 333 details from the same
session's global official snapshot. Its cap was 60 live requests/10 minutes.
The successful final scoped adapter run and discovery analysis are recorded in
`.tmp/regeneron-live.json`; the global snapshot is `.tmp/regeneron-global.json`.
These ignored artifacts and scripts are local audit evidence, not production data.

## Limitations

- Workday is a public, unversioned interface; facet labels, IDs, and jobs can
  change during pagination. IDs resolve afresh, but new opaque campus labels
  require validation before inclusion. Runtime detail checks detect contradictory
  or unconfirmed scope, not every possible future omission.
- One global posting, R50541, had no advertised `location` or country and was
  absent from the location-facet union, although its requisition location was
  TARRYTOWN/US. The 350 count is verified advertised-location coverage, not a
  claim that every potentially U.S.-available requisition is covered. No
  unfiltered production fallback or inference from a requisition-only location
  was added. R50396 similarly had an empty posting location and a UK requisition.
- Campus labels do not establish commute suitability. Mixed locations and
  description/location conflicts remain available to conservative eligibility
  review; no eligibility policy or geography normalization was changed.
- Strict validation intentionally makes a transient detail failure fail the
  source run rather than accept unverified availability. Source errors are
  handled by the existing pipeline.
- Relative listing dates remain unparsed under the existing Workday behavior.

## Offline validation

Focused source/discovery/transition/candidate suites: **473 passed**. Tests cover
dynamic nested facets, nationwide patterns, rejected foreign/unknown labels,
pagination/deduplication, broad nonmatching roles, full descriptions, preserved
foreign primary countries, annotated alternatives, fail-closed errors, unchanged
transition evidence gates, and all three candidate eligibility outcomes.

Full repository suite: **722 passed**. Repository-wide Ruff and `git diff --check`
passed. No commit or push was performed.
