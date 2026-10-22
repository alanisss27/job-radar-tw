# Explicit geography and accurate eligibility rendering

Foreign JobAdder locations previously reached unresolved commute/residence review
because physical geography recognized US states and remote country validation was
source-specific. The dry-run renderer also described unresolved clinical records
as having passed location requirements.

## Changes

- Recognize the full country names India, Australia, Canada, United Kingdom and
  New Zealand in location fields. With a configured US onsite scope, entirely
  foreign physical alternatives and explicit mandatory foreign worksites are
  unsuitable. Bare cities, unrecognized countries, and unresolved/mixed physical
  alternatives remain review-needed. Country abbreviations are not introduced;
  existing US state abbreviations retain their meaning.
- Recognize explicit foreign-only remote applicant residence requirements.
  Required applicant residence takes precedence over a generic US-remote label.
  Explicit contradictory applicant alternatives remain unresolved. Incidental
  headquarters, customer locations, travel and visits do not create exclusions.
  Existing US/state restrictions still apply, including Florida exclusions.
- Render assessed eligibility as confirmed by configured checks, review needed
  (not confirmed), or ineligible, including escaped review/exclusion reasons.
  Clinical discovery relevance remains separate from candidate qualification.
  Records without an assessment retain their previous rendering.

## Boundaries

This is bounded English evidence recognition, not geocoding or a complete country
catalogue. Ambiguous or unsupported evidence can still require review. No matching
thresholds, profiles, preferences, scheduling, ingestion, outbox delivery or
notification limits change. P3 remains review-only across overlapping profiles.

No historical decisions or queued messages are retroactively reclassified by
merging this change. A future authorized evaluation can apply the new assessment;
existing queue eligibility rechecks use the updated rules when executed. No live
scan, production database operation or Telegram delivery is part of validation.

## Validation

Synthetic regressions cover physical/remote foreign restrictions, ambiguous and
mixed alternatives, Florida/US safeguards, incidental international context,
escaped rendering, durable alert exclusion, and overlapping P3 profiles. Tests
use mocked HTTP, isolated SQLite storage and an external-network guard.

Full-suite results are compared against main under the same offline environment;
these local results do not substitute for Ubuntu CI or production validation.

Python 3.12.14 with locked dependencies, external DNS/connections blocked,
credential-free processes and no `.env`:

- 532 focused tests passed, including 52 new geography/rendering regressions and
  all 172 existing P1/P2/P3 cases.
- Main `75727c05`: 1,440 passed, 146 failed, zero errors.
- Feature: 1,492 passed, 146 failed, zero errors. All 146 failing node IDs match
  main exactly; no feature-only failures. These offline counts differ from the
  previously recorded 152-failure GitHub environment; CI was not invoked.
- Ruff lint, repository-wide formatting check and Git diff checks passed.

The existing scheduled/local parity fixture for `Remote Canada` now expects
`unsuitable / remote_residence_incompatible`; its parity assertion is retained.
Temporary harness issues involving Windows startup, loopback sockets, isolated
paths and guarded validation subprocesses were resolved before final comparison.
