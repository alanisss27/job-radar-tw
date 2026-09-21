from __future__ import annotations

import html
import re

from .config import ProfileConfig, SearchPreferences
from .eligibility import assess_candidate, work_arrangement
from .models import (
    CandidateProfile,
    DegreeLevel,
    JobLevel,
    MatchResult,
    ParsedJob,
    RawJob,
    RemoteType,
    ResumeProfile,
    ReviewFlag,
    Seniority,
    VisaSupport,
)
from .resume import SKILL_ALIASES


def _clinical_review_flags(title: str, description: str) -> list[ReviewFlag]:
    """Conservative posting-only advice; never used by scoring or routing."""
    text = re.sub(
        r"<[^>]+>",
        lambda m: "\n" if re.match(r"</?(?:p|li|ul|ol|div|h[1-6]|br)\b", m[0], re.I) else "",
        description,
    )
    clauses = re.split(r"[\n\r•;]+|(?<!Sr\.)(?<!sr\.)(?<=[.!?])\s+(?=[A-Z])", html.unescape(text))
    experience, ownership = [], []
    preferred_section = False
    for clause in clauses:
        clause = " ".join(clause.split()).strip(" -")
        lower = clause.lower()
        if not clause:
            continue
        if re.fullmatch(
            r"(?:preferred|desired|bonus)(?:(?: qualifications| requirements| skills| experience)(?: and experience| & experience)?)?(?: includes)?[: ]*",
            lower,
        ):
            preferred_section = True
            continue
        if re.fullmatch(
            r"(?:required qualifications|qualifications|requirements|responsibilities|what you.ll do|skills and experience you.ll bring)[: ]*",
            lower,
        ):
            preferred_section = False
            continue
        if preferred_section or re.search(
            r"\b(preferred|desirable|optional|a plus|nice to have|not required|support\w*|assist\w*|participat\w*)\b|\b(report\w* to|work\w* with)\b|\bno\b.*\brequired\b",
            lower,
        ):
            continue
        mandatory = bool(
            re.search(
                r"\b(required|must|minimum|at least)\b|\b\d+\s*(?:[–-]\s*\d+)?\s*\+?\s*years?\b",
                lower,
            )
        )
        direct = mandatory and bool(
            re.search(r"\b(experience|years?)\b", lower)
            and re.search(
                r"\bdirect (?:project|clinical trial|pm|ctm)[ /-]*(?:management|experience)\b"
                r"|\bas (?:a |an )?(?:project/clinical trial|clinical trial|project) manager\b"
                r"|\b(?:project/clinical trial|clinical trial|project) management experience\b"
                r"|\byears?\s*\((?:sr\.?\s*)?ctm\)",
                lower,
            )
        )
        # Require the action to govern project/trial work, not meetings about it.
        # Conservatively skip negation and meeting/discussion clauses instead of
        # inferring their grammatical scope.
        accountable = not re.search(
            r"\b(?:not|never|without|meetings?|discuss\w*|facilitat\w*)\b"
            r"|\b(?:isn|aren|won|don|doesn)['’]t\b",
            lower,
        ) and bool(
            re.search(
                r"\b(?:own|owns|owning|accountable for|responsible for|lead|leads|leading)\s+"
                r"(?:(?:the|assigned)\s+)*(?:project|program|trial|study)\b",
                lower,
            )
        )
        scope = all(
            re.search(pattern, lower)
            for pattern in (r"\bscope\b", r"\btimelines?\b", r"\bbudgets?\b")
        )
        client = bool(
            re.search(r"\bprimary client\b", lower)
            and re.search(r"\b(?:project|program) delivery\b", lower)
        )
        lifecycle = bool(
            re.search(r"\b(project|trial|study)\b", lower)
            and re.search(
                r"\b(?:initiation|start[ -]?up)\b.+\b(?:through|to)\b.+\bclose[ -]?out\b", lower
            )
        )
        evidence = clause if len(clause) <= 240 else clause[:239] + "…"
        if direct and evidence not in experience and len(experience) < 2:
            experience.append(evidence)
        if (
            (direct or accountable and (scope or client or lifecycle))
            and evidence not in ownership
            and len(ownership) < 2
        ):
            ownership.append(evidence)
    flags = []
    associate = re.search(r"\b(associate|coordinator|cta)\b", title, re.I)
    manager = re.search(r"\b(director|manager|head|president)\b", title, re.I)
    if associate and not manager and experience:
        flags.append(ReviewFlag(code="title_body_level_mismatch", evidence=experience[:2]))
    if ownership:
        flags.append(ReviewFlag(code="established_ownership_requirement", evidence=ownership[:2]))
    return flags


# Explicit foreign location markers are rejected, while an unqualified
# ``Remote``/``Home-based`` location remains eligible for broad discovery.
FOREIGN_LOCATION_TERMS = [
    "australia",
    "canada",
    "serbia",
    "singapore",
    "south korea",
    "taiwan",
    "united kingdom",
    "uk",
    "england",
    "spain",
    "poland",
    "hungary",
    "romania",
    "slovakia",
    "europe",
    "emea",
    "apac",
    "asia pacific",
    "latin america",
    "latam",
]
US_LOCATION_TERMS = ["united states", "united states of america", "u.s.", "us"]
VISA_SUPPORT_TERMS = [
    "will sponsor",
    "visa sponsorship is available",
    "sponsorship is available",
    "h-1b sponsorship",
    "h1b sponsorship",
    "tn sponsorship",
    "green card sponsorship",
]
VISA_REJECTION_TERMS = [
    "will not sponsor",
    "does not sponsor",
    "do not sponsor",
    "cannot sponsor",
    "unable to sponsor",
    "not able to sponsor",
    "sponsorship is not available",
    "without sponsorship",
    "now or in the future require sponsorship",
    "now or in future require sponsorship",
    "must be authorized to work in the united states without",
]
GREEN_CARD_ELIGIBLE_TERMS = [
    "green card",
    "green-card",
    "permanent resident",
    "lawful permanent resident",
    "u.s. person",
    "us person",
    "u.s. persons",
    "us persons",
]
CITIZENSHIP_ONLY_TERMS = [
    "citizenship required",
    "u.s. citizenship required",
    "us citizenship required",
    "united states citizenship required",
    "must be a u.s. citizen",
    "must be an u.s. citizen",
    "must be a us citizen",
    "must be an us citizen",
    "must be a united states citizen",
    "u.s. citizen only",
    "us citizen only",
    "united states citizen only",
    "only u.s. citizens",
    "only us citizens",
    "requires u.s. citizenship",
    "requires us citizenship",
]
LEVEL_RANK = {
    JobLevel.UNKNOWN: 0,
    JobLevel.ENTRY: 1,
    JobLevel.MID: 2,
    JobLevel.SENIOR: 3,
    JobLevel.LEAD: 4,
    JobLevel.STAFF: 4,
    JobLevel.PRINCIPAL: 5,
    JobLevel.DIRECTOR_PLUS: 6,
}
SENIORITY_BY_LEVEL = {
    JobLevel.ENTRY: Seniority.ENTRY,
    JobLevel.MID: Seniority.MID,
    JobLevel.SENIOR: Seniority.SENIOR,
    JobLevel.LEAD: Seniority.LEAD,
    JobLevel.STAFF: Seniority.LEAD,
    JobLevel.PRINCIPAL: Seniority.LEAD,
    JobLevel.DIRECTOR_PLUS: Seniority.DIRECTOR,
    JobLevel.UNKNOWN: Seniority.UNKNOWN,
}
LEVEL_BY_SENIORITY = {
    Seniority.ENTRY: JobLevel.ENTRY,
    Seniority.MID: JobLevel.MID,
    Seniority.SENIOR: JobLevel.SENIOR,
    Seniority.LEAD: JobLevel.LEAD,
    Seniority.DIRECTOR: JobLevel.DIRECTOR_PLUS,
    Seniority.UNKNOWN: JobLevel.UNKNOWN,
}


def _contains(text: str, terms: list[str]) -> bool:
    lower = f" {text.lower()} "
    return any(term.lower() in lower for term in terms)


def _discovery_hits(text: str, terms: list[str]) -> set[str]:
    """Match complete phrases without matching inside longer words."""
    return {
        term.strip().lower()
        for term in terms
        if term.strip()
        and re.search(r"(?<!\w)" + re.escape(term.strip()) + r"(?!\w)", text, re.IGNORECASE)
    }


def _requires_citizenship(text: str) -> bool:
    if not _contains(text, ["u.s. citizen", "us citizen", "united states citizen", "citizenship"]):
        return False
    if _contains(text, GREEN_CARD_ELIGIBLE_TERMS):
        return False
    return _contains(text, CITIZENSHIP_ONLY_TERMS)


def _detect_level(title: str) -> JobLevel:
    if _contains(title, ["intern", "entry level", "junior", "associate analyst"]):
        return JobLevel.ENTRY
    if _contains(title, ["director", "vice president", "vp ", "head of"]):
        return JobLevel.DIRECTOR_PLUS
    if _contains(title, ["principal"]):
        return JobLevel.PRINCIPAL
    if _contains(title, ["staff"]):
        return JobLevel.STAFF
    if _contains(title, ["lead", "manager"]):
        return JobLevel.LEAD
    if _contains(title, ["senior", " sr ", "sr."]):
        return JobLevel.SENIOR
    if _contains(title, ["analyst", "engineer", "scientist", "developer"]):
        return JobLevel.MID
    return JobLevel.UNKNOWN


def _extract_required_years(text: str) -> int | None:
    matches: list[int] = []
    optional_markers = ["preferred", "nice to have", "or equivalent", "a plus"]
    for sentence in re.split(r"(?<=[.!?;])\s+", text):
        lower = sentence.lower()
        if any(marker in lower for marker in optional_markers):
            continue
        for match in re.finditer(
            r"\b(\d{1,2})(?:\s*-\s*\d{1,2})?\s*\+?\s*years?\b", sentence, re.I
        ):
            value = int(match.group(1))
            if value > 20:
                continue
            before = sentence[max(0, match.start() - 40) : match.start()]
            after = sentence[match.end() : match.end() + 40]
            if re.search(r"\bexperience\b", after, re.I) or re.search(
                r"\b(minimum|at least|with)\b", before, re.I
            ):
                matches.append(value)
    return min(matches) if matches else None


def _extract_degree_required(text: str) -> DegreeLevel:
    sentences = re.split(r"(?<=[.!?])\s+", text)
    for sentence in sentences:
        lower = sentence.lower()
        if not any(term in lower for term in ["required", "must have"]):
            continue
        if any(term in lower for term in ["preferred", "nice to have", "or equivalent", "a plus"]):
            continue
        if re.search(r"\b(ph\.?d\.?|doctorate)\b", lower):
            return DegreeLevel.PHD
        if re.search(r"\b(master'?s?|ms|m\.s\.)\b", lower):
            return DegreeLevel.MASTER
    return DegreeLevel.NONE


def parse_job(raw: RawJob) -> ParsedJob:
    title = raw.title.lower()
    text = f" {raw.title} {raw.location_raw} {raw.description_raw} ".lower()
    requirements_text = f"{raw.title} {raw.description_raw}".lower()
    level = _detect_level(title)
    seniority = SENIORITY_BY_LEVEL[level]

    remote_type = work_arrangement(raw)[0]

    if _contains(title, ["analytics engineer", "data engineer"]):
        family = "analytics_engineering"
    elif "data scientist" in title:
        family = "data_science"
    elif _contains(title, ["bi analyst", "business intelligence", "bi developer"]):
        family = "business_intelligence"
    elif "analyst" in title:
        family = "data_analytics"
    else:
        family = "other"

    skills = {canonical for canonical, aliases in SKILL_ALIASES.items() if _contains(text, aliases)}
    citizenship = _requires_citizenship(text)
    clearance = _contains(text, ["security clearance", "secret clearance", "top secret", "ts/sci"])
    degree_required = _extract_degree_required(requirements_text)
    if _contains(text, VISA_REJECTION_TERMS):
        visa_support = VisaSupport.DOES_NOT_SUPPORT
    elif _contains(text, VISA_SUPPORT_TERMS):
        visa_support = VisaSupport.SUPPORTS
    else:
        visa_support = VisaSupport.UNKNOWN
    ambiguities = set()
    if remote_type == RemoteType.UNKNOWN:
        ambiguities.add("remote")
    if seniority == Seniority.UNKNOWN:
        ambiguities.add("seniority")
    if _contains(text, ["authorized to work", "sponsorship"]):
        ambiguities.add("visa")

    return ParsedJob(
        raw=raw,
        seniority=seniority,
        level=level,
        remote_type=remote_type,
        job_family=family,
        tech_keywords=skills,
        requires_citizenship=citizenship,
        requires_clearance=clearance,
        visa_support=visa_support,
        employment_type="contract"
        if "contract" in text
        else "full_time"
        if "full-time" in text
        else "unknown",
        required_years_min=_extract_required_years(requirements_text),
        degree_required=degree_required,
        people_management=_contains(
            text,
            [
                "manage a team",
                "managing a team",
                "lead a team",
                "leading a team",
                "direct reports",
                "people management",
                "hire and develop",
            ],
        ),
        ambiguities=ambiguities,
    )


def location_eligible(job: ParsedJob, preferences: SearchPreferences) -> bool:
    location = job.raw.location_raw.strip()
    location_lower = location.casefold()
    foreign_hits = _discovery_hits(location_lower, FOREIGN_LOCATION_TERMS)
    us_evidence = _discovery_hits(location_lower, US_LOCATION_TERMS)
    smartrecruiters = job.raw.metadata.get("smartrecruiters")
    country_code = (
        smartrecruiters.get("country_code") if isinstance(smartrecruiters, dict) else None
    )
    if (
        isinstance(country_code, str)
        and re.fullmatch(r"[a-z]{2}", country_code)
        and country_code != "us"
        and not us_evidence
    ):
        return False
    if foreign_hits and not us_evidence:
        return False
    if job.remote_type == RemoteType.REMOTE:
        return preferences.include_remote
    if not preferences.location_terms:
        return job.remote_type != RemoteType.REMOTE
    return _contains(job.raw.location_raw, preferences.location_terms)


def _term_hits(text: str, terms: list[str]) -> set[str]:
    return {term.lower() for term in terms if _contains(text, [term])}


def _term_score(text: str, terms: list[str], target_hits: int = 5) -> tuple[float, set[str]]:
    hits = _term_hits(text, terms)
    return min(1.0, len(hits) / max(1, min(target_hits, len(terms)))), hits


def _clinical_clauses(text: str) -> list[str]:
    """Return short posting clauses for conservative clinical evidence checks."""
    text = re.sub(r"<[^>]+>", "\n", text)
    return [" ".join(part.split()).strip() for part in re.split(r"[\n.;:]+", text) if part.strip()]


def _clinical_transition_title(title: str) -> bool:
    """Bounded support titles, not associate managers or monitoring occupations."""
    return bool(
        re.fullmatch(
            r"(?:senior\s+|sr\.?\s+)?(?:clinical\s+study\s+(?:associate|specialist)|"
            r"clinical\s+project\s+associate|(?:clinical\s+)?project\s+support\s+specialist|"
            r"clinical\s+trials?\s+assistant)(?:\s*[-–—,(].*)?",
            title.strip(), re.I,
        )
        and not re.search(
            r"\b(?:manager|director|head|lead|leader|monitoring|cra|nurse)\b", title, re.I
        )
    )


def _clinical_coordination_evidence(title: str, description: str) -> set[str]:
    """Find bounded human-study coordination evidence for clinical discovery."""
    lower_title = title.casefold()
    clauses = _clinical_clauses(description)
    evidence: set[str] = set()
    if (re.search(r"\bclinical\s+trials?\s+administrator\b", lower_title)
            or _clinical_transition_title(title)):
        evidence.update(_clinical_trial_administration_evidence(title, description))
    elif re.search(r"\bresearch associate\b", lower_title):
        human = re.compile(
            r"(?:recruit(?:ment)?|enroll(?:ment)?|screen(?:ing)?)"
            r"[^.\n]{0,120}\b(?:participant|subject)s?\b"
            r"|\b(?:participant|subject)s?\b[^.\n]{0,120}"
            r"(?:recruit(?:ment)?|enroll(?:ment)?|screen(?:ing)?)",
            re.I,
        )
        context = re.compile(
            r"\b(?:clinical\s+(?:study|trial|research)|study|trial|protocol|"
            r"protocol-specific|study\s+visit|informed\s+consent)\b",
            re.I,
        )
        documentation = re.compile(
            r"\b(?:source\s+documents?|source\s+documentation|crf|case\s+report\s+forms?|"
            r"study\s+logs?|protocol\s+compliance|ich[- ]?gcp|gcp\s+compliance|"
            r"clinical\s+study\s+file)\b",
            re.I,
        )
        human_evidence: list[str] = []
        for clause in clauses:
            if human.search(clause) and context.search(clause):
                human_evidence.append(clause)
        if human_evidence and any(documentation.search(clause) for clause in clauses):
            evidence.update(human_evidence)
    elif re.search(r"\bclinical\s+research\s+coordinator\b", lower_title):
        for clause in clauses:
            if re.search(
                r"\bclinical\s+(?:study|trial|research)|\bphase\s+[i1-3]", clause, re.I
            ) and re.search(
                r"\b(?:coordinate|planning|preparation|execution|closeout|close-out|"
                r"study\s+activities|study\s+operations|protocol|study\s+oversight)\b",
                clause,
                re.I,
            ):
                evidence.add(clause)
    elif re.search(
        r"\bclinical\s+enrollment\s+coordinator\b|\benrollment\s+coordinator\b", lower_title
    ):
        for clause in clauses:
            if (
                re.search(
                    r"\b(?:clinical\s+(?:study|trial|research)|protocol|early\s+phase|phase\s+[i1-3])\b",
                    clause,
                    re.I,
                )
                and re.search(r"\b(?:screen|screening|enroll|enrollment)\b", clause, re.I)
                and re.search(r"\b(?:participant|subject|volunteer)s?\b", clause, re.I)
            ):
                evidence.add(clause)
    return set(sorted(evidence)[:3])


def _clinical_trial_administration_evidence(title: str, description: str) -> set[str]:
    transition = _clinical_transition_title(title)
    if not transition and not re.search(r"\bclinical\s+trials?\s+administrator\b", title, re.I):
        return set()
    clauses = _clinical_clauses(description)
    categories = {
        "documents": re.compile(
            r"\b(?:tmf|e?tmf|trial\s+master\s+file|clinical\s+study\s+file|"
            r"investigator\s+files?|source\s+documents?|protocol(?:/(?:sop|regulatory)|[^.]{0,40}documentation)|"
            r"ich[- ]?gcp|gcp\s+compliance|compliance\s+with\s+gcp)\b",
            re.I,
        ),
        "tracking": re.compile(
            r"\b(?:ctms|track(?:ing)?\s+(?:study|study\s+activities|study\s+status|"
            r"(?:study|clinical[- ]study|clinical\s+trial)\s+suppl(?:y|ies)(?:\s+shipments?)?|"
            r"site\s+information)|project-management\s+systems?)\b",
            re.I,
        ),
        "materials": re.compile(
            r"\b(?:study\s+materials?|clinical[- ]study\s+suppl(?:y|ies)|"
            r"(?:study|clinical[- ]study|clinical\s+trial)\s+suppl(?:y|ies)\s+shipments?|site-specific\s+materials?|"
            r"investigator/site\s+(?:setup|documentation))\b",
            re.I,
        ),
        "meetings": re.compile(
            r"\b(?:study|clinical\s+project)\s+team\s+(?:updates?|support)|"
            r"investigator\s+meetings?|study[- ]team\s+(?:minutes?|correspondence)|"
            r"study\s+action[- ]items?\b",
            re.I,
        ),
        "lifecycle": re.compile(
            r"\b(?:study\s+(?:start[- ]?up|initiation|close[- ]?out|archiving)|"
            r"trial[- ]documentation\s+qc|clinical[- ]study\s+audit|"
            r"(?:clinical\s+)?(?:study|trial)\s+(?:audit|capa)\s+tracking)\b",
            re.I,
        ),
    }
    context = re.compile(
        r"\b(?:clinical\s+(?:trial|study|research\s+project)|study\s+protocol|"
        r"clinical\s+(?:project|study)\s+team|investigator|clinical\s+site|"
        r"ich[- ]?gcp|gcp|clinical\s+regulatory)\b",
        re.I,
    )
    if transition:
        # Reuse the administration categories without changing the existing path.
        # New variants require duties, not a title or employer boilerplate alone.
        categories.update({
            "meetings": re.compile(
                categories["meetings"].pattern + r"|\bstudy\s+(?:team\s+)?meetings?\b", re.I
            ),
            "reporting": re.compile(
                r"\b(?:study\s+(?:reports?|metrics)|reports?\s+and\s+metrics)\b", re.I
            ),
            "coordination": re.compile(
                r"\b(?:study\s+timelines?|"
                r"project\s+action\s+items?|project\s+plans?)\b", re.I
            ),
        })
        duty = re.compile(
            r"\b(?:(?:support|assist|maintain|track|deliver|collect|review)(?:s|ing)?|"
            r"(?:updat|coordinat|organiz|organis|prepar|reconcil|schedul|collat)"
            r"(?:e|es|ing))\b", re.I
        )
        clauses = [
            clause for clause in clauses
            if (action := duty.search(clause)) and any(
                (task := pattern.search(clause)) and action.start() < task.start()
                for pattern in categories.values()
            )
        ]
        context = re.compile(
            r"\b(?:clinical\s+(?:trials?|study|studies|research|operations|project)|"
            r"study\s+protocol|ich[- ]?gcp|gcp)\b", re.I
        )
    hits = {
        name: [clause for clause in clauses if pattern.search(clause)]
        for name, pattern in categories.items()
    }
    present = [name for name, values in hits.items() if values]
    if len(present) < 2:
        return set()
    contextual = any(
        any(pattern.search(clause) for pattern in categories.values())
        and (bool(context.search(clause)) if transition else
             any(context.search(nearby) for nearby in clauses[max(0, index - 1) : index + 2]))
        for index, clause in enumerate(clauses)
    )
    if not contextual:
        return set()
    excerpts = [clause for name in present for clause in hits[name]]
    label = "clinical study support" if transition else "clinical trial administration"
    return {f"{label}: {clause}" for clause in sorted(set(excerpts))[:3]}


def _clinical_project_support_evidence(title: str, description: str) -> set[str]:
    """Find task-level clinical project-support evidence, excluding boilerplate."""
    if not re.search(r"\bproject\s+(?:specialist|coordinator)\b", title, re.I):
        return set()
    clauses = _clinical_clauses(description)
    support = re.compile(
        r"\b(?:project\s+(?:management\s+)?plans?|project-level\s+(?:reports|metrics)|"
        r"action\s+items?|project\s+reports?|track(?:ing)?\s+(?:deliverables|actions)|"
        r"project\s+initiation|project\s+activities)\b",
        re.I,
    )
    clinical = re.compile(
        r"\b(?:clinical\s+(?:study|trial|operations|research)|study\s+(?:team|execution|deliverables)|"
        r"clinical\s+research|clinical\s+operations)\b",
        re.I,
    )
    operational = re.compile(
        r"\b(?:tmf|e?tmf|ctms|study\s+(?:start[ -]?up|initiation|close[ -]?out)|"
        r"site\s+(?:activation|payments?)|clinical\s+supplies|ich[- ]?gcp|protocol\s+compliance|"
        r"archive(?:d|ing)?\s+(?:documents|records))\b",
        re.I,
    )
    matched = [clause for clause in clauses if support.search(clause)]
    if not matched or not any(clinical.search(clause) for clause in clauses):
        return set()
    if not any(operational.search(clause) for clause in clauses):
        return set()
    return set(sorted(set(matched))[:3])


def _level_fit(
    job: ParsedJob, candidate: CandidateProfile, company_ndx_member: bool = False
) -> tuple[float, int]:
    if job.level == JobLevel.UNKNOWN:
        return 0.7, 0
    gap = LEVEL_RANK[job.level] - LEVEL_RANK[candidate.current_level]
    adjusted_gap = gap
    if candidate.company_scale == "small" and company_ndx_member and gap > 0:
        adjusted_gap += 0.5
    if gap == 0:
        return 1.0, gap
    if adjusted_gap > 0:
        return max(0.0, 1 - 0.45 * adjusted_gap), gap
    return max(0.85, 1 + 0.075 * adjusted_gap), gap


def _years_fit(job: ParsedJob, candidate: CandidateProfile) -> float:
    if job.required_years_min is None:
        return 1.0
    gap = job.required_years_min - candidate.years_experience
    return 1.0 if gap <= 0 else max(0.0, 1 - 0.18 * gap)


_PM_CATEGORY_PATTERNS = {
    "planning_schedule": r"\b(?:project (?:management )?plans?|timelines?|milestones?|project dependencies|project schedules?)\b",
    "coordination_stakeholders": r"\b(?:(?:cross[ -]functional|stakeholder|sponsor|client|vendor|project[ -]team) coordination|coordinat\w* (?:with )?(?:cross[ -]functional (?:scientific )?(?:teams?|activities|stakeholders?)|stakeholders?|sponsors?|clients?|vendors?|project teams?))\b",
    "tracking_governance": r"\b(?:status report\w*|report(?:s|ed|ing)? project status|(?:action[ -]item|risk|issue|status) tracking|track\w* (?:project )?(?:actions?|action items?|risks?|issues?|status)|project governance|project meeting follow[ -]up)\b",
    "deliverables_control": r"\b(?:project deliverables?|project documentation|change control|validation coordination|regulated project documentation)\b",
    "resource_financial": r"\b(?:resource coordination|coordinat\w* (?:project )?resources|project (?:budgets?|forecast\w*|invoicing|financial tracking))\b",
}


_PM_DUTY_ACTION = re.compile(
    r"^(?:[-*•]\s*)?(?:(?:you|the role|this role|the coordinator)\s+)?"
    r"(?:(?:will|must|is responsible for|are responsible for|responsible for|"
    r"responsibilities include|duties include)\s+)?"
    r"(?:(?:maintain|develop|track|support|own|lead|monitor|deliver|report)"
    r"(?:s|ed|ing)?|(?:manag|prepar|updat|facilitat|ensur|coordinat)(?:e[sd]?|ing)|"
    r"oversee(?:s|ing)?|oversaw|led)\s+", re.I,
)
_PM_WORK_DOMAIN = re.compile(
    r"\b(?:(?:biotech(?:nology)?|pharma(?:ceutical)?s?|biopharma(?:ceutical)?s?|"
    r"life[ -]sciences?|cro|contract research organization|clinical|gmp|gxp|glp)\s+"
    r"(?:(?:development|operations|validation|research|scientific|quality)\s+){0,2}"
    r"(?:projects?|operations|research|deliverables|development)|"
    r"(?:scientific|laboratory|lab) operations|regulated research)\b", re.I,
)

_PM_AFFIRMATIVE_FALLBACK_PATTERNS = (
    r"\b(?:manage|manages|managed|managing|lead|leads|led|leading)\s+(?:\w+\s+){0,2}projects?\b",
    r"\b(?:coordinate|coordinates|coordinated|coordinating)\s+(?:\w+\s+){0,2}projects?\b",
    r"\b(?:coordinate|coordinates|coordinated|coordinating)\s+(?:\w+\s+){0,3}"
    r"(?:project\s+)?(?:teams?|stakeholders?|sponsors?|clients?|vendors?)\b",
    r"\b(?:develop|develops|developed|developing|maintain|maintains|maintained|maintaining)\s+"
    r"(?:\w+\s+){0,2}project\s+(?:plans?|schedules?|timelines?|milestones?)\b",
    r"\b(?:own|owns|owned|owning|track|tracks|tracked|tracking|monitor|monitors|monitored|monitoring)\s+"
    r"(?:project\s+)?(?:milestones?|actions?|action items?|risks?|issues?|deliverables?|"
    r"timelines?|status)\b",
    r"\b(?:report|reports|reported|reporting)\s+(?:project\s+)?status\b",
    r"\bfacilitate(?:s|d|ing)?\s+project\s+meetings?\b",
    r"\bsupport(?:s|ed|ing)?\s+project\s+(?:planning|execution)\b",
)
_PM_AFFIRMATIVE_ACTION = re.compile(
    r"\b(?:manage|manages|managed|managing|lead|leads|led|leading|coordinate|coordinates|"
    r"coordinated|coordinating|develop|develops|developed|developing|maintain|maintains|"
    r"maintained|maintaining|own|owns|owned|owning|track|tracks|tracked|tracking|monitor|"
    r"monitors|monitored|monitoring|report|reports|reported|reporting|facilitate|facilitates|"
    r"facilitated|facilitating|support|supports|supported|supporting)\b",
    re.I,
)


def _affirmative_pm_fallback_evidence(description: str) -> bool:
    """Require an assigned project-management action for the broad fallback."""
    for clause in _clinical_clauses(html.unescape(description)):
        if re.search(
            r"\b(?:no|not|without|preferred|experience|familiarity|company|employer|"
            r"organization|department|capability|discipline)\b",
            clause,
            re.I,
        ):
            continue
        action = _PM_DUTY_ACTION.match(clause) or _PM_AFFIRMATIVE_ACTION.search(clause)
        if not action:
            continue
        if re.search(
            r"\b(?:is|are|means|refers to|includes?|involves?)\b",
            clause[action.end():],
            re.I,
        ):
            continue
        if any(re.search(pattern, clause, re.I) for pattern in _PM_AFFIRMATIVE_FALLBACK_PATTERNS):
            return True
    return False


def _transferable_pm_evidence(title: str, description: str) -> set[str]:
    """Path B: bounded titles, credible domain, and three distinct PM categories.

    This is additive to the existing clinical and explicit regulated-title paths.
    Category evidence comes from affirmative duty clauses, never title keywords.
    """
    if not re.fullmatch(
        r"(?:(?:(?:scientific|operations|gxp|gmp|validation) )?project "
        r"(?:coordinator|specialist|manager)|associate project manager|"
        r"(?:(?:scientific|regulated research) )?(?:program|study) coordinator)"
        r"(?:\s*(?:[-–—,(]).*)?",
        title.strip(), re.I,
    ) or re.search(
        r"\b(?:senior|sr|director|head|principal|executive|president|scientist|"
        r"technician|engineer|engineering|medical|cra|monitoring|data management|"
        r"statistical programming|software|construction|marketing|qa|qc|program lead)\b",
        title, re.I,
    ):
        return set()
    # Strong execution/leadership duties cannot be rescued by PM vocabulary.
    if re.search(
        r"\b(?:perform\w*|conduct\w*|execut\w*)\s+(?:\w+\s+){0,3}"
        r"(?:assays?|bench experiments?|laboratory testing|site monitoring|monitoring visits|"
        r"qc testing|statistical programming)\b|"
        r"\b(?:principal investigator|study director|independent scientific leadership)\b",
        description, re.I,
    ):
        return set()
    categories = set()
    work_domain = False
    for clause in _clinical_clauses(html.unescape(description)):
        if re.search(
            r"\b(?:no|not|without|preferred|experience|familiarity|"
            r"company|employer|organization|department|capability|discipline)\b",
            clause, re.I,
        ):
            continue
        action = _PM_DUTY_ACTION.match(clause)
        if not action:
            continue
        # Gerunds can also name concepts ("Tracking ... is ...").
        if re.search(
            r"\b(?:is|are|means|refers to|includes?|involves?)\b",
            clause[action.end():], re.I,
        ):
            continue
        # One affirmative work-context clause can ground the other PM duties;
        # employer descriptions and isolated industry nouns cannot do so.
        if not re.search(r"\b(?:software|IT|construction|marketing)\b", clause, re.I):
            work_domain |= bool(_PM_WORK_DOMAIN.search(clause))
        categories.update(
            name for name, pattern in _PM_CATEGORY_PATTERNS.items()
            if re.search(pattern, clause, re.I)
        )
    return categories if work_domain and len(categories) >= 3 else set()


def _match_discovery(
    job: ParsedJob,
    profile: ProfileConfig,
    preferences: SearchPreferences,
    resume: ResumeProfile | None = None,
    *,
    visa_sponsorship_required: bool = False,
    company_visa_support: VisaSupport = VisaSupport.UNKNOWN,
    candidate: CandidateProfile | None = None,
    company_ndx_member: bool = False,
) -> MatchResult:
    if (
        preferences.exclude_citizenship_required
        and job.requires_citizenship
        or preferences.exclude_clearance_required
        and job.requires_clearance
    ):
        return MatchResult(
            profile=profile.name,
            score=0,
            eligible=False,
            tier="filtered",
            filtered_reason="citizenship_or_clearance",
        )
    if visa_sponsorship_required and (
        job.visa_support == VisaSupport.DOES_NOT_SUPPORT
        or company_visa_support == VisaSupport.DOES_NOT_SUPPORT
    ):
        return MatchResult(
            profile=profile.name,
            score=0,
            eligible=False,
            tier="filtered",
            filtered_reason="visa_sponsorship",
        )
    if job.seniority in preferences.excluded_seniorities:
        return MatchResult(
            profile=profile.name,
            score=0,
            eligible=False,
            tier="filtered",
            filtered_reason="seniority",
        )
    if job.job_family == "other" and not profile.allow_other_job_family:
        return MatchResult(
            profile=profile.name,
            score=0,
            eligible=False,
            tier="filtered",
            filtered_reason="job_family",
        )
    if preferences.candidate_eligibility is None and not location_eligible(job, preferences):
        return MatchResult(
            profile=profile.name,
            score=0,
            eligible=False,
            tier="filtered",
            filtered_reason="location",
        )

    raw_gap = 0
    bucket = "target"
    fit = 0.0
    reach = 1.0
    if candidate:
        level_fit, raw_gap = _level_fit(job, candidate, company_ndx_member)
        years_fit = _years_fit(job, candidate)
        reach = round(level_fit * years_fit, 3)
        bucket = "target" if reach >= 0.7 else "stretch" if reach >= 0.35 else "unrealistic"
        if job.level != JobLevel.UNKNOWN and raw_gap > candidate.max_level_reach:
            return MatchResult(
                profile=profile.name,
                score=0,
                eligible=False,
                tier="filtered",
                filtered_reason="level_reach",
                fit=0.0,
                reach=reach,
                bucket=bucket,
                level=job.level.value,
                required_years_min=job.required_years_min,
            )
        if bucket == "unrealistic":
            return MatchResult(
                profile=profile.name,
                score=0,
                eligible=False,
                tier="filtered",
                filtered_reason="reach",
                fit=0.0,
                reach=reach,
                bucket=bucket,
                level=job.level.value,
                required_years_min=job.required_years_min,
            )

    title_text = job.raw.title.lower()
    title_desc = f"{job.raw.title} {job.raw.description_raw}".lower()
    title_hit = (
        bool(_discovery_hits(title_text, profile.title_terms))
        if profile.allow_other_job_family
        else _contains(title_text, profile.title_terms)
    )
    responsibility_hits = _discovery_hits(job.raw.description_raw, profile.responsibility_terms)
    domain_hits = _discovery_hits(title_desc, profile.domain_terms)
    responsibility_title_hits = _discovery_hits(title_text, profile.responsibility_title_terms)
    responsibility_title_exclusions = _discovery_hits(
        title_text, profile.responsibility_title_exclude_terms
    )
    clinical_coordination_hits: set[str] = set()
    clinical_project_support_hits: set[str] = set()
    transferable_pm_hits: set[str] = set()
    if profile.name == "clinical-discovery":
        transferable_pm_hits = _transferable_pm_evidence(
            job.raw.title, job.raw.description_raw
        )
        clinical_coordination_hits = _clinical_coordination_evidence(
            job.raw.title, job.raw.description_raw
        )
        clinical_project_support_hits = _clinical_project_support_evidence(
            job.raw.title, job.raw.description_raw
        )
    bounded_discovery_hit = bool(
        clinical_coordination_hits or clinical_project_support_hits or transferable_pm_hits
    )
    if (
        profile.allow_other_job_family
        and not title_hit
        and responsibility_hits
        and not bounded_discovery_hit
    ):
        if (
            len(responsibility_hits) < profile.responsibility_min_hits
            or profile.responsibility_requires_domain
            and not domain_hits
            or not responsibility_title_hits
            or responsibility_title_exclusions
            or not _affirmative_pm_fallback_evidence(job.raw.description_raw)
        ):
            return MatchResult(
                profile=profile.name,
                score=0,
                eligible=False,
                tier="filtered",
                filtered_reason="discovery_responsibility_evidence",
            )
    title_score = 1.0 if title_hit or responsibility_hits or bounded_discovery_hit else 0.0
    profile_domain_score = min(
        1.0, sum(term.lower() in title_desc for term in profile.domain_terms) / 3
    )
    desired_skills = {skill.lower() for skill in profile.skills}
    profile_skill_score = len({x.lower() for x in job.tech_keywords} & desired_skills) / max(
        1, min(5, len(desired_skills))
    )
    resume_hits: set[str] = set()
    resume_skill_score = 0.0
    resume_domain_score = 0.0
    if resume:
        resume_domain_score, resume_hits = _term_score(title_desc, resume.keywords)
        resume_skills = {skill.lower() for skill in resume.skills}
        if resume_skills:
            resume_skill_score = len({x.lower() for x in job.tech_keywords} & resume_skills) / max(
                1, min(5, len(resume_skills))
            )
    domain_score = max(profile_domain_score, resume_domain_score)
    skill_score = max(profile_skill_score, resume_skill_score)
    location_score = 1.0
    seniority_score = 1.0 if job.seniority in {Seniority.MID, Seniority.SENIOR} else 0.6
    if candidate:
        seniority_score = reach

    dimensions = {
        "title": title_score,
        "domain": domain_score,
        "skills": min(skill_score, 1.0),
        "location": location_score,
        "seniority": seniority_score,
    }
    score = round(
        sum(dimensions.get(key, 0) * weight for key, weight in profile.weights.items()), 3
    )
    fit_weights = {
        key: profile.weights.get(key, 0.0) for key in ("title", "domain", "skills", "location")
    }
    fit_weight_total = sum(fit_weights.values())
    fit = (
        sum(dimensions[key] * weight for key, weight in fit_weights.items()) / fit_weight_total
        if fit_weight_total
        else 0.0
    )
    reasons = [f"{key}: {value:.0%}" for key, value in dimensions.items() if value >= 0.5]
    if resume_hits:
        reasons.append("resume: " + ", ".join(sorted(resume_hits)[:5]))
    if responsibility_hits:
        reasons.append("responsibilities: " + ", ".join(sorted(responsibility_hits)[:5]))
    if clinical_coordination_hits:
        reasons.append("clinical coordination: " + " | ".join(sorted(clinical_coordination_hits)))
    if clinical_project_support_hits:
        reasons.append(
            "clinical project support: " + " | ".join(sorted(clinical_project_support_hits))
        )
    if transferable_pm_hits:
        reasons.append("transferable life-science PM: " + ", ".join(sorted(transferable_pm_hits)))
    gaps = []
    penalty = 0.0
    if candidate:
        if (
            job.required_years_min is not None
            and job.required_years_min > candidate.years_experience
        ):
            gaps.append(
                f"要求 {job.required_years_min} 年經驗（你 {candidate.years_experience} 年）"
            )
        if (
            job.degree_required == DegreeLevel.PHD
            and candidate.has_advanced_degree != DegreeLevel.PHD
        ):
            gaps.append("要求 PhD 學位")
            penalty += 0.25
        elif (
            job.degree_required == DegreeLevel.MASTER
            and candidate.has_advanced_degree == DegreeLevel.NONE
        ):
            gaps.append("要求碩士學位")
            penalty += 0.10
        if job.people_management and candidate.people_managed == 0:
            gaps.append("需要帶人經驗")
            penalty += 0.15
        if raw_gap > 0:
            gaps.append(f"職級高於現職 {raw_gap} 級")
        score = round(score * (1 - min(0.5, penalty)), 3)
    if visa_sponsorship_required and job.visa_support == VisaSupport.SUPPORTS:
        reasons.append("visa: sponsorship available")
    elif visa_sponsorship_required and company_visa_support in {
        VisaSupport.SUPPORTS,
        VisaSupport.LIKELY_SUPPORTS,
    }:
        reasons.append("visa: company likely supports sponsorship")
    elif visa_sponsorship_required and company_visa_support == VisaSupport.CASE_BY_CASE:
        gaps.append("visa sponsorship case-by-case")
    elif visa_sponsorship_required and job.visa_support == VisaSupport.UNKNOWN:
        gaps.append("visa sponsorship unknown")
    for label, terms in profile.gap_terms.items():
        if any(term.lower() in title_desc for term in terms):
            gaps.append(label)
    tier = (
        "strong"
        if score >= profile.strong_threshold
        else "match"
        if score >= profile.threshold
        else "below_threshold"
    )
    return MatchResult(
        profile=profile.name,
        score=score,
        eligible=score >= profile.threshold,
        tier=tier,
        reasons=reasons,
        gaps=gaps,
        fit=round(fit, 3),
        reach=reach,
        bucket=bucket,
        level=job.level.value if candidate else None,
        required_years_min=job.required_years_min if candidate else None,
        review_flags=(
            _clinical_review_flags(job.raw.title, job.raw.description_raw)
            if profile.name == "clinical-discovery"
            else []
        ),
    )


def match_job(
    job: ParsedJob,
    profile: ProfileConfig,
    preferences: SearchPreferences,
    resume: ResumeProfile | None = None,
    *,
    visa_sponsorship_required: bool = False,
    company_visa_support: VisaSupport = VisaSupport.UNKNOWN,
    candidate: CandidateProfile | None = None,
    company_ndx_member: bool = False,
) -> MatchResult:
    """Keep discovery scoring intact, then enforce independent candidate facts."""
    if preferences.candidate_eligibility is not None:
        job = job.model_copy(update={"remote_type": work_arrangement(job.raw)[0]})
    result = _match_discovery(
        job, profile, preferences, resume,
        visa_sponsorship_required=visa_sponsorship_required,
        company_visa_support=company_visa_support,
        candidate=candidate, company_ndx_member=company_ndx_member,
    )
    if preferences.candidate_eligibility is not None:
        assessment = assess_candidate(job.raw, preferences)
        result.discovery_eligible = result.eligible
        result.candidate_eligibility = assessment
        result.eligibility_reasons = assessment.hard_reasons + assessment.review_reasons
        result.eligible = result.eligible and assessment.status == "eligible"
        # Discovery tier, score, reasons and existing career filters remain intact.
    return result
