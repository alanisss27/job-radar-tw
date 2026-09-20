# Vertex Pharmaceuticals source

Vertex's official careers portal links to the Workday tenant at
`https://vrtx.wd501.myworkdayjobs.com/Vertex_Careers`.

The configured Workday identifiers are:

- tenant: `vrtx`
- career site: `Vertex_Careers`
- listing endpoint:
  `https://vrtx.wd501.myworkdayjobs.com/wday/cxs/vrtx/Vertex_Careers/jobs`
- detail endpoint base:
  `https://vrtx.wd501.myworkdayjobs.com/wday/cxs/vrtx/Vertex_Careers`

The bounded audit on 2026-09-20 reported 273 global postings and 256 postings
through the official U.S. `locationCountry` facet
(`bc33aa3152ec42d4995f4791a106ed09`). Vertex accepts a listing limit of 20;
larger requests returned HTTP 400. Later pages can report `total=0` while
still returning valid postings, so pagination must continue based on returned
postings and the initial positive total.

Detail enrichment is enabled for every returned posting. It provides the full
official description, primary and additional locations, country evidence, and
remote or hybrid wording. Remote status is primarily description-based (for
example, `Remote-Eligible` and `Hybrid-Eligible`); the structured
`remoteType` field was not reliable in the audited responses.

The current inventory is senior-heavy. The audit found clinical monitoring,
clinical study quality, GxP operations, GMP validation, project-management,
and preclinical operations roles, but no current title hits for Clinical Study
Associate, Clinical Trial Associate/Assistant, or Clinical Project Coordinator.
Existing discovery rules therefore remain unchanged and should continue to
screen senior or adjacent roles conservatively.

All 17 current non-U.S.-primary records were inspected at the detail level;
none had a U.S. additional location. Future foreign-primary requisitions with
U.S. additional locations would require a scope re-audit before changing the
facet strategy. The current configuration therefore uses the verified U.S.
country facet and preserves detail location evidence without adding
Vertex-specific matching rules.
