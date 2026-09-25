# Wave 1 employer onboarding - Sumitomo + Beacon - 2026-09-24

Only two registry entries were added. Both use existing adapters and only the
`clinical-discovery` profile from b6e90a9. No adapter, discovery, scoring,
threshold, candidate-eligibility, notification, or Career Ops logic changed.
No existing employer entry was changed.

## Production source contracts

| Slug | Adapter | Canonical identifiers |
| --- | --- | --- |
| `sumitomo-pharma-america` | Workday | Host `sumitomopharma.wd5.myworkdayjobs.com`, tenant `sumitomopharma`, board `SMPA` |
| `beacon-biosignals` | Greenhouse | Board token `beaconbiosignals` |

### Sumitomo Pharma America

- Official board: <https://sumitomopharma.wd5.myworkdayjobs.com/SMPA>
- Search: `POST https://sumitomopharma.wd5.myworkdayjobs.com/wday/cxs/sumitomopharma/SMPA/jobs`
- Detail: `GET https://sumitomopharma.wd5.myworkdayjobs.com/wday/cxs/sumitomopharma/SMPA{externalPath}`
- Public URL: `https://sumitomopharma.wd5.myworkdayjobs.com/en-US/SMPA{externalPath}`
- Page size 20 enumerates the complete SMPA board without title restrictions.
- Existing Workday identity is the external path, including requisition suffix;
  listing fields retain requisition IDs and full location/detail evidence.

### Beacon Biosignals

- Official board: <https://job-boards.greenhouse.io/beaconbiosignals>
- Listing with full descriptions:
  `GET https://boards-api.greenhouse.io/v1/boards/beaconbiosignals/jobs?content=true`
- Greenhouse job ID is the stable identity; `absolute_url` is retained unchanged.
- The live Associate posting is `Remote`, while its description restricts it to
  Pacific or Mountain time zones in the U.S. Both facts are retained; `Remote`
  is not rewritten to unrestricted U.S. remote.

## Mayo Clinic - approved employer candidate; onboarding deferred

- Official discovery service investigated: <https://careers.mayoclinic.org/careers>
- Eightfold PCS-X endpoints use domain `mc.org`, location `United States`, and
  page size 10.
- Investigation measured approximately 1,391 U.S. postings: approximately 140
  listing requests plus approximately 1,391 detail requests, or approximately
  1,531 HTTP requests per broad scan.
- The existing Eightfold adapter enriches every listing with a detail request
  before discovery filtering. Mayo's broad inventory contains unrelated
  physician, nursing, patient-care, and hospital-service roles.
- A narrow single query or title allowlist creates meaningful recall risk for the
  expanded clinical, scientific, data, startup, regulatory, and project families.
  Efficient ingestion requires a separate design; Mayo is not enabled in the
  production registry by this Wave 1 change.
- The investigation observed a Jacksonville Clinical Data Associate and verified
  Florida location parsing, but this is investigation evidence only. No Mayo
  source was enabled, monitored, backfilled, or connected to notifications.

## Bounded validation evidence

Validation invoked the source adapters directly through bounded transports. It
did not invoke `run_pipeline`, initialize storage, claim a run, enqueue
notifications, perform a backfill, or contact Telegram.

| Source | Inventory/sample | Result |
| --- | --- | --- |
| Sumitomo | 28 jobs; full board, two listing pages and 28 detail requests | 28 parsed, 28 unique IDs, no adapter warnings; shortest description 6,546 characters |
| Beacon | 25 jobs; full board in one content-enabled listing | 25 parsed, 25 unique IDs, no adapter warnings; shortest description 3,874 characters |

Observed relevant titles included Sumitomo Associate Clinical Project Manager
and Beacon Clinical Study Operations Associate. Full descriptions, stable IDs,
canonical URLs, and remote/location evidence were preserved.

## Regression coverage and rollout

`tests/test_wave1_sources.py` covers the two production source configurations,
existing adapter routing, clinical-discovery assignment, pagination and
deduplication, full text, source identity, public URLs, IDs, US Remote, remote
restrictions, and unchanged repeat-observation planning.

The enabled-source count becomes 80 (126 total registry entries). Mayo remains
an approved employer candidate with onboarding deferred. Existing ICON, Fortrea,
and McKesson/SCRI configurations are unchanged. Bausch + Lomb, Pennington, KPS
Life, and Pinnacle were not added. No historical alert backfill was requested or
run.
