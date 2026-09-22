"""Deterministic candidate gates. Discovery scores are not qualification evidence.

No geocoding or routing: configured states screen physical attendance, while
uncertain opportunities remain available for review, outside immediate alerts.
"""

from __future__ import annotations

import html
import re
from typing import TYPE_CHECKING

from .models import AttendanceEvidence, EligibilityAssessment, LocationEvidence, RawJob, RemoteType

if TYPE_CHECKING:
    from .config import SearchPreferences


STATE_NAMES = dict(
    zip(
        "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC".split(),
        "Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|Florida|Georgia|Hawaii|Idaho|Illinois|Indiana|Iowa|Kansas|Kentucky|Louisiana|Maine|Maryland|Massachusetts|Michigan|Minnesota|Mississippi|Missouri|Montana|Nebraska|Nevada|New Hampshire|New Jersey|New Mexico|New York|North Carolina|North Dakota|Ohio|Oklahoma|Oregon|Pennsylvania|Rhode Island|South Carolina|South Dakota|Tennessee|Texas|Utah|Vermont|Virginia|Washington|West Virginia|Wisconsin|Wyoming|District of Columbia".split(
            "|"
        ),
        strict=True,
    )
)

# Extend this vocabulary, not employer/title exclusions. LPN and LVN remain
# distinct unless the posting explicitly accepts either credential.
LICENSE_PATTERNS = {
    "RN": r"(?<!\w)(?:RN|R\.N\.|registered nurse)(?!\w)",
    "LPN": r"(?<!\w)(?:LPN|L\.P\.N\.|licensed practical nurse)(?!\w)",
    "LVN": r"(?<!\w)(?:LVN|L\.V\.N\.|licensed vocational nurse)(?!\w)",
    "NP": r"\b(?:NP|APRN|nurse practitioner|advanced practice registered nurse)\b",
    "PA": r"\b(?:physician assistant|PA-C)\b",
    "PHYSICIAN": r"\b(?:physician(?! assistant)|medical license|MD/DO)\b",
    "PHARMACIST": r"\bpharmacist\b",
}


def normalize_state(value: str) -> str | None:
    value = value.strip()
    return next(
        (
            code
            for code, name in STATE_NAMES.items()
            if value.casefold() in {code.casefold(), name.casefold()}
        ),
        None,
    )


def states_in(text: str) -> set[str]:
    # Abbreviations must be uppercase: prose "in" and "or" are not states.
    found = set()
    for code, name in sorted(STATE_NAMES.items(), key=lambda item: -len(item[1])):
        pattern = r"\b" + re.escape(name) + r"\b"
        if re.search(pattern, text, re.I):
            found.add(code)
            text = re.sub(pattern, " ", text, flags=re.I)
    return found | {code for code in STATE_NAMES if re.search(r"\b" + code + r"\b", text)}


def license_names(text: str) -> set[str]:
    if text.strip().upper() in LICENSE_PATTERNS:
        return {text.strip().upper()}
    names = {name for name, pattern in LICENSE_PATTERNS.items() if re.search(pattern, text, re.I)}
    if "NP" in names and "advanced practice registered nurse" in text.lower():
        names.discard("RN")
    return names


def plain_text(text: str) -> str:
    for _ in range(2):
        text = html.unescape(text)
    text = re.sub(r"</?(?:p|li|ul|ol|div|h[1-6]|br)\b[^>]*>", "\n", text, flags=re.I)
    return re.sub(r"<[^>]+>", "", text)


def clauses(text: str) -> list[str]:
    text = re.sub(r"\bR\.N\.", "RN", plain_text(text), flags=re.I)
    text = re.sub(r"\bL\.P\.N\.", "LPN", text, flags=re.I)
    text = re.sub(r"\bL\.V\.N\.", "LVN", text, flags=re.I)
    return [
        " ".join(part.split())
        for part in re.split(r"[\n;]+|(?<=[.!?])\s+(?=[A-Z])", text)
        if part.strip()
    ]


def _structured(raw: RawJob) -> dict:
    value = raw.metadata.get("eligibility", {})
    return value if isinstance(value, dict) else {}


def credential_clauses(text: str) -> list[str]:
    """Keep section meaning before ATS HTML is flattened to display text."""
    mode = ""
    result = []
    separated = []
    for clause in clauses(re.sub(r"\bbut\b|\bwhereas\b", "\n", text, flags=re.I)):
        if re.search(r"\bpreferred\b", clause, re.I) and re.search(r"\brequired\b", clause, re.I):
            separated.extend(re.split(r"\band\b", clause, flags=re.I))
        else:
            separated.append(clause)
    for clause in separated:
        if re.fullmatch(
            r"(?:preferred|desired|nice to have)(?: qualifications| requirements| credentials)?[: ]*",
            clause,
            re.I,
        ):
            mode = "Preferred: "
            continue
        if re.fullmatch(
            r"(?:(?:required|minimum)(?: qualifications| requirements| credentials)?(?: and experience)?|requirements)[: ]*",
            clause,
            re.I,
        ):
            mode = "Required: "
            continue
        if re.fullmatch(
            r"(?:qualifications|responsibilities|what you bring to the team|benefits)[: ]*",
            clause,
            re.I,
        ):
            mode = ""
        if license_names(clause) or re.search(
            r"\b(?:professional|clinical|nursing)\s+licen[cs]e\b", clause, re.I
        ):
            result.append(mode + clause)
    return result


def license_rejections(raw: RawJob, held: set[str] | None) -> list[str]:
    held = held or set()
    evidence: list[tuple[str, bool]] = [(raw.title, True)]
    evidence.extend((clause, False) for clause in credential_clauses(raw.description_raw))
    for value in _structured(raw).get("requirements", []):
        evidence.extend((clause, False) for clause in credential_clauses(str(value)))
    for value in _structured(raw).get("required_licenses", []):
        evidence.append((str(value), True))
    rejected = []
    for clause, occupational in evidence:
        names = license_names(clause)
        if not names:
            if re.search(
                r"\b(?:professional|clinical|nursing)\s+licen[cs]e\b.*\brequired\b", clause, re.I
            ) and not re.search(r"not required|preferred", clause, re.I):
                rejected.append(f"required_license_unknown: {clause}")
            continue
        if re.search(r"\b(preferred|desirable|optional|not required|a plus)\b", clause, re.I):
            continue
        if re.search(
            r"\b(?:work(?:ing)?|collaborat\w*)\s+with\b|\b(?:supervise|support|manage)\s+(?:the\s+)?(?:nurses|licensed staff)\b",
            clause,
            re.I,
        ):
            continue
        mandatory = occupational or bool(
            re.search(
                r"\b(required|must|mandatory|shall|current|active|valid|unrestricted)\b",
                clause,
                re.I,
            )
        )
        if not mandatory:
            continue
        alternatives = bool(re.search(r"\bor\b|/", clause, re.I))
        satisfied = bool(names & held) if alternatives else names <= held
        if satisfied:
            continue
        if alternatives and re.search(r"\b(degree|experience|equivalent)\b", clause, re.I):
            rejected.append(f"license_alternative_unverified: {clause}")
        else:
            rejected.append(f"required_license_not_held ({', '.join(sorted(names))}): {clause}")
    return list(dict.fromkeys(rejected))


REMOTE = r"\b(?:remote|home[- ]based|work(?:ing)? from home|telecommut\w*)\b"
PHYSICAL = r"\b(?:on[- ]?site|in[- ]person|office[- ]based|clinic[- ]based|physical attendance)\b"


def work_arrangement(raw: RawJob) -> tuple[RemoteType, list[str]]:
    """Prefer explicit job evidence; generic company remote boilerplate is ignored."""
    structured = _structured(raw)
    labels = [raw.location_raw, raw.title, str(structured.get("work_arrangement", ""))]
    body = clauses(raw.description_raw)
    remote, physical, hybrid = [], [], []
    for text in labels + body:
        is_label = text in labels
        job_context = is_label or bool(
            re.search(
                r"\b(?:this|the)\s+(?:role|position|job)\b|\b(?:you will|you must|remote based|remote (?:role|position|job)|home[- ]based|work from home|fully remote|100% remote)\b",
                text,
                re.I,
            )
        )
        if re.search(r"\bhybrid\b", text, re.I) and job_context:
            hybrid.append(text)
        if re.search(REMOTE, text, re.I) and job_context:
            if re.search(
                r"\b(?:not|no|non)[ -]+remote\b|remote\s+(?:work\s+)?(?:is\s+)?not", text, re.I
            ):
                physical.append(text)
            else:
                remote.append(text)
        if re.search(PHYSICAL, text, re.I) and (
            job_context
            or re.search(
                r"\b(?:required|must|attendance|days?\s+(?:per|a|each)\s+week)\b", text, re.I
            )
        ):
            physical.append(text)
        if re.search(
            r"\b(?:conduct|perform)\b.*\b(?:patient visits|blood draws|phlebotomy|ECGs|EKGs)\b|\b(?:maintain|manage)\b.*\b(?:supplies|equipment|products)\s+onsite\b",
            text,
            re.I,
        ):
            physical.append(text)
        if re.search(
            r"\b(?:attend|attendance|report to|work at)\b.*\b(?:clinic|site|office|hospital)\b",
            text,
            re.I,
        ):
            physical.append(text)
    attendance = attendance_frequency(raw)
    if attendance.evidence:
        physical.extend(attendance.evidence)
    if hybrid:
        return RemoteType.HYBRID, []
    if remote and physical:
        return RemoteType.UNKNOWN, [f"conflicting_work_arrangement: {remote[0]} / {physical[0]}"]
    if physical:
        return RemoteType.ONSITE, []
    if remote:
        return RemoteType.REMOTE, []
    if raw.location_raw.strip():
        return RemoteType.ONSITE, []
    return RemoteType.UNKNOWN, ["work_arrangement_unknown: no reliable work arrangement/location"]


def _location_key(value: str) -> str:
    text = value.casefold().strip()
    for code, name in STATE_NAMES.items():
        text = re.sub(r"\b" + re.escape(name.casefold()) + r"\b", code.casefold(), text)
    text = re.sub(r"\b(?:united states(?: of america)?|usa|us|on[- ]?site|hybrid)\b", "", text)
    return " ".join(re.sub(r"[^\w]+", " ", text).split())


def remote_rejections(raw: RawJob, preferences: SearchPreferences) -> list[str]:
    cfg = preferences.candidate_eligibility
    if not preferences.include_remote:
        return ["remote_not_allowed: include_remote is false"]
    unknown = []
    restrictions = []
    excluded = set()
    scope_evidence = [raw.location_raw]
    structured = _structured(raw)
    structured_locations = structured.get("applicant_locations", [])
    if structured_locations:
        scope_evidence.extend(str(x) for x in structured_locations)
        allowed_states = set().union(*(states_in(str(x)) for x in structured_locations))
        if not allowed_states and not re.search(
            r"United States|\bUSA?\b|U\.S\.", " ".join(map(str, structured_locations)), re.I
        ):
            unknown.append("remote_residence_unknown_or_incompatible: " + str(structured_locations))
        restrictions.append(allowed_states)
    source_country = raw.metadata.get("smartrecruiters", {}).get("country_code", "")
    if source_country and source_country.lower() != "us":
        return [f"remote_residence_incompatible: source country {source_country}"]
    location_states = states_in(raw.location_raw)
    national_location = bool(re.search(r"United States|\bUSA?\b|U\.S\.", raw.location_raw, re.I))
    if location_states and re.search(REMOTE, raw.location_raw, re.I) and not national_location:
        restrictions.append(location_states)
    for clause in clauses(raw.description_raw) + [raw.title, raw.location_raw]:
        if (
            clause == raw.location_raw
            and national_location
            and not re.search(
                r"only|restricted|limited|excluding|except|cannot|not eligible|not available|must not|may not",
                clause,
                re.I,
            )
        ):
            continue
        if re.search(
            r"\b(?:resid\w*|based|located|work|hiring|available|eligible|permitted|approved|remote|home[- ]based)\b",
            clause,
            re.I,
        ) and re.search(
            r"\b(?:must|only|restricted|limited|cannot|not eligible|eligible states|permitted|approved states|not available|excluding|except|remote|home[- ]based)\b",
            clause,
            re.I,
        ):
            scope_evidence.append(clause)
            states = states_in(clause)
            if re.search(
                r"\b(?:excluding|except|cannot|not eligible|not available|not permitted|must not|may not)\b",
                clause,
                re.I,
            ):
                excluded.update(states)
            elif states:
                restrictions.append(states)
            elif re.search(
                r"\b(?:certain|selected|specific|approved|eligible) states\b|\bonly\b",
                clause,
                re.I,
            ) and not re.search(r"United States|\bUS\b|\bUSA\b|U\.S\.", clause):
                unknown.append(f"remote_residence_unknown: {clause}")
    restrictions = [states for states in restrictions if states]
    if restrictions or excluded:
        if not cfg.residence_state:
            return ["remote_residence_unknown: candidate residence state not configured"]
        if cfg.residence_state in excluded or any(
            cfg.residence_state not in states for states in restrictions
        ):
            return unknown + [
                f"remote_residence_incompatible ({cfg.residence_state}): "
                + " | ".join(scope_evidence)
            ]
        return unknown
    scope = " ".join(scope_evidence)
    if re.search(
        r"\bUnited States(?: of America)?\b|\bUSA?\b|\bU\.S\.|\b(?:worldwide|global)\b",
        scope,
        re.I,
    ):
        return unknown
    return unknown + ["remote_residence_unknown: no explicit US or compatible state scope"]


def attendance_frequency(raw: RawJob) -> AttendanceEvidence:
    """Parse explicit physical days, never infer onsite days from remote days."""
    result = AttendanceEvidence()
    text = plain_text("\n".join([raw.title, raw.location_raw, raw.description_raw]))
    text = text.casefold()
    for word, number in {
        "one": "1",
        "two": "2",
        "three": "3",
        "four": "4",
        "five": "5",
        "six": "6",
        "seven": "7",
    }.items():
        text = re.sub(r"\b" + word + r"\b", number, text)
    physical = bool(re.search(PHYSICAL + r"|patient visits|blood draws|attend the clinic", text))
    result.physical_per_diem = bool(re.search(r"per[- ]diem", text) and physical)
    weeks = set()
    kinds = set()
    # Separate remote and onsite clauses so '3 remote days and 2 onsite days'
    # cannot borrow the physical context for the remote frequency.
    parts = re.split(r"[\n;.!]+|\b(?:and|but)\b", text)
    for part in parts:
        onsite = bool(
            re.search(
                PHYSICAL + r"|(?:at|attend|in) (?:the )?(?:office|clinic|site|hospital)", part
            )
        )
        if not onsite and not (result.physical_per_diem and re.search(r"per[- ]diem", part)):
            continue
        if re.search(r"no (?:required )?on[- ]?site|on[- ]?site.*not required", part):
            continue
        # Remove explicitly remote day phrases before extracting attendance.
        part = re.sub(r"\d+(?:\s*[-\u2013]\s*\d+)?\s+(?:remote|work.from.home)\s+days?", "", part)
        part = re.sub(r"\d+\s+days?\s*(?:/|per |a |each )?week\s+(?:remote|from home)", "", part)
        matches = re.findall(
            r"([1-7])(?:\s*(?:-|\u2013|to)\s*([1-7]))?\s*(?:(?:on[- ]?site|in[- ]person|office)\s+)?days?"
            r"\s*(?:(?:on[- ]?site|in (?:the )?office)\s*)?(?:/|per\s+|a\s+|each\s+)(?:week|wk)\b",
            part,
        )
        if matches:
            weeks.update(max(int(lo), int(hi or lo)) for lo, hi in matches)
            kinds.add("weekly")
            result.evidence.append(part.strip())
        elif re.search(r"monthly|(?:per|a|each) month|/month", part):
            kinds.add("monthly")
            result.evidence.append(part.strip())
        elif re.search(r"occasional(?:ly)?|as needed|periodic(?:ally)?", part):
            kinds.add("occasional")
            result.evidence.append(part.strip())
    result.conflicting = len(weeks) > 1 or len(kinds) > 1
    if weeks:
        result.kind = "weekly"
        result.max_days_per_week = max(weeks)
        result.category = "limited" if max(weeks) <= 2 else "frequent"
    elif kinds:
        result.kind = sorted(kinds)[0]
        result.category = "limited"
    if not kinds and re.search(
        r"(?:daily|full[- ]time).*on[- ]?site|on[- ]?site.*(?:daily|full[- ]time)", text
    ):
        result.category = "frequent"
        result.evidence.append("explicit daily/full-time onsite attendance")
    if result.conflicting:
        result.category = "unknown"
    return result


def location_states(label: str) -> set[str]:
    """Only parse a state in a location-shaped field, not arbitrary body prose."""
    cleaned = re.sub(
        r"\b(?:United States(?: of America)?|USA|US|hybrid|on[- ]?site)\b", "", label, flags=re.I
    )
    cleaned = re.sub(r"\b\d{5}(?:-\d{4})?\b", "", cleaned).strip(" ,-")
    direct = normalize_state(cleaned)
    if direct:
        return {direct}
    # Location fields commonly contain more than one city or a country suffix,
    # for example ``Boston / Waltham, MA`` or ``Boston, MA, United States``.
    # Resolve state tokens wherever they occur in the location-shaped value;
    # do not require the state to be the final comma-delimited component.
    found = states_in(cleaned)
    if found:
        return found
    # A conflicting/foreign trailing component does not disappear into a US match.
    pieces = [part.strip() for part in cleaned.split(",") if part.strip()]
    if len(pieces) >= 2:
        state = normalize_state(pieces[-1])
        return {state} if state else set()
    return set()


def _physical_assessment(
    raw: RawJob, preferences: SearchPreferences, result: EligibilityAssessment
) -> None:
    cfg = preferences.candidate_eligibility
    policy = cfg.commuting_policy
    labels = [part.strip() for part in re.split(r";|\||\n", raw.location_raw) if part.strip()]
    mandatory = []
    state_pattern = "|".join(
        re.escape(name)
        for name in sorted([*STATE_NAMES, *STATE_NAMES.values()], key=len, reverse=True)
    )
    for clause in clauses(raw.description_raw):
        if re.search(r"not required|optional|no attendance", clause, re.I):
            continue
        sites = re.finditer(
            r"(?:on[- ]?site in|office[- ]based in|clinic[- ]based in|report to (?:the )?(?:office|clinic|site) in|work at (?:the )?(?:office|clinic|site) in)\s+([A-Za-z .'-]+,\s*(?:"
            + state_pattern
            + r")\b)",
            clause,
            re.I,
        )
        mandatory.extend(site[1] for site in sites)
    all_labels = list(dict.fromkeys(labels + mandatory))
    possible = policy.onsite_states
    expanded = None if possible is None else possible | policy.limited_attendance_states
    limited = result.attendance.category == "limited"
    uncertain_schedule = result.attendance.category == "unknown"
    allowed = expanded if limited or uncertain_schedule else possible
    approved = {
        _location_key(x)
        for x in preferences.location_terms
        if location_states(x) and len(_location_key(x).split()) > 1
    }
    for label in all_labels:
        states = location_states(label)
        result.locations.append(
            LocationEvidence(
                label=label,
                states=sorted(states),
                status="state_identified" if states else "unresolved",
                source="mandatory_attendance_text" if label in mandatory else "posting_location",
            )
        )

    def incompatible(loc: LocationEvidence) -> bool:
        return bool(allowed is not None and loc.states and not set(loc.states) & allowed)

    # A separately required site is not an alternative to the listing location.
    mandatory_bad = [
        loc for loc in result.locations if loc.label in mandatory and incompatible(loc)
    ]
    all_bad = bool(result.locations) and all(incompatible(loc) for loc in result.locations)
    if mandatory_bad or all_bad:
        evidence = mandatory_bad or result.locations
        result.hard_reasons.append(
            "onsite_geography_incompatible: resolved out-of-scope physical attendance: "
            + "; ".join(loc.label for loc in evidence)
        )
    elif not result.locations or any(not loc.states for loc in result.locations):
        result.review_reasons.append("commute_unresolved: location missing or ambiguous")
    elif len(all_labels) > 1:
        result.review_reasons.append(
            "multiple_attendance_locations: alternatives or mandatory sites need verification"
        )
    elif _location_key(all_labels[0]) in approved and not result.review_reasons:
        # Exact explicitly approved labels only, never a state or substring.
        return
    else:
        reason = "limited_attendance_commute_review" if limited else "commute_not_verified"
        result.review_reasons.append(
            reason + ": practical commute not established; no distance inferred"
        )
    # Show plausible state-level opportunities, not wholly unlocatable records.
    result.review_visible = any(
        loc.states and (expanded is None or set(loc.states) & expanded) for loc in result.locations
    )


def assess_candidate(raw: RawJob, preferences: SearchPreferences) -> EligibilityAssessment:
    result = EligibilityAssessment()
    cfg = preferences.candidate_eligibility
    if cfg is None:
        return result
    result.work_arrangement, arrangement_errors = work_arrangement(raw)
    result.attendance = attendance_frequency(raw)
    result.review_reasons.extend(arrangement_errors)
    if result.attendance.conflicting:
        result.review_reasons.append(
            "conflicting_attendance_frequency: " + " | ".join(result.attendance.evidence)
        )
    for reason in license_rejections(raw, cfg.held_professional_licenses):
        target = (
            result.hard_reasons
            if reason.startswith("required_license_not_held")
            else result.review_reasons
        )
        target.append(reason)
    if result.work_arrangement == RemoteType.REMOTE:
        for reason in remote_rejections(raw, preferences):
            target = (
                result.hard_reasons
                if reason.startswith(("remote_residence_incompatible", "remote_not_allowed"))
                else result.review_reasons
            )
            target.append(reason)
        result.review_visible = not result.review_reasons or bool(
            states_in(raw.location_raw) or re.search(r"United States|\bUSA?\b", raw.location_raw)
        )
    else:
        _physical_assessment(raw, preferences, result)
        if any(reason.startswith("conflicting_work_arrangement") for reason in arrangement_errors):
            # A known residence mismatch still wins when remote/physical evidence conflicts.
            result.hard_reasons.extend(
                reason
                for reason in remote_rejections(raw, preferences)
                if reason.startswith(("remote_residence_incompatible", "remote_not_allowed"))
            )
    result.status = (
        "unsuitable"
        if result.hard_reasons
        else "review_needed"
        if result.review_reasons
        else "eligible"
    )
    if result.status == "unsuitable":
        result.review_visible = False
    return result


def candidate_rejections(raw: RawJob, preferences: SearchPreferences) -> list[str]:
    """Compatibility adapter for queued alerts: BOTH review and unsuitable block."""
    result = assess_candidate(raw, preferences)
    return result.hard_reasons + result.review_reasons if result.status != "eligible" else []
