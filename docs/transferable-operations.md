The `transferable-operations` profile is supplemental discovery over inventory
already fetched for configured companies. Loading this profile enables it for
each company alongside that company's assigned profiles; it adds no sources or
queries. Removing it from the loaded profile configuration disables this
supplemental evaluation. Normal unchanged-job skipping still applies; this
change does not run a historical backfill.
Bounded titles with missing duty/transferability evidence can use the existing
candidate-hydration path on adapters that already support it; no new detail
fetching mechanism or source scope is introduced.

Bounded titles cover associate/coordinator/specialist roles in product, business,
project/program operations, implementation, onboarding, process, and workflow
coordination, plus junior operations analysts. Senior/lead/director titles and
unbounded manager titles are excluded. Associate Product Manager additionally
requires an affirmative JD statement accepting operations/project coordination
experience; title or generic product keywords are insufficient.

At least two distinct affirmative JD duty categories are required: project
tracking, cross-functional coordination, process/SOP documentation, data quality
or analysis, reporting, vendor coordination, and workflow improvement. Multiple
keywords in one category count only once. Negated duties, experience/knowledge
requirements, preferred qualifications, and company boilerplate do not supply
duty evidence. Required specialist credentials, software/product ownership,
engineering, Salesforce administration, and sales quotas are exclusions.

Discovery passes are **review-only**. The current candidate model cannot verify
general operations/product experience or every JD qualification, so this profile
adds `operations_qualifications_unverified`, retains missing location/credential
evidence as review-needed, and never creates an immediate alert. Existing hard
eligibility exclusions (Florida/residence, licenses, citizenship/clearance, visa)
and candidate level/reach gates remain in force. Discovery relevance is not a
claim that a candidate meets qualifications; degrees/experience must be reviewed.
There is no new mechanism to approve or bypass those checks.

This prevents the new profile from duplicating alerts from existing profiles.
The daily summary also suppresses a supplemental review entry if another
displayed profile covers the same company/job. All decisions remain persisted
and included in Phase 1 diagnostics. `profile_discovery_outcomes` uses bounded
`profile__outcome` keys so passes and exclusions can be attributed to this profile.

`tests/fixtures/transferable_operations.json` is explicitly synthetic, based only
on the task's documented requirements. No verified Vetcove JD was found locally;
the positive fixture is not evidence about Vetcove or a real posting.

Limitations: deterministic wording/title coverage is deliberately bounded; some
valid paraphrases or title variants may remain undiscovered. Existing ATS source
scopes can exclude a job before matching. Existing qualification/citizenship
parsing retains its current coverage. No claims of complete credential detection
or candidate qualification are made, and no live verification was performed.
