"""Bounded, review-first operations discovery; no inferred candidate qualifications."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .eligibility import clauses

PROFILE = "transferable-operations"

_TITLE = re.compile(
    r"(?:associate product manager|"
    r"(?:(?:entry level|junior|associate) )?"
    r"(?:(?:product|business|project|program) operations (?:associate|coordinator|specialist|analyst)|"
    r"(?:project|program|implementation|client implementation|customer implementation|onboarding|workflow|process) "
    r"(?:operations )?(?:associate|coordinator|specialist)|"
    r"operations (?:associate|coordinator|specialist)|"
    r"(?:associate|coordinator|specialist) (?:product|business|project|program) operations))$",
    re.I,
)
_SENIOR = re.compile(
    r"\b(?:senior|sr|lead|staff|principal|director|head|vp|chief|executive)\b", re.I
)
_ACTION = re.compile(
    r"^(?:(?:you|the successful candidate) (?:will|must) |"
    r"(?:responsibilities|duties)(?: include)?[: ]+|responsible for )?"
    r"(?:track|maintain|monitor|coordinate|collaborate|document|write|create|develop|"
    r"validate|clean|analy[sz]e|prepare|produce|report|manage|improve|streamline|support|"
    r"facilitate|update|ensure|configure|implement|build|design)\b",
    re.I,
)
_DUTIES = {
    "project_tracking": r"\b(?:track|maintain|monitor|update)\w*\b.{0,60}\b(?:project (?:plans?|status|timelines?|milestones?)|action items?|deliverables?)\b",
    "cross_functional_coordination": r"\b(?:coordinate|collaborate|facilitate)\w*\b.{0,80}\b(?:cross[ -]functional|stakeholders?|teams?)\b",
    "process_documentation": r"\b(?:document|write|create|develop|maintain|update)\w*\b.{0,60}\b(?:SOPs?|standard operating procedures?|process documentation|processes|procedures?)\b",
    "data_quality_analysis": r"\b(?:validate|clean|analy[sz]e|ensure|maintain)\w*\b.{0,60}\b(?:data|data quality|datasets?)\b",
    "reporting": r"\b(?:prepare|produce|create|maintain|report|update)\w*\b.{0,60}\b(?:reports?|dashboards?|status updates?|project status)\b",
    "vendor_coordination": r"\b(?:coordinate|manage|collaborate)\w*\b.{0,60}\b(?:vendors?|suppliers?|external partners?)\b",
    "workflow_improvement": r"\b(?:improve|streamline|develop)\w*\b.{0,60}\b(?:workflows?|process improvements?|operational processes)\b",
}
_SPECIALIZED = re.compile(
    r"\b(?:salesforce (?:administr\w*|configuration|implementation)|salesforce certified|software engineering|engineering degree|"
    r"computer science degree|degree in (?:engineering|computer science)|SAP|Epic|Workday configuration|HL7|FHIR|"
    r"API integrations?|SQL development|PMP|CPA|clinical licen[cs]e|professional licen[cs]e|"
    r"certification|certified|sales quotas?|revenue quotas?|quota[ -]carrying|"
    r"software develop\w*|programming|coding|product ownership|software product management|product management experience)\b",
    re.I,
)
_OWNERSHIP = re.compile(
    r"\b(?:own|define|drive|lead|manage)\w*\b.{0,60}\b(?:product roadmap|product strategy|"
    r"product backlog|software products?|software architecture|engineering (?:team|delivery)|software development)\b",
    re.I,
)


@dataclass(frozen=True)
class OperationsEvidence:
    duties: tuple[str, ...]
    rejection: str | None


def operations_evidence(title: str, description: str) -> OperationsEvidence:
    title = re.sub(
        r"\s*(?:\((?:remote|us remote|united states|florida)\)|[-–—] (?:remote|us remote))\s*$",
        "",
        title,
        flags=re.I,
    )
    title = re.sub(r"\s+(?:I|1)$", "", title)
    normalized = re.sub(r"[^a-z0-9 ]+", " ", title.lower())
    normalized = " ".join(normalized.split())
    if _SENIOR.search(normalized):
        return OperationsEvidence((), "operations_seniority")
    if not _TITLE.fullmatch(normalized):
        return OperationsEvidence((), "operations_title_scope")
    duties: set[str] = set()
    accepts_transfer = False
    required_section = False
    for text in clauses(description):
        text = text.strip(" -*•\t")
        if re.fullmatch(
            r"(?:minimum|required) (?:qualifications|requirements)[: ]*|requirements[: ]*",
            text,
            re.I,
        ):
            required_section = True
            continue
        if re.fullmatch(
            r"(?:preferred qualifications|responsibilities|duties|benefits)[: ]*", text, re.I
        ):
            required_section = False
            continue
        negated = bool(re.search(r"\b(?:not|no|without|never|optional|preferred)\b", text, re.I))
        # Explicit requirements and assigned specialist duties are exclusions.
        if not negated and (
            _OWNERSHIP.search(text)
            or _SPECIALIZED.search(text)
            and (
                required_section
                or _ACTION.match(text)
                or re.search(r"\b(?:must|required|mandatory|minimum|need|requires)\b", text, re.I)
            )
        ):
            return OperationsEvidence(tuple(sorted(duties)), "operations_specialized_requirement")
        if (
            not negated
            and re.search(
                r"\b(?:accept\w*|welcom\w*|qualif\w*|consider\w*|transferable|equivalent)\b",
                text,
                re.I,
            )
            and re.search(r"\bexperience\b", text, re.I)
            and re.search(
                r"\b(?:operations|project coordination|project management|program coordination)\b",
                text,
                re.I,
            )
        ):
            accepts_transfer = True
        if (
            not negated
            and _ACTION.match(text)
            and not re.search(
                r"\b(?:experience|familiarity|knowledge|ability to|company|we offer)\b",
                text,
                re.I,
            )
        ):
            duties.update(
                code for code, pattern in _DUTIES.items() if re.search(pattern, text, re.I)
            )
    if normalized == "associate product manager" and not accepts_transfer:
        return OperationsEvidence(tuple(sorted(duties)), "operations_transferability_unverified")
    return OperationsEvidence(
        tuple(sorted(duties)),
        None if len(duties) >= 2 else "operations_duties_insufficient",
    )
