"""Private configuration parity with synthetic facts; no ingestion or secrets."""

from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from job_monitor.config import Settings, load_preferences
from job_monitor.eligibility import assess_candidate
from job_monitor.models import RawJob


POLICY = {
    "preferences": {
        "location_terms": [],
        "include_remote": True,
        "exclude_citizenship_required": True,
        "exclude_clearance_required": True,
        "excluded_seniorities": [],
        "candidate_eligibility": {
            "residence_state": "FL",
            "held_professional_licenses": [],
            "commuting_policy": {
                "onsite_states": ["FL"],
                "limited_attendance_states": [],
            },
        },
    },
}


@pytest.fixture
def parity(tmp_path, monkeypatch):
    local_path = tmp_path / "preferences.local.yml"
    scheduled_path = tmp_path / "runner-preferences"
    text = yaml.safe_dump(POLICY)
    local_path.write_text(text, encoding="utf-8")
    # The workflow transports a complete file, not an overlay/merge.
    scheduled_path.write_text(text, encoding="utf-8")
    local = load_preferences(local_path)
    monkeypatch.setenv("PREFERENCES_CONFIG", str(scheduled_path))
    scheduled = load_preferences(Settings(_env_file=None).preferences_config)
    assert local == scheduled
    return local, scheduled


def test_workflow_provisions_private_configuration_before_any_consumers():
    workflow = yaml.safe_load(Path(".github/workflows/monitor.yml").read_text(encoding="utf-8"))
    steps = workflow["jobs"]["monitor"]["steps"]
    index = next(
        i for i, step in enumerate(steps) if step.get("name") == "Provision private preferences"
    )
    step = steps[index]
    assert step["env"]["PREFERENCES_YAML"] == "${{ secrets.PREFERENCES_YAML }}"
    assert step["id"] == "preferences"
    assert "github.event_name == 'schedule'" in step["if"]
    assert "inputs.clinical_title_backfill_report != true" in step["if"]
    script = step["run"]
    assert "umask 077" in script
    assert 'mktemp "${RUNNER_TEMP}/job-radar-preferences.XXXXXX"' in script
    assert 'printf \'%s\\n\' "$PREFERENCES_YAML" > "$preferences_file"' in script
    assert '"PREFERENCES_CONFIG=$preferences_file" >> "$GITHUB_ENV"' in script
    assert "load_preferences(Path(sys.argv[1]))" in script
    assert script.index("load_preferences(") < script.index("PREFERENCES_CONFIG=")
    assert "Invalid PREFERENCES_YAML" in script
    missing_secret_branch = script.split("\nfi\n", 1)[0]
    assert "exit 1" in missing_secret_branch
    assert "exit 0" not in missing_secret_branch
    assert "ready=true" not in missing_secret_branch
    assert "PREFERENCES_CONFIG=" not in missing_secret_branch
    assert "::error::PREFERENCES_YAML is required" in missing_secret_branch
    assert script.index("load_preferences(") < script.index("ready=true")
    assert "'ready=true' >> \"$GITHUB_OUTPUT\"" in script
    assert "set -x" not in script
    for i, consumer in enumerate(steps):
        if "uv run monitor " in consumer.get("run", ""):
            assert i > index
        assert "PREFERENCES_CONFIG" not in consumer.get("env", {})
        if consumer.get("name") in {
            "Run scheduled monitor",
            "Run manual monitor",
            "Run one-time clinical title backfill",
            "Notify Telegram on workflow failure",
        }:
            assert "steps.preferences.outputs.ready == 'true'" in consumer["if"]
    assert all("upload-artifact" not in step.get("uses", "") for step in steps)


def test_public_fallback_stays_unknown_and_unchanged(monkeypatch):
    monkeypatch.delenv("PREFERENCES_CONFIG", raising=False)
    preferences = load_preferences(Settings(_env_file=None).preferences_config)
    assert preferences.candidate_eligibility is not None
    assert preferences.candidate_eligibility.residence_state is None
    assert preferences.candidate_eligibility.commuting_policy.onsite_states is None
    assert preferences.location_terms == []


@pytest.mark.parametrize(
    "payload,valid",
    [
        (POLICY, True),
        (
            {
                "preferences": {
                    "candidate_eligibility": {
                        "residence_state": "FL",
                        "commuting_policy": {"onsite_states": []},
                    }
                }
            },
            True,
        ),
        ({"preferences": {}}, False),
        ({"preferences": {"candidate_eligibility": None}}, False),
        ({"preferences": {"candidate_eligibility": {"residence_state": "FL"}}}, False),
        (
            {
                "preferences": {
                    "candidate_eligibility": {"commuting_policy": {"onsite_states": ["FL"]}}
                }
            },
            False,
        ),
        (
            {
                "preferences": {
                    "candidate_eligibility": {"residence_state": "SYNTHETIC_PRIVATE_VALUE"}
                }
            },
            False,
        ),
        ("preferences: [SYNTHETIC_PRIVATE_VALUE", False),
    ],
)
def test_workflow_private_validation_does_not_log_input(tmp_path, payload, valid):
    steps = yaml.safe_load(Path(".github/workflows/monitor.yml").read_text(encoding="utf-8"))[
        "jobs"
    ]["monitor"]["steps"]
    script = next(s["run"] for s in steps if s.get("name") == "Provision private preferences")
    validator = script.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    path = tmp_path / "private-preferences"
    path.write_text(
        payload if isinstance(payload, str) else yaml.safe_dump(payload), encoding="utf-8"
    )
    result = subprocess.run(
        [sys.executable, "-B", "-", str(path)],
        input=validator,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert (result.returncode == 0) is valid
    assert result.stdout == ""
    assert "SYNTHETIC_PRIVATE_VALUE" not in result.stderr
    if not valid:
        assert (
            result.stderr.strip()
            == "Invalid PREFERENCES_YAML; correct the private preferences secret."
        )


@pytest.mark.parametrize(
    "location,body,status,reason",
    [
        ("Remote United States", "This role is fully remote.", "eligible", None),
        ("Remote United States", "Candidates must reside in Florida or Georgia.", "eligible", None),
        (
            "Remote United States",
            "Candidates must reside in California.",
            "unsuitable",
            "remote_residence_incompatible",
        ),
        (
            "Remote United States",
            "Remote work is available except in Florida.",
            "unsuitable",
            "remote_residence_incompatible",
        ),
        ("Remote", "This role is fully remote.", "review_needed", "remote_residence_unknown"),
        (
            "Remote Canada",
            "This role is fully remote.",
            "review_needed",
            "remote_residence_unknown",
        ),
        ("Orlando, FL", "This role is onsite.", "review_needed", "commute_not_verified"),
        (
            "Orlando, FL",
            "This role is hybrid two days per week onsite.",
            "review_needed",
            "limited_attendance_commute_review",
        ),
        ("Miami, FL", "This role is onsite.", "review_needed", "commute_not_verified"),
        ("Atlanta, GA", "This role is onsite.", "unsuitable", "onsite_geography_incompatible"),
        (
            "Atlanta, GA",
            "This role is hybrid one day per week onsite.",
            "unsuitable",
            "onsite_geography_incompatible",
        ),
        (
            "Remote United States",
            "This role is fully remote. Travel up to 25% is required.",
            "eligible",
            None,
        ),
        (
            "Remote United States",
            "This role is fully remote. You must work at the office in Boston, MA monthly.",
            "unsuitable",
            "onsite_geography_incompatible",
        ),
        (
            "Remote United States",
            "This role is fully remote. An active RN license is required.",
            "unsuitable",
            "required_license_not_held",
        ),
    ],
)
def test_local_and_scheduled_eligibility_decisions_are_identical(
    parity, location, body, status, reason
):
    job = RawJob(
        source_company="synthetic",
        title="Operations Coordinator",
        location_raw=location,
        description_raw=body,
        url="https://example.test/parity",
    )
    local, scheduled = [assess_candidate(job, preferences) for preferences in parity]
    assert local == scheduled
    assert scheduled.status == status
    if reason:
        assert any(r.startswith(reason) for r in scheduled.hard_reasons + scheduled.review_reasons)
