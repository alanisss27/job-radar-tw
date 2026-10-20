# Phase 1 / Phase 2 deployment preparation

Prepared locally; nothing has been committed, pushed, or deployed. No live
ingestion, notification delivery, or production database writes were performed.

## Production configuration gate

The scheduled, manual-monitor, and matching-backfill workflow paths require
`PREFERENCES_YAML`. It must load through `load_preferences` with candidate
eligibility enabled, a known residence state, and an explicitly configured onsite
state scope. Empty onsite scope is an explicit ban, not unknown. Missing,
malformed, disabled, or unknown eligibility fails before matching. The workflow
exports `PREFERENCES_CONFIG` and a readiness output only after successful
validation. Production matching and the workflow Telegram failure alert require
that output; configuration failure therefore sends no Telegram message.

The secret is written to an unpredictable runner-temp file with private
permissions. Validation errors do not print its contents. The read-only clinical
backfill report path can skip provisioning, and local development retains the
existing public-default and explicit-path loading behavior.

The actual ignored private preferences file passed the existing loader and the
workflow's validator without displaying its contents. A GitHub secret's presence
or value has not been remotely verified. Local visa sponsorship is false; the
workflow references `vars.VISA_SPONSORSHIP_REQUIRED` with a false fallback, which
does not prove the repository variable's actual value.

## Manual GitHub setup

1. Open the repository's **Settings → Secrets and variables → Actions → Secrets**:
   <https://github.com/alanisss27/job-radar-tw/settings/secrets/actions>.
2. Choose **New repository secret** and name it exactly `PREFERENCES_YAML`.
3. In a trusted local editor, copy the full YAML from the ignored
   `config/preferences.local.yml` into the secret value. Supply YAML content,
   not a path. The descriptive `search_area` field may be omitted; it does not
   affect eligibility. Avoid addresses or unnecessary personal details. Do not
   put this content in a commit, issue, PR, or ordinary Actions variable.
4. Confirm the intended policy privately: Florida residence, Florida-only onsite
   scope, no additional limited-attendance states, no unverified commute
   approvals, remote work enabled, no held professional licenses unless verified,
   and unchanged citizenship/clearance/seniority settings. Save the secret.
5. On **Variables**, inspect `VISA_SPONSORSHIP_REQUIRED`:
   <https://github.com/alanisss27/job-radar-tw/settings/variables/actions>.
   Confirm its value is `false` to match current local configuration. If it is
   absent, the workflow currently defaults to false; explicitly setting it to
   `false` makes the intended state visible. If a different value is present,
   resolve the policy discrepancy before deployment rather than assuming parity.

No workflow dispatch is needed for this preparation. The production guard takes
effect only after the reviewed workflow is approved and published to the branch
used by scheduled runs. Repository settings have not been changed here.

## Pending-file review and CI

The scoped files below contain only Phase 1 diagnostics, Phase 2 review-only
discovery, configuration transport, synthetic tests, and related documentation.
No credential patterns or copied private locality were found in the reviewed
pending files. Personal preferences, `.env`, resume/candidate files, raw snapshots,
temporary replay scripts, databases, and build artifacts remain outside the
commit scope; ignored personal files are not tracked. Automated pattern checks
supplement the explicit diff review and are not a universal secret detector.

Ruff formatting still fails for `matching.py`, `notifier.py`, `pipeline.py`, and
`storage.py` at both HEAD and in the proposed tree. Newly added feature blocks
were cleaned up without broadly formatting those baseline files. New modules and
tests are formatted; `tests/test_pipeline.py` passes after Git's configured LF
normalization. The known 73 legacy failing test nodes were previously confirmed
baseline-identical and were not rerun as a broad suite during preparation.

The Test workflow runs `ruff format --check .` before pytest, so formatting debt
prevents green CI now; legacy failures remain a later CI blocker. Whether red CI
blocks a merge depends on branch protection, which has not been verified remotely.
The monitor workflow has no dependency on the Test workflow, so red CI alone does
not prevent scheduled execution after code reaches the scheduled branch. Baseline
CI cleanup needs a separately scoped change/approval; it was not mixed into this
deployment work.

## Scoped commit/push plan — approval required, not executed

First provision/verify the private configuration and visa setting, review the
pending diff, and resolve the CI policy/blockers. Then use a feature branch rather
than pushing directly to the scheduled production branch. The following PowerShell
commands are a plan only:

```powershell
git switch -c phase1-phase2-readiness
$deploymentPaths = @(
  '.github/workflows/monitor.yml',
  'config/profiles.yml',
  'src/job_monitor/diagnostics.py',
  'src/job_monitor/transferable_operations.py',
  'src/job_monitor/matching.py',
  'src/job_monitor/notifier.py',
  'src/job_monitor/pipeline.py',
  'src/job_monitor/storage.py',
  'tests/test_diagnostics.py',
  'tests/test_transferable_operations.py',
  'tests/test_scheduled_preferences.py',
  'tests/test_pipeline.py',
  'tests/fixtures/transferable_operations.json',
  'docs/run-diagnostics.md',
  'docs/transferable-operations.md',
  'docs/transferable-operations-replay-2026-10-20.md',
  'docs/candidate-eligibility.md',
  'docs/deployment-readiness.md'
)
git add -- $deploymentPaths
git diff --cached --check
git diff --cached --stat
git diff --cached
git commit -m "Add run diagnostics and review-only operations discovery with private workflow configuration"
git push -u origin phase1-phase2-readiness
```

Open a PR for those reviewed files, require the intended checks, and obtain
separate approval before merging/publishing to the scheduled branch. No secrets
or ignored files should be force-added, and no ingestion/backfill dispatch is
part of this plan.
