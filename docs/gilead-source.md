# Gilead Sciences source

Gilead's official careers portal is the Workday tenant at
`https://gilead.wd1.myworkdayjobs.com/gileadcareers`.

## Configuration and scope

Job Radar uses the existing `WorkdaySource` with tenant `gilead`, career site
`gileadcareers`, a listing limit of 20, and the official Workday listing and
detail APIs:

- `https://gilead.wd1.myworkdayjobs.com/wday/cxs/gilead/gileadcareers/jobs`
- `https://gilead.wd1.myworkdayjobs.com/wday/cxs/gilead/gileadcareers`

The standard `locationCountry` facet is not usable for this tenant. U.S.
scope is resolved dynamically from the nested
`locationMainGroup -> locations` facets. The configured semantic pattern
accepts explicit `United States - ...` labels and the observed `US Field` and
`US Remote` labels. It does not use a fixed list of location IDs. If the
expected facet structure disappears or no intended U.S. labels resolve, the
source fails closed rather than fetching an unscoped global result.

Detail enrichment is required. Workday details preserve the primary location,
additional locations, the original description, and remote/hybrid wording.
Foreign-primary postings with a U.S. additional location remain mixed-location
records; the primary location is not rewritten as U.S. scope. The existing
location validation and downstream candidate-eligibility rules remain in force.

## Audit snapshot

The bounded audit observed 493 global postings and 398 postings in the union
of the current U.S. location facets. These are an inventory snapshot, not a
permanent count. Approximately 40 U.S. facet values were present at audit
time, so facet values must remain dynamic.

A normal U.S. fetch costs approximately 20 listing POSTs plus up to 398 detail
GETs, or about 418 requests. The shared Workday retry and pacing controller is
used for all of them.

The current inventory is senior-heavy. Representative Clinical Program Manager
and Clinical Trials Manager roles remain below the existing clinical-discovery
threshold. No Gilead-specific title rules or discovery changes were added.

Future changes to the facet shape or location labels require review. The
source must continue to fail closed when U.S. scope cannot be interpreted
conservatively.
