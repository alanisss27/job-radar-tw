# Clinical discovery scope expansion ? 2026-09-24

This changes Job Radar discovery only. The existing score formula, weights, fit
logic, eligibility checks, notification gates, employer integrations and career
facts are unchanged. Clinical discovery profile version is now 1.2.

## Audit of production baseline bce3c2e

- Explicit existing coverage: clinical project/operations/trial associate titles,
  regulated GMP/GxP/validation project titles, Scientific Project Manager and
  Scientific Program Coordinator.
- Conditional existing coverage: clinical trial assistants, Clinical Study
  Associate/Specialist, clinical project support, bounded clinical research
  coordination and trial managers, plus transferable life-science project roles.
- Startup terms were description signals, not a full title family. Scientific
  Project Coordinator had a more demanding multi-duty path; the new domain-qualified
  route intentionally increases recall while preserving that existing path.
- Clinical Study Associate and Scientific Program Coordinator are not duplicated.
  Research/scientific project titles shared between the requested families occur once.

## Exact new canonical title terms

Case is ignored. Start-Up, Start Up and StartUp normalize to startup, including
Unicode hyphens. Numbered levels I?IV / 1?4 and contract/location suffixes are
accepted; this does not admit associate managers or directors.

### Scientific Research Operations

- `scientific research coordinator`
- `scientific leadership research coordinator`
- `scientific operations coordinator`
- `scientific operations associate`
- `research operations coordinator`
- `research operations associate`
- `research project coordinator`
- `research program coordinator`
- `scientific project coordinator`
- `scientific affairs coordinator`
- `research operations specialist`
- `scientific operations specialist`

### Study Development Operations

- `study operations associate`
- `study operations coordinator`
- `study operations specialist`
- `clinical study coordinator`
- `clinical study operations associate`
- `clinical study operations coordinator`
- `clinical study operations specialist`
- `clinical development coordinator`
- `clinical development associate`
- `clinical development operations associate`
- `clinical development operations coordinator`
- `development operations coordinator`
- `development operations associate`

### Study Startup Activation

- `study startup specialist`
- `study startup associate`
- `study startup coordinator`
- `site activation specialist`
- `site activation associate`
- `site activation coordinator`
- `clinical startup specialist`
- `startup specialist`

### Study Regulatory

- `regulatory coordinator - clinical trials`
- `clinical regulatory coordinator`
- `clinical regulatory associate`
- `study regulatory coordinator`
- `study regulatory specialist`
- `regulatory startup specialist`

### Clinical Data Coordination

- `clinical data coordinator`
- `clinical data associate`
- `clinical data specialist`
- `clinical data management associate`
- `clinical data management coordinator`
- `data coordinator - clinical trials`
- `clinical trial data coordinator`

### Cro Proposals

- `proposals development associate`
- `proposal development associate`
- `proposal associate`
- `proposal coordinator`
- `clinical proposal associate`
- `clinical proposal coordinator`
- `proposal development coordinator`

### Life Science Project Program

- `r&d project coordinator`
- `r&d program coordinator`
- `drug development project coordinator`
- `drug development program coordinator`
- `development project coordinator`
- `development program coordinator`
- `technical project coordinator - life sciences`
- `project associate - clinical development`
- `project associate - drug development`
- `project coordinator - clinical development`
- `program associate - clinical development`

## Boundaries and noise expectations

| Family | Context and expected noise |
| --- | --- |
| Scientific/research operations | Life-science/clinical/pharma/biotech context; some non-CPM research support may surface. Generic Research Coordinator is not added. |
| Study/development operations | Domain context; Clinical Study Coordinator also needs study/project workflow evidence to avoid patient-scheduling-only roles. |
| Startup/activation | Domain context; no prior startup-experience requirement is imposed by this discovery route. Senior management and specialist IRT/RTSM requirements are not rescued. |
| Study regulatory | Domain plus IRB/IEC, essential documents, startup or related study workflow; CMC, labeling and strategy-focused work is not rescued. |
| Clinical data coordination | Secondary pathway with domain context; data scientists, generic analysts and database engineering are not added. |
| CRO proposals | Domain context; unrelated sales, government contracting, construction, marketing and IT work is blocked unless explicitly tied to clinical-development services. |
| Life-science project/program | Domain context; no broad coordinator/associate wildcard. Unrelated project work is not rescued. |

These are additive discovery routes, not global exclusions. Existing search paths
remain intact. The implementation never checks the candidate's previous startup
experience, or changes how Career Ops evaluates candidate fit. Ordinary preferred
experience and office-software skills do not block the added route.

More relevant discovery results and eligibility-review entries are expected, with
some peripheral research, clinical-data and proposals roles. No percentage or
production count is claimed without a representative production replay. Additional
individual alerts remain subject to the unchanged strong-tier, score, eligibility,
newness, source-age and per-run gates.

## Rollout limitations

The pipeline normally evaluates new/changed postings, not unchanged records.
Changing the profile version does not automatically re-evaluate existing jobs.
No backfill or monitor run is part of this change. Employers outside existing
coverage (including CRC/Teamtailor) remain outside coverage. Upstream source-side
search filters and incomplete ATS descriptions can still limit discovery; source
configuration and parsers are intentionally unchanged.

## Validation

- Full Python suite: 1,039 passed (including 112 scope tests).
- Strict configuration validation: passed; 124 employers, 78 enabled, four profiles.
- Ruff lint and formatting of the new module/tests and configuration schema: passed.
- Repository-wide formatting check still fails on pre-existing formatting; baseline
  failures were verified without changing unrelated files.
- Every pre-existing profile setting and title term was compared with the baseline:
  unchanged apart from the clinical profile version and the new family dictionary.
- No source, eligibility, pipeline, notification, Career Ops or application-record
  changes; no persisted monitor run or notification backfill was performed.
