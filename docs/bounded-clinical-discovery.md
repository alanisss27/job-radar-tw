# Bounded clinical discovery: P1 / P2 / P3

These changes distinguish permission to retrieve a JD from permission to discover
a role, assess candidate eligibility, or enqueue an individual alert. They do not
change existing family thresholds, source pagination, private preferences,
scheduling, daily claims, outbox behavior, or notification limits.

## P1: existing contextual titles can reach detail retrieval

The candidate gate now accepts exact configured contextual family titles, their
existing normalized startup spelling/levels/suffixes, and the bounded P2/P3
titles. JD context is not required for this retrieval decision. Known hard
eligibility and career exclusions still block the newly added permission.
The previous candidate routes are preserved.

The pipeline still calls candidate hydration only for City of Hope, Charter,
Oracle, JobAdder and Teamtailor metadata, after the unchanged-job shortcut. It
does not hydrate at inventory retrieval time. All five adapters' existing URL,
identity, checked-detail, fingerprint, pagination and retry contracts remain
unchanged. Tests exercise their real detail implementations with mock transport:
one request for a newly selected listing, none for unrelated listings, none on
the next unchanged scan. Existing adapter tests cover inventory-only retrieval,
pagination and identity/failure checks. Transport retries remain as before.

Phenom's separate teaser/detail design is not newly opted into this gate. Changing
its inventory fingerprint/detail lifecycle requires separate validation; no
implicit all-adapter or inventory-time fetch expansion is introduced here.

## P2: narrow aliases, with assigned JD work

`clinical_support_aliases` adds Clinical Trials Associate and Site Start-Up
Specialist (including I/II variants). They require affirmative clinical/sponsor/
CRO/study-document work, not employer boilerplate or requirements-only keywords.
An explicit guard prevents the generic clinical responsibility fallback from
rescuing a new alias that lacks this evidence. These aliases retain the existing
clinical profile's career and candidate eligibility checks.

Clinical Implementation Coordinator/Specialist (I/II) and Implementation
Specialist II/2 reuse transferable-operations. Only these new variants require
assigned clinical work in addition to the existing two distinct operations-duty
categories. Assigned software/engineering/system-specialist duties are excluded.
They remain qualification-review-only; the clinical fallback cannot turn them
into confirmed matches. Existing generic implementation titles are unchanged.

## P3: quality and study-document support, review first

The clinical profile recognizes bounded Clinical Quality / Clinical Quality
Assurance / GCP Quality (Assurance), TMF/eTMF (Document/Quality/QC/Support), and
Trial Master File Coordinator/Specialist titles. I/II are review candidates;
senior/lead/principal/staff and III/IV variants are explicitly rejected.

Assigned quality/document duties need clinical-trial, sponsor/CRO, GCP,
TMF/eTMF, or study-document context. Manufacturing/GMP, laboratory/bench QC,
software/engineering implementation, unrelated administration, and explicit
specialized credential requirements are discovery exclusions. Such a credential
exclusion is not a claim that a private candidate lacks that credential.
Missing evidence stays unresolved; it is never confirmed eligible.

P3 reuses `clinical-discovery` rather than introducing another overlapping profile.
Its positive decisions always receive `clinical_quality_qualifications_unverified`
and `eligible=false`. This review guard applies to overlapping profiles too.
Existing hard residence/attendance/license/citizenship filters remain effective.
No P3 individual alert may be enabled without a separately approved future change.
Evidence and review/exclusion codes flow into existing per-run diagnostics.

## Validation and rollout limits

The synthetic fixture file contains no real postings or candidate evidence.
Independent suites are `test_clinical_detail_candidates.py` (P1),
`test_clinical_aliases.py` (P2), and `test_clinical_quality_review.py` (P3).
All network is mocked; SQLite storage is in-memory or isolated test storage.

English action/context rules intentionally trade some recall for bounded scope;
this is not a comprehensive credential or grammar parser. Ambiguous evidence
remains unconfirmed. Successful P1 selection can add detail requests for those
titles, bounded by existing changed/new candidate inventory and adapter retries.
Unchanged listings are not automatically re-evaluated just because profile
versions changed. A separately authorized bounded replay/backfill is needed to
assess already stored inventory; no live run or notification is part of this work.
Historical queued decisions are not retroactively reclassified by a matching-only
change; this work does not inspect or modify production outbox state.

Local validation: 463 focused offline tests passed, including 172 new cases.
An isolated comparison of the existing clinical suites against main `5a57a016`
found 142 identical failing node IDs and no feature-only failures: main had
513 passes / 142 failures; the feature snapshot had 515 passes / 142 failures
(two additional config-driven alias cases). No collection errors occurred.
The full suite was not rerun. Ruff lint/format, actionlint 1.7.12 on the unchanged
monitor workflow, and diff checks passed.
