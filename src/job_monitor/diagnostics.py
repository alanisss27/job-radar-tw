"""Bounded, aggregate-only diagnostics; never retain posting or candidate text."""

from __future__ import annotations

import re
from collections import Counter
from datetime import UTC, datetime

from .models import MatchResult


class DecisionDiagnostics:
    def __init__(self) -> None:
        self.started_at = datetime.now(UTC).isoformat()
        self.jobs: set[tuple[str, str]] = set()
        self.evaluated: set[tuple[str, str]] = set()
        self.matched: set[tuple[str, str]] = set()
        self.decisions = 0
        self.outcomes: Counter = Counter()
        self.profile_outcomes: Counter = Counter()
        self.discovery_reasons: Counter = Counter()
        self.eligibility: Counter = Counter()
        self.hard: Counter = Counter()
        self.review: Counter = Counter()
        self.source: Counter = Counter()

    @staticmethod
    def _increment(counter: Counter, code: str) -> None:
        # Only machine-like codes, never evidence following ':' or '(';
        # cap vocabulary as well as code length for forward compatibility.
        match = re.match(r"^([a-z][a-z0-9_-]{0,63})(?=[: (]|$)", code)
        key = match[1] if match else "unknown"
        if key not in counter and len(counter) >= 64:
            key = "other"
        counter[key] += 1

    def observe_job(self, company: str, job_id: str) -> None:
        self.jobs.add((company, job_id))

    def observe(self, company: str, job_id: str, result: MatchResult) -> None:
        identity = (company, job_id)
        self.evaluated.add(identity)
        self.decisions += 1
        discovery = result.discovery_eligible
        if discovery is None:
            discovery = result.eligible
        outcome = (
            "passed"
            if discovery
            else "filtered"
            if result.filtered_reason
            else "below_threshold"
            if result.tier == "below_threshold"
            else "unknown"
        )
        self.outcomes[outcome] += 1
        self._increment(self.profile_outcomes, f"{result.profile}__{outcome}")
        if not discovery:
            self._increment(self.discovery_reasons, result.filtered_reason or outcome)
        assessment = result.candidate_eligibility
        if assessment is None:
            self.eligibility["unknown"] += 1
        else:
            self._increment(self.eligibility, assessment.status)
            for reasons, counts in (
                (assessment.hard_reasons, self.hard),
                (assessment.review_reasons, self.review),
            ):
                codes: Counter = Counter()
                for reason in reasons:
                    self._increment(codes, reason)
                for code in codes:
                    self._increment(counts, code)
        if result.notification_eligible:
            self.matched.add(identity)

    def observe_source_warning(self, warning: dict[str, str]) -> None:
        # These are existing Workday exclusion warnings. Other source warning
        # types are not silently assumed to represent an excluded job.
        known = {
            "location validation failed": "workday_location_validation_failed",
            "detail request failed after retries": "workday_detail_request_failed",
            "detail HTTP failure": "workday_detail_http_failure",
            "detail availability indicates posting is closed or unavailable": "workday_detail_unavailable",
        }
        self._increment(self.source, known.get(warning.get("reason", ""), "unclassified_warning"))

    def snapshot(self, run_id: str, stats: dict, *, complete: bool) -> dict:
        return {
            "schema_version": 1,
            "run_id": run_id,
            "started_at": self.started_at,
            "recorded_at": datetime.now(UTC).isoformat(),
            "complete": complete,
            "inventory": {"new": stats.get("jobs_new"), "changed": stats.get("jobs_changed")},
            "unique_jobs_observed": len(self.jobs),
            "unique_jobs_evaluated": len(self.evaluated),
            "profile_decisions": self.decisions,
            "discovery_outcomes": dict(self.outcomes),
            "profile_discovery_outcomes": dict(self.profile_outcomes),
            "discovery_reasons": dict(self.discovery_reasons),
            "candidate_statuses": dict(self.eligibility),
            "candidate_hard_reasons": dict(self.hard),
            "candidate_review_reasons": dict(self.review),
            "final_matches": stats.get("matches"),
            "unique_jobs_matched": len(self.matched),
            "source_stage": {"total_excluded": None, "observed_warnings": dict(self.source)},
            "count_semantics": "Outcomes/reasons count profile decisions; each reason code once per decision. Reasons overlap. Source warnings are separate observations; total exclusions unknown. Unchanged jobs are normally not evaluated. Null means unknown; missing reason keys mean no observed occurrences, not comprehensive coverage.",
        }


def render_diagnostics(diagnostics: dict | None) -> str:
    if diagnostics is None:
        return "Decision diagnostics: unknown"

    def reasons(key: str) -> str:
        counts = diagnostics.get(key)
        if counts is None:
            return "unknown"
        return (
            ", ".join(
                f"{code}={count}"
                for code, count in sorted(counts.items(), key=lambda x: (-x[1], x[0]))[:5]
            )
            or "none observed"
        )

    source = diagnostics.get("source_stage", {})
    warnings = source.get("observed_warnings")
    source_reasons = (
        ", ".join(f"{code}={count}" for code, count in sorted((warnings or {}).items())[:5])
        or "none observed"
        if warnings is not None
        else "unknown"
    )
    return "\n".join(
        [
            f"Decisions: {diagnostics.get('unique_jobs_evaluated', 'unknown')} unique jobs / {diagnostics.get('profile_decisions', 'unknown')} profile decisions; matches={diagnostics.get('final_matches') if diagnostics.get('final_matches') is not None else 'unknown'}",
            "Discovery: "
            + reasons("discovery_outcomes")
            + "; reasons: "
            + reasons("discovery_reasons"),
            "Eligibility: " + reasons("candidate_statuses"),
            "Hard exclusions: "
            + reasons("candidate_hard_reasons")
            + "; review needed: "
            + reasons("candidate_review_reasons"),
            f"Source exclusion total: unknown; observed warnings={sum(warnings.values()) if warnings is not None else 'unknown'} ({source_reasons})",
            "Coverage: "
            + (
                "completed source processing"
                if diagnostics.get("complete")
                else "partial; counts are observations only"
            ),
            "Reason counts overlap per profile decision; top 5 shown. Unchanged jobs normally not evaluated.",
        ]
    )
