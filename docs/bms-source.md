# Bristol Myers Squibb source

Job Radar uses BMS's official public Eightfold CareerHub / PCS-X search
service at [jobs.bms.com](https://jobs.bms.com/careers). The careers site is
the authoritative source; applications may continue to use BMS's Workday
tenant, but Workday is not the enumeration source.

## Source contract

- Search: `https://jobs.bms.com/api/pcsx/search`
- Detail: `https://jobs.bms.com/api/pcsx/position_details`
- Domain: `bms.com`
- Public posting URL: `https://jobs.bms.com/careers/job/{numeric_position_id}`
- U.S. request filter: `location=United States`
- Effective page size: 10 (the service returns 10 even when a larger `num`
  is requested)
- Pagination: GET requests using the `start` offset; records are deduplicated
  by stable numeric position ID.

The bounded audit measured 641 global postings, 406 postings in the official
United States-filtered inventory, and 14 when filtered for Remote. A normal
U.S. fetch therefore costs about 41 listing GETs plus up to 406 detail GETs,
or approximately 447 requests. Detail enrichment is required because the
listing response does not provide the complete description or all metadata.

Each retained posting is enriched from `position_details`. The adapter keeps
the numeric position ID, BMS requisition ID (for example `R1603280`), full
HTML-derived description, all locations and standardized locations,
`workLocationOption`, `locationFlexibility`, the Workday application URL when
present, and the official BMS public URL. A requisition with U.S. and foreign
locations remains a mixed-location record; the U.S. search makes it
U.S.-available but does not rewrite it as U.S.-primary.

Search failures remain source-fatal because they prevent reliable
enumeration. A detail response that is unavailable or lacks the required
description is excluded with a source warning while other postings are
retained. This follows the existing source warning path and avoids treating
unvalidated detail as complete.

The adapter is generic for the audited PCS-X shape and is configuration-driven
in `config/companies.yml`. No BMS title matching or discovery exception was
added. Current representative BMS inventory is senior-heavy; the existing
clinical-discovery profile continues to leave Senior Clinical Trial
Management Associate, Global Trial Lead, and Clinical Research Associate
examples below its current threshold. The BMS source does not change that
profile or any eligibility/geography policy.
