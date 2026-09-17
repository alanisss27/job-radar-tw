# Candidate eligibility

This is a discovery filter, not a commuting or routing service. Clinical-discovery
scores, tiers, reasons, and career filters remain independent of candidate facts.
The result JSON records `discovery_eligible` and a structured `candidate_eligibility`
assessment. No database migration is required.

## Status and aggregation

- `eligible`: no hard failure or unresolved eligibility facts. This still requires
  the existing discovery threshold and career filters to become an opportunity.
- `review_needed`: unresolved but not proven unsuitable. Practical commuting is
  not inferred from a state or city name.
- `unsuitable`: a required professional license is not held, remote residence is
  incompatible, or required physical geography violates configured state policy.

Hard failures override all review reasons. Both types of evidence are retained.
Assessment evidence includes work arrangement, attendance frequency and supporting
text, location labels/states and their source. No coordinates, mileage, polygons,
Census data, network geocoding, or driving-time estimates are used.

The existing `eligible` boolean is true only when both discovery and the candidate
assessment pass. With `candidate_eligibility: null`, legacy discovery-only behavior
is retained; this is not appropriate for personalized candidate notifications.

## Private configuration

Use `PREFERENCES_CONFIG=config/preferences.local.yml` in the ignored local `.env`.
The private preferences file is ignored by Git. Do not use the example career
profile as a real candidate's profile. Career targets and compensation are separate.

```yaml
preferences:
  location_terms: []
  include_remote: true
  exclude_citizenship_required: true
  exclude_clearance_required: true
  excluded_seniorities: []
  candidate_eligibility:
    residence_state: null
    held_professional_licenses: []
    commuting_policy:
      search_area: null
      onsite_states: null
      limited_attendance_states: []
```

`search_area` is a descriptive general city/area label only, never a street address.
It does not drive matching. `residence_state` and policy state lists accept US state
names or postal abbreviations. Residence alone does not imply a commuting policy.

`onsite_states` establishes the candidate's broad physical-attendance search
scope. A recognized location outside that explicitly configured scope is unsuitable.
A location inside the scope is normally review-needed, NOT automatically commutable.
Null means the scope has not been configured; geography stays under review. An empty
list explicitly permits no states, apart from limited-attendance extensions.

`limited_attendance_states` optionally adds states retained for review for limited
or uncertain schedules. No extra states are assumed. Known frequent attendance
uses only `onsite_states`; unknown schedules use the expanded scope conservatively.
This permits border-region candidates to configure flexibility without city lists.

Exact full city/state entries in `location_terms` represent manually approved
commuting locations, not substrings. They can pass physical geography only inside
the applicable state policy, with a single unambiguous location and no conflicting
or unresolved eligibility evidence. Do not approve an entire city when only part
of it is acceptable. State-only or city-only entries never approve commuting.

The checked-in public configuration enables assessment with unknown facts. The
private configuration does not automatically propagate to deployment; provision
it privately when deploying. National US-remote opportunities do not require a
commuting policy. No historical matches are automatically reevaluated.

## Attendance and location interpretation

Explicit physical attendance of 1-2 days/week is classified as limited, 3-7 as
frequent. Ranges use the maximum. Monthly/occasional attendance is limited. Daily
or full-time onsite is frequent without inventing a numeric count. Hybrid without
a schedule is unknown. Remote days are not converted to onsite days. Contradictory
frequency statements require review. These are screening categories, not commute
approvals or estimates of willingness to travel.

Per-diem schedules with physical evidence remain location-dependent, including
patient visits and clinic attendance. Supported body-only remote/home-based evidence
is retained. National US-remote work bypasses physical geography; state restrictions
and exclusions must permit the candidate's residence. Unknown remote scope remains
under review. Known mismatches still block when other facts are uncertain.

State normalization accepts city/state labels, full state names, optional US country
suffixes and ZIP suffixes. Multiple locations are evaluated conservatively: all
known alternatives outside the scope block; mixed/unclear alternatives require
review. Explicit mandatory body sites outside the policy block even if the listing
label is local. Missing/unrecognized locations never pass automatically.

## Licenses

The existing required-license vocabulary and detection are retained: occupational
titles, description clauses and supported ATS requirement evidence are examined.
Preferred, optional, negated and incidental staff mentions do not impose a license.
RN/LPN/LVN and existing extended credential aliases are supported. Missing candidate
license information never implies possession. Explicit license-only alternatives
accept any listed held credential; conjunctions require all listed credentials.
Unverified non-license alternatives or unnamed required licenses require review.
License jurisdiction, expiration and compact privileges are not certified.

## Notifications

Immediate alerts require explicitly eligible assessments plus existing discovery,
score, freshness and target-bucket checks. Pending alerts recheck the exact stored
posting version against current candidate facts before claiming and before sending.
Both review-needed and unsuitable records block delivery. The queue retains reasons
and does not let blocked entries consume the delivery cap.

Daily summaries retain the normal eligible section and a separate
`Commute/eligibility review needed` section. `DAILY_SUMMARY_MAX_REVIEWS` defaults to
5 and can be zero. Review entries must pass discovery and have plausible recognized
geography or supported remote scope; their eligibility is explicitly unconfirmed.
Unlocatable records are counted separately and retained in match history, not shown
as confirmed local opportunities. Unsuitable jobs appear in neither opportunity
section. Review counts are separate from eligible-match counts.

## Limits and verification

State screening deliberately retains distant cities within the selected state for
manual review. It does not certify a one-hour commute, distinguish sides of a city,
or infer relocation/travel willingness. Free-text parsers cover explicit common
wording, not every possible job description. Unsupported or contradictory wording
needs human review and possibly a regression fixture. No Census dataset or resolver
is required or planned by this simplified implementation.

Tests use synthetic postings, mocked sources/notifiers and temporary local SQLite
only. A future production reevaluation requires separate authorization.
