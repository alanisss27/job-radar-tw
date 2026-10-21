"""New bounded clinical support routes, independent of candidate eligibility."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .eligibility import clauses

_ACTION = re.compile(
    r"^(?:(?:you|the successful candidate) will |responsible for |"
    r"(?:responsibilities|duties)(?: include)?[: ]+)?"
    r"(?:support|assist|maintain|review|track|reconcile|file|archive|coordinate|"
    r"document|prepare|update|verify|check|manage|organize|organise|ensure|validate|create)\b",
    re.I,
)
_CONTEXT = re.compile(
    r"\b(?:clinical (?:trials?|stud(?:y|ies)|research|operations)|sponsors?|CRO|"
    r"good clinical practice|ICH[ -]?GCP|GCP|e?TMF|trial master files?|"
    r"study documentation|essential (?:trial|study) documents?)\b",
    re.I,
)
_INCIDENTAL = re.compile(
    r"\b(?:not|no|without|never|preferred|experience|familiarity|knowledge|"
    r"our company|about us|we are|equal opportunity)\b",
    re.I,
)
_UNRELATED = re.compile(
    r"\b(?:manufacturing|laboratory QC|lab QC|laboratory quality|lab quality|"
    r"(?:laboratory|lab) (?:testing|tests?)|bench (?:testing|experiments?)|assays?|batch release|GMP|"
    r"software|engineering|Salesforce|SAP|Epic|HL7|FHIR|API integrations?|"
    r"office administration|executive assistance|payroll)\b",
    re.I,
)
_QUALITY_TITLE = re.compile(
    r"(?:(?:senior|sr\.?|lead|principal|staff) )?"
    r"(?:clinical quality(?: assurance)?|GCP quality(?: assurance)?|"
    r"(?:clinical )?e?TMF(?: (?:document|quality|QC))?|trial master file) (?:support )?(?:coordinator|specialist)"
    r"(?:\s+(?:I{1,3}|IV|[1-4]))?(?:\s*[-,(].*)?",
    re.I,
)
_SENIOR = re.compile(
    r"\b(?:senior|sr|lead|principal|staff|director|head|manager|III|IV|3|4)\b", re.I
)
_QUALITY_WORK = re.compile(
    r"\b(?:documents?|documentation|files?|records?|e?TMF|GCP|"
    r"quality|compliance|audits?|inspections?|CAPA)\b",
    re.I,
)
_CREDENTIAL = re.compile(
    r"\b(?:certification|certified|CCRA|CCRP|RAC|CQA|ASQ|RN|MD|PhD|PharmD|doctorate|"
    r"(?:engineering|computer science|medical|nursing|doctoral) degree|"
    r"degree in (?:engineering|computer science|medicine|nursing)|"
    r"professional licen[cs]e|clinical licen[cs]e|nursing licen[cs]e|"
    r"licen[cs]ed pharmacist)\b",
    re.I,
)


def clinical_work_clauses(description: str) -> list[str]:
    """Affirmative assigned clinical work, not title or employer boilerplate."""
    return [
        text
        for text in clauses(description)
        if _ACTION.match(text.strip(" -*•\t"))
        and _CONTEXT.search(text)
        and not _INCIDENTAL.search(text)
        and not _UNRELATED.search(text)
    ]


def _title_text(title: str) -> str:
    return re.sub(r"[\u2010-\u2015]", "-", " ".join(title.split()))


def clinical_alias_title(title: str) -> bool:
    return bool(
        re.fullmatch(
            r"(?:clinical trials associate|site start[ -]?up specialist)"
            r"(?:\s+(?:I|II|1|2))?(?:\s*[-,(].*)?",
            _title_text(title),
            re.I,
        )
    )


def implementation_alias(title: str) -> str | None:
    """Only selected new variants; existing generic operations titles are untouched."""
    title = re.sub(
        r"\s*(?:\((?:remote|us remote|united states|florida)\)|"
        r"[-–—] (?:remote|us remote))\s*$",
        "",
        _title_text(title),
        flags=re.I,
    )
    match = re.fullmatch(
        r"clinical implementation (coordinator|specialist)(?:\s+(?:I|II|1|2))?",
        title.strip(),
        re.I,
    )
    if match:
        return "implementation " + match[1].lower()
    if re.fullmatch(r"implementation specialist (?:II|2)", title.strip(), re.I):
        return "implementation specialist"
    return None


def quality_support_title(title: str) -> bool:
    return bool(_QUALITY_TITLE.fullmatch(_title_text(title)))


@dataclass(frozen=True)
class QualityEvidence:
    duties: tuple[str, ...]
    rejection: str | None


def quality_support_evidence(title: str, description: str) -> QualityEvidence:
    if not quality_support_title(title):
        return QualityEvidence((), "clinical_quality_title_scope")
    if _SENIOR.search(title):
        return QualityEvidence((), "clinical_quality_seniority")
    if _UNRELATED.search(title):
        return QualityEvidence((), "clinical_quality_unrelated_work")
    required = False
    for text in clauses(description):
        if re.fullmatch(
            r"(?:required|minimum)(?: qualifications| requirements)?[: ]*|requirements[: ]*",
            text,
            re.I,
        ):
            required = True
            continue
        if re.fullmatch(
            r"(?:preferred qualifications|responsibilities|duties|benefits)[: ]*", text, re.I
        ):
            required = False
            continue
        if re.search(r"\b(?:not|no|without|never|preferred)\b", text, re.I):
            continue
        credential_records = bool(
            _ACTION.match(text.strip(" -*•\t"))
            and re.search(
                r"\b(?:certification|licen[cs]e|credential) (?:records?|files?|documentation)\b",
                text,
                re.I,
            )
            and not re.search(r"\b(?:hold|possess|have)\b", text, re.I)
        )
        if (
            not credential_records
            and _CREDENTIAL.search(text)
            and (
                required
                or re.search(r"\b(?:required|must|mandatory|requires|minimum)\b", text, re.I)
            )
        ):
            return QualityEvidence((), "clinical_quality_specialized_credential")
        if (
            _ACTION.match(text.strip(" -*•\t"))
            or re.match(
                r"^(?:perform|conduct|implement|build|design|develop|configure|test)\b", text, re.I
            )
        ) and _UNRELATED.search(text):
            return QualityEvidence((), "clinical_quality_unrelated_work")
    work = [
        text
        for text in clinical_work_clauses(description)
        if _QUALITY_WORK.search(text)
        and not re.search(
            r"\b(?:calendars?|invoices?|billing|financial|expenses?|travel arrangements)\b",
            text,
            re.I,
        )
    ]
    return QualityEvidence(
        ("clinical_quality_document_support",) if work else (),
        None if work else "clinical_quality_context_missing",
    )
