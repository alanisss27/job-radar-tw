"""Context-qualified discovery titles; no candidate fit or score calculations."""

from __future__ import annotations

import html
import re


def _normalize(value: str) -> str:
    value = html.unescape(value).casefold()
    value = re.sub(r"[\u2010-\u2015]", "-", value)
    value = re.sub(r"\bstart[ -]?up\b", "startup", value)
    return re.sub(r"\s+", " ", value).strip()


_DOMAIN = re.compile(
    r"\b(?:clinical (?:trials?|stud(?:y|ies)|research|development|operations)|"
    r"life[ -]sciences?|pharma(?:ceutical)?s?|biotech(?:nology)?|biopharma(?:ceutical)?|"
    r"drug development|contract research organi[sz]ation|cro|"
    r"biomedical|translational research)\b"
)
_STUDY_WORK = re.compile(
    r"\b(?:irb|iec|e?tmf|trial master files?|essential documents?|site activation|"
    r"study startup|study closeout|study operations|study documentation|"
    r"study timelines?|study metrics|protocol submissions?|regulatory documents?|"
    r"study team|project coordination)\b"
)
_UNRELATED = re.compile(
    r"\b(?:software (?:development|projects?|engineering|proposals?|releases?)|"
    r"information technology|it projects?|construction|marketing|"
    r"advertising|commercial sales|sales proposals?|government contract(?:ing)?|"
    r"database engineer(?:ing)?|data scien(?:ce|tist)|cmc|labeling|labelling|"
    r"submissions strategy|regulatory strategy|manufacturing)\b"
)
_SERVICES = re.compile(
    r"\b(?:clinical[ -](?:development|research|trial) services|"
    r"(?:proposals?|bids?) for (?:clinical trials?|clinical research|drug development)|"
    r"(?:clinical[ -](?:development|research|trial)|drug development)[ -]"
    r"(?:services? )?proposals?)\b"
)
_SPECIALIST_REQUIREMENT = re.compile(
    r"\b(?:irt|rtsm)\b[^.!?;\n]{0,60}\bexperience\b[^.!?;\n]{0,30}\brequired\b|"
    r"\b(?:require[sd]?|must have)\b[^.!?;\n]{0,60}\b(?:irt|rtsm)\b"
    r"[^.!?;\n]{0,30}\bexperience\b",
    re.I,
)
_TITLE_EXCLUSIONS = re.compile(
    r"\b(?:senior|sr\.?|lead|manager|director|head|principal|vp|president|"
    r"irt|rtsm|cmc|manufacturing|software|it|construction|marketing|advertising|"
    r"sales|patient services?|scheduling|administrative)\b"
)


def contextual_title_evidence(
    title: str, description: str, families: dict[str, list[str]]
) -> set[str]:
    """Add bounded title recall, without requiring prior professional experience.

    Numbered associate levels and location/contract suffixes are accepted. A new
    route cannot rescue unrelated titles or generic employer boilerplate. These
    restrictions apply only to this expansion, never to existing discovery paths.
    """
    title = _normalize(title)
    if _TITLE_EXCLUSIONS.search(title):
        return set()
    if _SPECIALIST_REQUIREMENT.search(description):
        return set()
    clauses = [
        _normalize(clause)
        for clause in re.split(r"[.!?;\n]+", html.unescape(description))
        if clause.strip()
    ]
    # Employer industry alone must not rescue an unrelated support job.
    clauses = [
        clause
        for clause in clauses
        if not re.search(r"\b(?:our company|about us|we are|equal opportunity)\b", clause)
    ]
    context = " ".join(clauses)
    relevant_context = " ".join(
        c for c in clauses if not _UNRELATED.search(c) or _SERVICES.search(c)
    )
    if not _DOMAIN.search(title + " " + relevant_context):
        return set()
    if _UNRELATED.search(context) and not _SERVICES.search(context):
        return set()
    hits = set()
    for family, terms in families.items():
        for term in terms:
            pattern = re.escape(_normalize(term)) + r"(?:\s+(?:i{1,3}|iv|[1-4]))?(?:\s*[-,(].*)?"
            if not re.fullmatch(pattern, title):
                continue
            if family == "study_regulatory" and not _STUDY_WORK.search(context):
                continue
            if _normalize(term) == "clinical study coordinator" and not _STUDY_WORK.search(context):
                continue
            hits.add(f"{family}: {term}")
    return hits
