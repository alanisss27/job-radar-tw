from __future__ import annotations

import asyncio
import email.utils
import hashlib
import json
import logging
import random
import re
import time
from abc import ABC, abstractmethod
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from uuid import UUID

import httpx
from bs4 import BeautifulSoup
from pydantic import ValidationError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .models import AtsType, CompanyConfig, RawJob
from .eligibility import credential_clauses
from .active_status import ActiveStatus, page_status

logger = logging.getLogger(__name__)


def _eligibility_metadata(item: dict) -> dict:
    """Retain explicit ATS requirement facts otherwise lost during normalization."""

    def texts(value):
        if isinstance(value, str):
            return [value]
        if isinstance(value, list):
            return [text for entry in value for text in texts(entry)]
        if isinstance(value, dict):
            return [
                text
                for key in ("name", "value", "text", "content", "addressRegion", "addressCountry")
                for text in texts(value.get(key))
            ]
        return []

    facts = {}
    credential_evidence = [
        clause
        for key in (
            "content",
            "descriptionHtml",
            "descriptionPlain",
            "description",
            "jobDescription",
        )
        for text in texts(item.get(key))
        for clause in credential_clauses(text)
    ]
    if credential_evidence:
        facts["requirements"] = list(dict.fromkeys(credential_evidence))
    for target, keys in {
        "requirements": ("qualifications", "educationRequirements", "experienceRequirements"),
        "required_licenses": ("requiredLicenses",),
        "applicant_locations": ("applicantLocationRequirements",),
    }.items():
        values = [text for key in keys for text in texts(item.get(key))]
        if values:
            facts.setdefault(target, []).extend(values)
    arrangement = item.get("workplaceType") or item.get("remoteType") or item.get("jobLocationType")
    if not arrangement and item.get("isRemote") is True:
        arrangement = "remote"
    if arrangement:
        facts["work_arrangement"] = str(arrangement)
    for entry in item.get("metadata", []) or []:
        if isinstance(entry, dict):
            name = str(entry.get("name", "")).casefold()
            value = texts(entry.get("value"))
            if name in {"required licenses", "required professional licenses"} and value:
                facts.setdefault("required_licenses", []).extend(value)
            elif name in {"workplace type", "remote type", "work arrangement"} and value:
                facts["work_arrangement"] = " ".join(value)
    for entry in item.get("lists", []) or []:
        if isinstance(entry, dict) and any(
            word in entry.get("text", "").lower() for word in ("qualification", "requirement")
        ):
            facts.setdefault("requirements", []).extend(texts(entry.get("content")))
    return {"eligibility": facts} if facts else {}


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 10_000_000_000 else value
        return datetime.fromtimestamp(seconds, tz=UTC)
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        return None


def _html_text(value: str | None) -> str:
    return BeautifulSoup(value or "", "html.parser").get_text(" ", strip=True)


def _usable_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _workday_detail_unavailable(detail: Any) -> bool:
    """Return true only for explicit boolean Workday unavailable signals."""
    return isinstance(detail, Mapping) and (
        detail.get("posted") is False or detail.get("canApply") is False
    )


def _item_summary(item: Any) -> str:
    if not isinstance(item, dict):
        return repr(item)[:300]
    keys = (
        "id",
        "title",
        "text",
        "name",
        "externalPath",
        "absolute_url",
        "hostedUrl",
        "applyUrl",
        "jobUrl",
        "locationsText",
        "postedOn",
        "bulletFields",
    )
    return repr({key: item[key] for key in keys if key in item})[:500]


def _warn_skipped_item(source: str, company_slug: str, reason: str, item: Any) -> None:
    logger.warning(
        "Skipping malformed %s posting for %s (%s): %s",
        source,
        company_slug,
        reason,
        _item_summary(item),
    )


def _append_raw_job(
    jobs: list[RawJob],
    source: str,
    company_slug: str,
    item: Any,
    **fields: Any,
) -> None:
    try:
        jobs.append(RawJob(**fields))
    except ValidationError:
        _warn_skipped_item(source, company_slug, "invalid fields", item)


class SourceError(RuntimeError):
    pass


class WorkdayRequestError(SourceError):
    """A bounded Workday request retry budget was exhausted."""

    def __init__(self, method: str, url: str, status_code: int | None, reason: str | None = None):
        self.method = method
        self.url = url
        self.status_code = status_code
        detail = f"HTTP {status_code}" if status_code is not None else (reason or "transport error")
        super().__init__(f"Workday {method} {url} failed after retries with {detail}")


class WorkdayLocationValidationError(SourceError):
    """A single Workday detail lacked validated scope evidence."""


class WorkdayRequestController:
    """Small shared limiter/retry policy for concurrent Workday tenants."""

    transient_statuses = frozenset({429, 500, 502, 503, 504})
    max_attempts = 3
    max_retry_after_seconds = 30.0
    base_backoff_seconds = 0.5
    max_backoff_seconds = 8.0

    def __init__(self, concurrency: int = 2, min_interval_seconds: float = 0.15):
        self.semaphore = asyncio.Semaphore(concurrency)
        self.pacing_lock = asyncio.Lock()
        self.min_interval_seconds = min_interval_seconds
        self.next_request_at = 0.0

    @classmethod
    def _retry_after(cls, response: httpx.Response) -> float | None:
        value = response.headers.get("Retry-After")
        if not value:
            return None
        try:
            return min(cls.max_retry_after_seconds, max(0.0, float(value)))
        except ValueError:
            try:
                target = email.utils.parsedate_to_datetime(value)
                delay = target.timestamp() - time.time()
                return min(cls.max_retry_after_seconds, max(0.0, delay))
            except (TypeError, ValueError, OverflowError):
                return None

    @classmethod
    def _backoff(cls, attempt: int) -> float:
        base = min(cls.max_backoff_seconds, cls.base_backoff_seconds * (2**attempt))
        return min(cls.max_backoff_seconds, base + random.uniform(0.0, 0.25))

    async def _pace(self) -> None:
        async with self.pacing_lock:
            now = time.monotonic()
            delay = max(0.0, self.next_request_at - now)
            self.next_request_at = max(now, self.next_request_at) + self.min_interval_seconds
        if delay:
            await asyncio.sleep(delay)

    async def request(self, client: httpx.AsyncClient, method: str, url: str, **kwargs: Any):
        for attempt in range(self.max_attempts):
            await self._pace()
            try:
                # Hold the Workday permit only while the HTTP request is in flight.
                async with self.semaphore:
                    response = await client.request(method, url, **kwargs)
            except httpx.TransportError as exc:
                if attempt + 1 >= self.max_attempts:
                    raise WorkdayRequestError(
                        method, url, None, f"{type(exc).__name__}: {exc}"
                    ) from exc
                await asyncio.sleep(self._backoff(attempt))
                continue
            if response.status_code not in self.transient_statuses:
                response.raise_for_status()
                return response
            delay = self._retry_after(response)
            if attempt + 1 >= self.max_attempts:
                await response.aclose()
                raise WorkdayRequestError(method, url, response.status_code)
            await response.aclose()
            await asyncio.sleep(delay if delay is not None else self._backoff(attempt))
        raise AssertionError("unreachable")


class JobSource(ABC):
    def __init__(self, company: CompanyConfig, client: httpx.AsyncClient):
        self.company = company
        self.client = client

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        retry=retry_if_exception_type((httpx.HTTPError, SourceError)),
        reraise=True,
    )
    async def get_json(self, url: str, **kwargs: Any) -> Any:
        response = await self.client.get(url, **kwargs)
        response.raise_for_status()
        return response.json()

    @abstractmethod
    async def fetch(self) -> list[RawJob]: ...


class GreenhouseSource(JobSource):
    async def fetch(self) -> list[RawJob]:
        token = self.company.ats_config["board_token"]
        url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
        payload = await self.get_json(url)
        jobs: list[RawJob] = []
        for item in payload.get("jobs", []):
            if not isinstance(item, dict):
                _warn_skipped_item("Greenhouse", self.company.slug, "not an object", item)
                continue
            title = item.get("title")
            item_url = item.get("absolute_url")
            if not _usable_text(title):
                _warn_skipped_item("Greenhouse", self.company.slug, "missing title", item)
                continue
            if not _usable_text(item_url):
                _warn_skipped_item("Greenhouse", self.company.slug, "missing URL", item)
                continue
            location = item.get("location")
            location_name = location.get("name", "") if isinstance(location, dict) else ""
            _append_raw_job(
                jobs,
                "Greenhouse",
                self.company.slug,
                item,
                source_company=self.company.slug,
                external_job_id=(str(item.get("id")) if item.get("id") is not None else None),
                title=title,
                location_raw=location_name,
                description_raw=_html_text(item.get("content")),
                posted_at=_parse_datetime(item.get("updated_at")),
                url=item_url,
                metadata={
                    "departments": item.get("departments", []),
                    "active_status_evidence": {"apply_url": item.get("applyUrl")},
                    **_eligibility_metadata(item),
                },
            )
        return jobs


class LeverSource(JobSource):
    async def fetch(self) -> list[RawJob]:
        site = self.company.ats_config["site"]
        payload = await self.get_json(f"https://api.lever.co/v0/postings/{site}?mode=json")
        jobs: list[RawJob] = []
        for item in payload:
            if not isinstance(item, dict):
                _warn_skipped_item("Lever", self.company.slug, "not an object", item)
                continue
            title = item.get("text")
            item_url = item.get("hostedUrl") or item.get("applyUrl")
            if not _usable_text(title):
                _warn_skipped_item("Lever", self.company.slug, "missing title", item)
                continue
            if not _usable_text(item_url):
                _warn_skipped_item("Lever", self.company.slug, "missing URL", item)
                continue
            categories = item.get("categories")
            categories = categories if isinstance(categories, dict) else {}
            _append_raw_job(
                jobs,
                "Lever",
                self.company.slug,
                item,
                source_company=self.company.slug,
                external_job_id=str(item.get("id")) if item.get("id") is not None else None,
                title=title,
                location_raw=categories.get("location", ""),
                description_raw=_html_text(item.get("descriptionPlain") or item.get("description")),
                posted_at=_parse_datetime(item.get("createdAt")),
                url=item_url,
                metadata={
                    "categories": categories,
                    "active_status_evidence": {"apply_url": item.get("applyUrl")},
                    **_eligibility_metadata(item),
                },
            )
        return jobs


class AshbySource(JobSource):
    async def fetch(self) -> list[RawJob]:
        board = self.company.ats_config["board_name"]
        payload = await self.get_json(f"https://api.ashbyhq.com/posting-api/job-board/{board}")
        jobs: list[RawJob] = []
        for item in payload.get("jobs", []):
            if not isinstance(item, dict):
                _warn_skipped_item("Ashby", self.company.slug, "not an object", item)
                continue
            title = item.get("title")
            item_url = item.get("jobUrl") or item.get("applyUrl")
            if not _usable_text(title):
                _warn_skipped_item("Ashby", self.company.slug, "missing title", item)
                continue
            if not _usable_text(item_url):
                _warn_skipped_item("Ashby", self.company.slug, "missing URL", item)
                continue
            _append_raw_job(
                jobs,
                "Ashby",
                self.company.slug,
                item,
                source_company=self.company.slug,
                external_job_id=str(item.get("id") or item_url),
                title=title,
                location_raw=item.get("location", ""),
                description_raw=_html_text(
                    item.get("descriptionHtml") or item.get("descriptionPlain")
                ),
                posted_at=_parse_datetime(item.get("publishedAt")),
                url=item_url,
                metadata={
                    "department": item.get("department"),
                    "active_status_evidence": {"apply_url": item.get("applyUrl")},
                    **_eligibility_metadata(item),
                },
            )
        return jobs


class SmartRecruitersSource(JobSource):
    async def fetch(self) -> list[RawJob]:
        identifier = self.company.ats_config["company_identifier"]
        base = f"https://api.smartrecruiters.com/v1/companies/{identifier}/postings"
        offset = 0
        jobs: list[RawJob] = []
        while True:
            payload = await self.get_json(base, params={"limit": 100, "offset": offset})
            content = payload.get("content", [])
            for item in content:
                if not isinstance(item, dict):
                    _warn_skipped_item("SmartRecruiters", self.company.slug, "not an object", item)
                    continue
                item_id = item.get("id")
                title = item.get("name")
                if item_id is None:
                    _warn_skipped_item("SmartRecruiters", self.company.slug, "missing ID", item)
                    continue
                if not _usable_text(title):
                    _warn_skipped_item("SmartRecruiters", self.company.slug, "missing title", item)
                    continue
                detail = await self.get_json(f"{base}/{item_id}")
                location = item.get("location")
                location = location if isinstance(location, dict) else {}
                sections = (detail.get("jobAd") or {}).get("sections") or {}
                description = " ".join(
                    _html_text(section.get("text"))
                    for section in sections.values()
                    if isinstance(section, dict)
                )
                application_url = detail.get("applyUrl") or detail.get("applicationUrl")
                _append_raw_job(
                    jobs,
                    "SmartRecruiters",
                    self.company.slug,
                    item,
                    source_company=self.company.slug,
                    external_job_id=str(item_id),
                    title=title,
                    location_raw=", ".join(
                        str(location.get(key, ""))
                        for key in ("city", "region", "country")
                        if location.get(key)
                    ),
                    description_raw=description,
                    posted_at=_parse_datetime(item.get("releasedDate")),
                    url=f"https://jobs.smartrecruiters.com/{identifier}/{item_id}",
                    metadata={
                        "active_status_evidence": {"apply_url": application_url},
                        **_eligibility_metadata(detail),
                        "smartrecruiters": {
                            "country_code": str(location.get("country", "")).strip().lower()
                        },
                    },
                )
            offset += len(content)
            if not content or offset >= int(payload.get("totalFound", offset)):
                break
        return jobs


class WorkdaySource(JobSource):
    def __init__(
        self,
        company: CompanyConfig,
        client: httpx.AsyncClient,
        request_controller: WorkdayRequestController | None = None,
    ):
        super().__init__(company, client)
        self.request_controller = request_controller or WorkdayRequestController()
        self.warnings: list[dict[str, str]] = []

    def _record_exclusion(
        self, item: Mapping[str, Any], title: str, path: str, reason: str
    ) -> None:
        self.warnings.append(
            {
                "company": self.company.name,
                "title": str(title),
                "location": str(item.get("locationsText") or ""),
                "reason": reason,
                "url": self.company.ats_config.get("detail_base_url", "").rstrip("/") + path,
            }
        )

    async def _request(self, method: str, url: str, **kwargs: Any):
        return await self.request_controller.request(self.client, method, url, **kwargs)

    async def fetch(self) -> list[RawJob]:
        cfg = self.company.ats_config
        endpoint = cfg["endpoint"]
        site = cfg["site"]
        applied_facets = cfg.get("applied_facets", {})
        if not isinstance(applied_facets, Mapping) or any(
            not isinstance(key, str)
            or not key.strip()
            or not isinstance(values, list)
            or any(not isinstance(value, str) or not value.strip() for value in values)
            for key, values in applied_facets.items()
        ):
            raise SourceError(
                f"Workday applied_facets for {self.company.slug} must map nonempty "
                "string keys to lists of nonempty string IDs"
            )
        applied_facets = dict(applied_facets)
        # Some tenants expose locations but silently ignore country facets.
        # Resolve configured label patterns each run instead of pinning location IDs.
        facet_patterns = cfg.get("facet_patterns", {})
        if not isinstance(facet_patterns, Mapping) or any(
            not isinstance(key, str)
            or not key.strip()
            or not isinstance(pattern, str)
            or not pattern.strip()
            or key in applied_facets
            for key, pattern in facet_patterns.items()
        ):
            raise SourceError(
                "Workday facet_patterns must map distinct facet keys to regex strings"
            )
        try:
            patterns = {key: re.compile(pattern) for key, pattern in facet_patterns.items()}
        except re.error as exc:
            raise SourceError("Invalid Workday facet pattern") from exc
        location_facet_parameter = cfg.get("location_facet_parameter", "locations")
        if not isinstance(location_facet_parameter, str) or not location_facet_parameter.strip():
            raise SourceError("Workday location_facet_parameter must be a nonempty string")
        validate_locations = cfg.get("validate_location_facets", False)
        facet_country = cfg.get("location_facet_country")
        if validate_locations and ("locations" not in patterns or not cfg.get("detail_api_base")):
            raise SourceError(
                "Workday location validation requires locations pattern and detail API"
            )
        if facet_country is not None and (
            not validate_locations or not _usable_text(facet_country)
        ):
            raise SourceError("Workday location_facet_country requires validated location facets")
        if patterns:
            response = await self._request(
                "POST",
                endpoint,
                json={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""},
            )
            payload = response.json()
            resolved = {key: [] for key in patterns}
            resolved_facet_keys: dict[str, str] = {}

            def collect_facets(nodes):
                if not isinstance(nodes, list):
                    return
                for node in nodes:
                    if not isinstance(node, dict):
                        continue
                    key = node.get("facetParameter")
                    pattern_key = (
                        "locations"
                        if key == location_facet_parameter and "locations" in patterns
                        else key
                    )
                    values = node.get("values", [])
                    if pattern_key in patterns and isinstance(values, list):
                        resolved_facet_keys.setdefault(pattern_key, key)
                        for value in values:
                            if (
                                isinstance(value, dict)
                                and isinstance(value.get("descriptor"), str)
                                and _usable_text(value.get("id"))
                                and patterns[pattern_key].search(value["descriptor"])
                            ):
                                resolved[pattern_key].append(value["id"])
                    collect_facets(values)

            collect_facets(payload.get("facets") if isinstance(payload, dict) else None)
            if any(not ids for ids in resolved.values()):
                raise SourceError("Workday facet patterns resolved no IDs; refusing unscoped fetch")
            applied_facets.update(
                {
                    resolved_facet_keys.get(key, key): list(dict.fromkeys(ids))
                    for key, ids in resolved.items()
                }
            )
        limit = int(cfg.get("limit", 20))
        jobs: list[RawJob] = []
        seen: set[str] = set()
        search_texts = cfg.get("search_texts") or [""]
        for search_text in search_texts:
            offset = 0
            reported_total: int | None = None
            while True:
                response = await self._request(
                    "POST",
                    endpoint,
                    json={
                        "appliedFacets": applied_facets,
                        "limit": limit,
                        "offset": offset,
                        "searchText": search_text,
                    },
                )
                payload = response.json()
                if not isinstance(payload, dict) or not isinstance(
                    payload.get("jobPostings"), list
                ):
                    raise SourceError(
                        f"Workday response for {self.company.slug} has no valid jobPostings list"
                    )
                page_total = payload.get("total")
                if isinstance(page_total, int) and page_total > 0:
                    reported_total = page_total
                postings = payload["jobPostings"]
                for item in postings:
                    if not isinstance(item, dict):
                        _warn_skipped_item("Workday", self.company.slug, "not an object", item)
                        continue
                    external_path = item.get("externalPath", "")
                    title = item.get("title")
                    if not _usable_text(title):
                        _warn_skipped_item("Workday", self.company.slug, "missing title", item)
                        continue
                    if not _usable_text(external_path):
                        _warn_skipped_item("Workday", self.company.slug, "missing URL", item)
                        continue
                    if external_path in seen:
                        continue
                    seen.add(external_path)
                    detail_url = cfg.get("detail_base_url", "").rstrip("/") + external_path
                    location_parts = [item.get("locationsText", "")]
                    bullet_fields = item.get("bulletFields") or []
                    if isinstance(bullet_fields, list):
                        description = " ".join(str(value) for value in bullet_fields)
                    else:
                        description = str(bullet_fields)
                    eligibility_metadata = _eligibility_metadata(item)
                    location_metadata = {}
                    active_status_evidence = {}
                    if cfg.get("detail_api_base"):
                        try:
                            detail_response = await self._request(
                                "GET", cfg["detail_api_base"].rstrip("/") + external_path
                            )
                            detail = detail_response.json().get("jobPostingInfo", {})
                            if _workday_detail_unavailable(detail):
                                self._record_exclusion(
                                    item,
                                    title,
                                    external_path,
                                    "detail availability indicates posting is closed or unavailable",
                                )
                                continue
                            apply_action = (detail.get("positionUserActions") or {}).get(
                                "applyAction"
                            )
                            active_status_evidence = {
                                "posted": detail.get("posted"),
                                "canApply": detail.get("canApply"),
                                "apply_url": apply_action.get("applyUrl")
                                if isinstance(apply_action, dict)
                                else None,
                            }
                            if validate_locations:
                                primary = detail.get("location")
                                additional = detail.get("additionalLocations") or []
                                if (
                                    not _usable_text(primary)
                                    or not isinstance(additional, list)
                                    or any(not _usable_text(value) for value in additional)
                                    or not any(
                                        patterns["locations"].search(value)
                                        for value in [primary, *additional]
                                    )
                                ):
                                    raise WorkdayLocationValidationError(
                                        "Workday detail does not confirm scoped location for "
                                        f"{self.company.slug}{external_path}"
                                    )
                                scoped_additional = [
                                    value
                                    for value in additional
                                    if patterns["locations"].search(value)
                                ]
                                primary_country = (detail.get("country") or {}).get("descriptor")
                                if (
                                    facet_country
                                    and not scoped_additional
                                    and primary_country != facet_country
                                ):
                                    raise WorkdayLocationValidationError(
                                        "Workday primary country does not confirm location facet "
                                        f"for {self.company.slug}{external_path}"
                                    )
                                # Country belongs to the primary location, never to an
                                # additional location merely selected by a search facet.
                                location_metadata = {
                                    "workday_locations": {
                                        "primary": primary,
                                        "additional": additional,
                                        "primary_country": detail.get("country"),
                                        "requisition_location": detail.get(
                                            "jobRequisitionLocation"
                                        ),
                                        "listing": item.get("locationsText"),
                                        "facet_country": facet_country,
                                        "scoped_additional": scoped_additional,
                                    }
                                }
                                location_parts = [primary]
                            eligibility_metadata.update(_eligibility_metadata(detail))
                            description = _html_text(detail.get("jobDescription") or description)
                            detail_locations = [
                                detail.get("location"),
                                *(detail.get("additionalLocations") or []),
                                (detail.get("country") or {}).get("descriptor"),
                                (detail.get("jobRequisitionLocation") or {})
                                .get("country", {})
                                .get("alpha2Code"),
                            ]
                            if validate_locations:
                                # Put primary country before alternatives. Annotate only
                                # validated additional labels with their scoped country;
                                # this supplies US-availability evidence without rewriting
                                # a foreign primary country or losing mixed-location facts.
                                detail_locations = [
                                    primary_country,
                                    (detail.get("jobRequisitionLocation") or {})
                                    .get("country", {})
                                    .get("alpha2Code"),
                                    *(
                                        f"{value}, {facet_country}"
                                        if facet_country and value in scoped_additional
                                        else value
                                        for value in additional
                                    ),
                                ]
                            for location in detail_locations:
                                if location and location.casefold() not in {
                                    value.casefold() for value in location_parts if value
                                }:
                                    location_parts.append(location)
                        except WorkdayRequestError as exc:
                            if validate_locations:
                                self._record_exclusion(
                                    item,
                                    title,
                                    external_path,
                                    "detail request failed after retries",
                                )
                                logger.error(
                                    "Excluding Workday posting after detail request failure "
                                    "for %s%s: %s",
                                    self.company.slug,
                                    external_path,
                                    exc,
                                )
                                continue
                            logger.warning(
                                "Unable to enrich Workday detail for %s%s; using listing fields: %s",
                                self.company.slug,
                                external_path,
                                exc,
                            )
                        except WorkdayLocationValidationError as exc:
                            if validate_locations:
                                self._record_exclusion(
                                    item, title, external_path, "location validation failed"
                                )
                                logger.error(
                                    "Excluding Workday posting after location validation failure "
                                    "for %s%s: %s",
                                    self.company.slug,
                                    external_path,
                                    exc,
                                )
                                continue
                            raise
                        except httpx.HTTPStatusError as exc:
                            if validate_locations:
                                self._record_exclusion(
                                    item, title, external_path, "detail HTTP failure"
                                )
                                logger.error(
                                    "Excluding Workday posting after detail HTTP failure "
                                    "for %s%s: %s",
                                    self.company.slug,
                                    external_path,
                                    exc,
                                )
                                continue
                            logger.warning(
                                "Unable to enrich Workday detail for %s%s; using listing fields: %s",
                                self.company.slug,
                                external_path,
                                exc,
                            )
                        except (httpx.HTTPError, ValueError, TypeError, AttributeError) as exc:
                            if validate_locations:
                                self._record_exclusion(
                                    item,
                                    title,
                                    external_path,
                                    "detail evidence malformed or unavailable",
                                )
                                logger.error(
                                    "Excluding Workday posting after detail validation failure "
                                    "for %s%s: %s",
                                    self.company.slug,
                                    external_path,
                                    exc,
                                )
                                continue
                            logger.warning(
                                "Unable to enrich Workday detail for %s%s; using listing fields",
                                self.company.slug,
                                external_path,
                                exc,
                            )
                    _append_raw_job(
                        jobs,
                        "Workday",
                        self.company.slug,
                        item,
                        source_company=self.company.slug,
                        external_job_id=external_path,
                        title=title,
                        location_raw="; ".join(str(value) for value in location_parts if value),
                        description_raw=description,
                        posted_at=_parse_datetime(item.get("postedOn")),
                        url=detail_url or f"https://{site}{external_path}",
                        metadata={
                            "workday": item,
                            "active_status_evidence": active_status_evidence,
                            **location_metadata,
                            **eligibility_metadata,
                        },
                    )
                offset += len(postings)
                if not postings:
                    break
                if reported_total is not None and offset >= reported_total:
                    break
                if len(postings) < limit:
                    break
        return jobs


class TalemetrySource(JobSource):
    async def fetch(self) -> list[RawJob]:
        cfg = self.company.ats_config
        endpoint = cfg["endpoint"]
        page = 1
        jobs: list[RawJob] = []
        while True:
            payload = await self.get_json(endpoint, params={"page": page})
            entries = payload.get("entries", []) if isinstance(payload, dict) else []
            if not isinstance(entries, list):
                raise SourceError(f"Talemetry response for {self.company.slug} has no entries list")
            for item in entries:
                if not isinstance(item, dict):
                    _warn_skipped_item("Talemetry", self.company.slug, "not an object", item)
                    continue
                location = item.get("location") if isinstance(item.get("location"), dict) else {}
                if str(location.get("country", "")).casefold() != "united states":
                    continue
                title = item.get("title")
                item_id = item.get("id") or item.get("talemetry_job_id")
                if not _usable_text(title) or item_id is None:
                    _warn_skipped_item("Talemetry", self.company.slug, "missing title or ID", item)
                    continue
                detail_url = cfg["detail_base_url"].rstrip("/") + f"/{item_id}.json"
                detail_response = await self.client.get(detail_url)
                detail_response.raise_for_status()
                detail_soup = BeautifulSoup(detail_response.text, "html.parser")
                description = detail_soup.select_one(".job-details__content-description")
                canonical = detail_soup.select_one('link[rel="canonical"]')
                _append_raw_job(
                    jobs,
                    "Talemetry",
                    self.company.slug,
                    item,
                    source_company=self.company.slug,
                    external_job_id=str(item_id),
                    title=title,
                    location_raw=", ".join(
                        str(location.get(key, ""))
                        for key in ("locality", "region_abbr", "country")
                        if location.get(key)
                    ),
                    description_raw=_html_text(str(description) if description else ""),
                    posted_at=None,
                    url=(urljoin(detail_url, canonical.get("href")) if canonical else detail_url),
                    metadata={"talemetry": item},
                )
            if not entries or len(entries) < int(payload.get("per_page", len(entries))):
                break
            page += 1
        return jobs


class JibeSource(JobSource):
    async def fetch(self) -> list[RawJob]:
        cfg = self.company.ats_config
        limit = int(cfg.get("limit", 100))
        static_params = cfg.get("query_params", {})
        if not isinstance(static_params, Mapping):
            raise SourceError(f"Jibe query_params for {self.company.slug} must be a mapping")
        page = 1
        jobs: list[RawJob] = []
        while True:
            params = dict(static_params)
            # Pagination is adapter-owned; static configuration cannot override it.
            params.update(page=page, limit=limit)
            payload = await self.get_json(cfg["endpoint"], params=params)
            entries = payload.get("jobs", []) if isinstance(payload, dict) else []
            if not isinstance(entries, list):
                raise SourceError(f"Jibe response for {self.company.slug} has no jobs list")
            for wrapper in entries:
                item = wrapper.get("data") if isinstance(wrapper, dict) else None
                if not isinstance(item, dict):
                    _warn_skipped_item("Jibe", self.company.slug, "missing data object", wrapper)
                    continue
                if str(item.get("country_code", "")).upper() != "US":
                    continue
                title = item.get("title")
                item_id = item.get("req_id") or item.get("slug")
                if not _usable_text(title) or not _usable_text(item_id):
                    _warn_skipped_item("Jibe", self.company.slug, "missing title or ID", item)
                    continue
                location = item.get("full_location") or item.get("location_name") or ""
                _append_raw_job(
                    jobs,
                    "Jibe",
                    self.company.slug,
                    item,
                    source_company=self.company.slug,
                    external_job_id=str(item_id),
                    title=title,
                    location_raw=str(location),
                    description_raw=_html_text(item.get("description")),
                    posted_at=_parse_datetime(item.get("posted_date")),
                    url=item.get("apply_url") or f"{self.company.careers_url}/jobs/{item_id}",
                    metadata={
                        "jibe": item,
                        "active_status_evidence": {"apply_url": item.get("apply_url")},
                        **_eligibility_metadata(item),
                    },
                )
            total = payload.get("totalCount") if isinstance(payload, dict) else None
            if (
                not entries
                or (isinstance(total, int) and page * limit >= total)
                or len(entries) < limit
            ):
                break
            page += 1
        return jobs


class JsonLdSource(JobSource):
    async def fetch(self) -> list[RawJob]:
        response = await self.client.get(str(self.company.careers_url))
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        found: list[dict[str, Any]] = []
        for node in soup.select('script[type="application/ld+json"]'):
            try:
                value = json.loads(node.string or "null")
            except json.JSONDecodeError:
                continue
            candidates = value if isinstance(value, list) else [value]
            for candidate in candidates:
                if isinstance(candidate, dict) and candidate.get("@type") == "JobPosting":
                    found.append(candidate)
                elif isinstance(candidate, dict) and isinstance(candidate.get("@graph"), list):
                    found.extend(
                        x
                        for x in candidate["@graph"]
                        if isinstance(x, dict) and x.get("@type") == "JobPosting"
                    )
        jobs = []
        for item in found:
            title = item.get("title")
            if not _usable_text(title):
                _warn_skipped_item("JSON-LD", self.company.slug, "missing title", item)
                continue
            location = item.get("jobLocation") or item.get("applicantLocationRequirements") or ""
            if isinstance(location, (dict, list)):
                location = json.dumps(location, ensure_ascii=False)
            identifier = item.get("identifier")
            identifier_value = identifier.get("value") if isinstance(identifier, dict) else None
            _append_raw_job(
                jobs,
                "JSON-LD",
                self.company.slug,
                item,
                source_company=self.company.slug,
                external_job_id=str(identifier_value or item.get("url", "")),
                title=title,
                location_raw=str(location),
                description_raw=_html_text(item.get("description")),
                posted_at=_parse_datetime(item.get("datePosted")),
                url=item.get("url") or str(self.company.careers_url),
                metadata={"jsonld": item, **_eligibility_metadata(item)},
            )
        return jobs


class EightfoldSource(JobSource):
    """Generic public Eightfold CareerHub / PCS-X source."""

    def __init__(self, company: CompanyConfig, client: httpx.AsyncClient):
        super().__init__(company, client)
        self.warnings: list[dict[str, str]] = []

    async def fetch(self) -> list[RawJob]:
        cfg = self.company.ats_config
        endpoint = cfg["search_endpoint"]
        detail_endpoint = cfg["detail_endpoint"].rstrip("/")
        domain = cfg["domain"]
        limit = min(int(cfg.get("limit", 10)), 10)
        if limit <= 0:
            raise SourceError("Eightfold page limit must be positive")
        location = cfg.get("location", "")
        query = cfg.get("query", "")
        public_template = cfg["public_job_url_template"]
        jobs: list[RawJob] = []
        seen: set[str] = set()
        start = 0
        reported_total: int | None = None
        while True:
            payload = await self.get_json(
                endpoint,
                params={
                    "domain": domain,
                    "start": start,
                    "num": limit,
                    "query": query,
                    "location": location,
                },
            )
            data = payload.get("data") if isinstance(payload, dict) else None
            positions = data.get("positions") if isinstance(data, dict) else None
            total = data.get("count") if isinstance(data, dict) else None
            if not isinstance(positions, list):
                raise SourceError(
                    f"Eightfold response for {self.company.slug} has no valid positions list"
                )
            if isinstance(total, int) and total >= 0:
                reported_total = total
            if not positions:
                break
            for item in positions:
                if not isinstance(item, dict):
                    _warn_skipped_item("Eightfold", self.company.slug, "not an object", item)
                    continue
                position_id = item.get("id")
                title = item.get("name")
                if position_id is None or not str(position_id).isdigit() or not _usable_text(title):
                    _warn_skipped_item("Eightfold", self.company.slug, "missing ID or title", item)
                    continue
                key = str(position_id)
                if key in seen:
                    continue
                seen.add(key)
                public_url = public_template.format(position_id=key)
                location_values = item.get("locations")
                if isinstance(location_values, list):
                    listing_locations = [
                        str(value) for value in location_values if value is not None
                    ]
                elif location_values is None:
                    listing_locations = []
                else:
                    listing_locations = [str(location_values)]
                warning = {
                    "company": self.company.name,
                    "title": str(title),
                    "location": "; ".join(listing_locations),
                    "reason": "detail enrichment failed",
                    "url": public_url,
                }
                try:
                    detail_payload = await self.get_json(
                        detail_endpoint,
                        params={"domain": domain, "position_id": key},
                    )
                    detail = (
                        detail_payload.get("data") if isinstance(detail_payload, dict) else None
                    )
                    if not isinstance(detail, dict) or not _usable_text(
                        detail.get("jobDescription")
                    ):
                        raise ValueError("missing detail data or job description")
                except (httpx.HTTPError, SourceError, TypeError, ValueError) as exc:
                    warning["reason"] = f"detail enrichment failed: {type(exc).__name__}"
                    self.warnings.append(warning)
                    logger.error(
                        "Excluding Eightfold posting after detail failure for %s/%s: %s",
                        self.company.slug,
                        key,
                        exc,
                    )
                    continue
                detail_title = detail.get("name") or title
                detail_locations = detail.get("locations")
                if isinstance(detail_locations, list):
                    locations = [str(value) for value in detail_locations if value is not None]
                elif detail_locations is None:
                    locations = listing_locations
                else:
                    locations = [str(detail_locations)]
                requisition_id = (
                    detail.get("atsJobId") or detail.get("displayJobId") or item.get("atsJobId")
                )
                work_location = detail.get("workLocationOption") or item.get("workLocationOption")
                flexibility = detail.get("locationFlexibility") or item.get("locationFlexibility")
                apply_action = (detail.get("positionUserActions") or {}).get("applyAction")
                application_url = (
                    apply_action.get("applyUrl") if isinstance(apply_action, dict) else None
                )
                metadata = {
                    "eightfold": {
                        "position_id": detail.get("id", position_id),
                        "display_job_id": detail.get("displayJobId") or item.get("displayJobId"),
                        "ats_job_id": detail.get("atsJobId") or item.get("atsJobId"),
                        "requisition_id": requisition_id,
                        "locations": locations,
                        "standardized_locations": detail.get("standardizedLocations")
                        or item.get("standardizedLocations")
                        or [],
                        "work_location_option": work_location,
                        "location_flexibility": flexibility,
                        "application_url": application_url,
                        "listing": item,
                        "detail": detail,
                    },
                    **_eligibility_metadata(detail),
                }
                _append_raw_job(
                    jobs,
                    "Eightfold",
                    self.company.slug,
                    item,
                    source_company=self.company.slug,
                    external_job_id=key,
                    title=str(detail_title),
                    location_raw="; ".join(locations),
                    description_raw=_html_text(detail.get("jobDescription")),
                    posted_at=_parse_datetime(detail.get("postedTs") or item.get("postedTs")),
                    url=public_url,
                    metadata=metadata,
                )
            start += len(positions)
            if reported_total is not None:
                if start >= reported_total:
                    break
            elif len(positions) < limit:
                break
        return jobs


class SuccessFactorsSource(JobSource):
    """Public SAP SuccessFactors career pages with HTML search and detail pages."""

    _job_path = re.compile(r"/job/[^/?#]+/(\d+)(?:/|$)", re.I)
    _date_formats = ("%b %d, %Y", "%B %d, %Y", "%m/%d/%y", "%m/%d/%Y")

    @staticmethod
    def _posted_date(value: str) -> datetime | None:
        value = value.strip()
        for date_format in SuccessFactorsSource._date_formats:
            try:
                return datetime.strptime(value, date_format).replace(tzinfo=UTC)
            except ValueError:
                continue
        return None

    @classmethod
    def _listing_items(cls, content: str, base_url: str) -> list[dict[str, Any]]:
        soup = BeautifulSoup(content, "html.parser")
        found: dict[str, dict[str, Any]] = {}
        for anchor in soup.select("a[href]"):
            href = urljoin(base_url, anchor.get("href", ""))
            path = urlsplit(href).path
            match = cls._job_path.search(path)
            title = anchor.get_text(" ", strip=True)
            if not match or not title:
                continue
            job_id = match.group(1)
            row = anchor.find_parent("tr") or anchor.find_parent("li") or anchor.parent
            row_text = row.get_text(" | ", strip=True) if row else title
            cells = [cell.get_text(" ", strip=True) for cell in row.select("td, th")] if row else []
            if cells:
                # Career-site result tables expose Title, Location, and Date columns.
                title_index = next((i for i, cell in enumerate(cells) if title in cell), 0)
                remainder = [cell for i, cell in enumerate(cells) if i != title_index and cell]
                location = remainder[0] if remainder else ""
                date_text = next((cell for cell in remainder if cls._posted_date(cell)), "")
                if date_text == location and len(remainder) > 1:
                    location = remainder[1]
            else:
                without_title = row_text.replace(title, "", 1).strip(" |")
                date_match = re.search(
                    r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},\s+\d{4}\b|\b\d{1,2}/\d{1,2}/\d{2,4}\b",
                    without_title,
                    re.I,
                )
                date_text = date_match.group(0) if date_match else ""
                location = without_title.replace(date_text, "", 1).strip(" |")
            found.setdefault(
                job_id,
                {
                    "id": job_id,
                    "title": title,
                    "location": location,
                    "posted_at": cls._posted_date(date_text),
                    "url": href,
                },
            )
        return list(found.values())

    @staticmethod
    def _next_startrow(content: str, base_url: str, current: int) -> int | None:
        offsets = []
        for anchor in BeautifulSoup(content, "html.parser").select("a[href]"):
            query = dict(parse_qsl(urlsplit(urljoin(base_url, anchor["href"])).query))
            try:
                offset = int(query.get("startrow", "0"))
            except ValueError:
                continue
            if offset > current:
                offsets.append(offset)
        return min(offsets) if offsets else None

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        retry=retry_if_exception_type((httpx.HTTPError, SourceError)),
        reraise=True,
    )
    async def _get_html(self, url: str, **kwargs: Any) -> str:
        response = await self.client.get(url, timeout=20, follow_redirects=True, **kwargs)
        response.raise_for_status()
        return response.text

    async def fetch(self) -> list[RawJob]:
        endpoint = str(self.company.ats_config["search_endpoint"])
        page_size = int(self.company.ats_config.get("page_size", 25))
        base_query = dict(parse_qsl(urlsplit(endpoint).query))
        listing_base = urlsplit(endpoint)._replace(query="").geturl()
        listings: dict[str, dict[str, Any]] = {}
        startrow = 0
        while True:
            page_url = listing_base
            page = await self._get_html(
                page_url,
                params={**base_query, "startrow": startrow},
            )
            page_items = self._listing_items(page, listing_base)
            if not page_items:
                if startrow == 0:
                    logger.info("SuccessFactors returned no listings for %s", self.company.slug)
                break
            new_ids = 0
            for item in page_items:
                if item["id"] not in listings:
                    listings[item["id"]] = item
                    new_ids += 1
            if not new_ids:
                break
            next_startrow = self._next_startrow(page, listing_base, startrow)
            if next_startrow is not None:
                startrow = next_startrow
            elif len(page_items) >= page_size:
                startrow += page_size
            else:
                break

        jobs: list[RawJob] = []
        for item in listings.values():
            try:
                detail_html = await self._get_html(item["url"])
            except (httpx.HTTPError, SourceError) as exc:
                logger.warning(
                    "Skipping unavailable SuccessFactors detail for %s job %s: %s",
                    self.company.slug,
                    item["id"],
                    exc,
                )
                continue
            soup = BeautifulSoup(detail_html, "html.parser")
            title_node = soup.select_one("h1")
            title = title_node.get_text(" ", strip=True) if title_node else item["title"]
            description_node = soup.select_one(
                "#jobDescription, .jobDescription, .job-description, [class*='jobDescription']"
            ) or soup.select_one("main")
            if description_node is None:
                description_node = soup.body or soup
            description = description_node.get_text(" ", strip=True)
            apply_url = None
            for anchor in soup.select("a[href]"):
                label = anchor.get_text(" ", strip=True).strip(" »›")
                target = anchor.get("href", "").strip()
                href = urljoin(item["url"], target)
                if (
                    re.match(r"^apply(?:\s|$)", label, re.I)
                    and target
                    and not target.startswith("#")
                    and urlsplit(href).scheme in {"http", "https"}
                ):
                    if (
                        anchor.has_attr("disabled")
                        or anchor.get("aria-disabled", "").casefold() == "true"
                    ):
                        continue
                    apply_url = href
                    break
            status = page_status(detail_html)
            if apply_url and status is not ActiveStatus.INACTIVE:
                status = ActiveStatus.ACTIVE
            metadata = {
                "successfactors": {"requisition_id": item["id"], "apply_url": apply_url},
                "active_status": status.value,
                "active_status_page_checked": True,
                "active_status_evidence": {"apply_url": apply_url} if apply_url else {},
            }
            _append_raw_job(
                jobs,
                "SuccessFactors",
                self.company.slug,
                item,
                source_company=self.company.slug,
                external_job_id=item["id"],
                title=title or item["title"],
                location_raw=item["location"],
                description_raw=description,
                posted_at=item["posted_at"],
                url=item["url"],
                metadata=metadata,
            )
        return jobs


class TeamtailorSource(JobSource):
    """Public Teamtailor career sites rendered as ordinary HTML pages."""

    _job_path = re.compile(r"/jobs/(\d+)-([^/?#]+)(?:/|$)", re.I)
    _apply_labels = {"apply for this job", "apply now", "apply"}

    @classmethod
    def _listing_items(cls, content: str, base_url: str) -> list[dict[str, Any]]:
        soup = BeautifulSoup(content, "html.parser")
        items: dict[str, dict[str, Any]] = {}
        for anchor in soup.select("a[href]"):
            detail_url = urljoin(base_url, anchor["href"])
            match = cls._job_path.search(urlsplit(detail_url).path)
            title = anchor.get_text(" ", strip=True)
            if not match or not title:
                continue
            job_id = match.group(1)
            card = anchor.find_parent("li") or anchor.parent
            card_text = card.get_text(" ", strip=True) if card else title
            remainder = re.sub(re.escape(title), "", card_text, count=1, flags=re.I).strip(
                " ·|•-\t"
            )
            parts = [
                part.strip(" ·|•\t")
                for part in re.split(r"\s*[·|•]\s*", remainder)
                if part.strip(" ·|•\t")
            ]
            arrangement = next(
                (
                    part
                    for part in parts
                    if re.fullmatch(r"fully remote|remote|hybrid|on[ -]?site", part, re.I)
                ),
                "",
            )
            department = parts[0] if parts else ""
            location = parts[1] if len(parts) > 1 else ""
            if arrangement and parts and parts[-1].casefold() == arrangement.casefold():
                if len(parts) > 2:
                    location = parts[-2]
                if len(parts) > 2:
                    department = parts[0]
            items.setdefault(
                job_id,
                {
                    "id": job_id,
                    "title": title,
                    "url": detail_url,
                    "department": department,
                    "location": location,
                    "remote_status": arrangement,
                },
            )
        return list(items.values())

    @staticmethod
    def _next_listing_url(content: str, current_url: str) -> str | None:
        soup = BeautifulSoup(content, "html.parser")
        for anchor in soup.select("a[rel~='next'][href], a[href]"):
            label = anchor.get_text(" ", strip=True).casefold()
            if "next" not in label and "next" not in anchor.get("rel", []):
                continue
            candidate = urljoin(current_url, anchor["href"])
            if candidate != current_url:
                return candidate
        return None

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        retry=retry_if_exception_type((httpx.HTTPError, SourceError)),
        reraise=True,
    )
    async def _get_html(self, url: str) -> str:
        response = await self.client.get(url, timeout=20, follow_redirects=True)
        response.raise_for_status()
        return response.text

    @staticmethod
    def _apply_evidence(content: str, detail_url: str) -> tuple[ActiveStatus, dict[str, Any]]:
        status = page_status(content)
        if status is ActiveStatus.INACTIVE:
            return status, {}
        soup = BeautifulSoup(content, "html.parser")
        for anchor in soup.select("a[href]"):
            label = re.sub(r"\s+", " ", anchor.get_text(" ", strip=True)).casefold()
            target = anchor.get("href", "").strip()
            apply_url = urljoin(detail_url, target)
            if (
                label in TeamtailorSource._apply_labels
                and target
                and not target.startswith("#")
                and urlsplit(apply_url).scheme in {"http", "https"}
            ):
                return ActiveStatus.ACTIVE, {"apply_url": apply_url}

        # CRC's detail page uses this exact enabled action to reveal its application form.
        for button in soup.select("button"):
            label = re.sub(r"\s+", " ", button.get_text(" ", strip=True)).casefold()
            if label != "apply for this job":
                continue
            if button.has_attr("disabled") or button.get("aria-disabled", "").casefold() == "true":
                return ActiveStatus.INACTIVE, {"disabled_apply_action": True}
            has_form_affordance = bool(
                soup.select_one(
                    "form[action], [data-action*='apply' i], [aria-controls*='application' i], [id*='application' i], [class*='application-form' i]"
                )
                or re.search(r"\bloading application form\b", soup.get_text(" ", strip=True), re.I)
            )
            if has_form_affordance:
                return ActiveStatus.ACTIVE, {
                    "apply_action": {"label": "Apply for this job", "form_affordance": True}
                }
        return status, {}

    async def fetch(self) -> list[RawJob]:
        first_page = str(self.company.ats_config["listing_endpoint"])
        pending = [first_page]
        visited_pages: set[str] = set()
        listings: dict[str, dict[str, Any]] = {}
        while pending:
            page_url = pending.pop(0)
            if page_url in visited_pages:
                continue
            visited_pages.add(page_url)
            page = await self._get_html(page_url)
            for item in self._listing_items(page, page_url):
                listings.setdefault(item["id"], item)
            next_url = self._next_listing_url(page, page_url)
            if next_url and next_url not in visited_pages:
                pending.append(next_url)

        jobs: list[RawJob] = []
        for item in listings.values():
            try:
                detail_html = await self._get_html(item["url"])
            except (httpx.HTTPError, SourceError) as exc:
                logger.warning(
                    "Skipping unavailable Teamtailor detail for %s job %s: %s",
                    self.company.slug,
                    item["id"],
                    exc,
                )
                continue
            soup = BeautifulSoup(detail_html, "html.parser")
            title_node = soup.select_one("h1")
            title = title_node.get_text(" ", strip=True) if title_node else item["title"]
            description_node = soup.select_one(
                "[data-job-description], #job-description, .job-description, .prose"
            ) or soup.select_one("main")
            if description_node is None:
                description_node = soup.body or soup
            description = description_node.get_text(" ", strip=True)
            status, apply_evidence = self._apply_evidence(detail_html, item["url"])
            department = item["department"]
            metadata = {
                "teamtailor": {
                    "job_id": item["id"],
                    "department": department,
                    "remote_status": item["remote_status"],
                    **apply_evidence,
                },
                "active_status": status.value,
                "active_status_page_checked": True,
                "active_status_evidence": apply_evidence,
                "eligibility": {"work_arrangement": item["remote_status"]}
                if item["remote_status"]
                else {},
            }
            _append_raw_job(
                jobs,
                "Teamtailor",
                self.company.slug,
                item,
                source_company=self.company.slug,
                external_job_id=item["id"],
                title=title or item["title"],
                location_raw="; ".join(
                    value for value in (item["location"], item["remote_status"]) if value
                ),
                description_raw=description,
                posted_at=None,
                url=item["url"],
                metadata=metadata,
            )
        return jobs


class CityOfHopeSource(JobSource):
    """Server-rendered City of Hope listings, hydrating only matched candidates."""

    _job_link = re.compile(r"/job/[^?#]+", re.I)
    _remote = re.compile(r"United States\s*\(This is a remote job\)", re.I)
    _job_ref = re.compile(
        r"\b(?:Job\s+Ref(?:erence)?|Req(?:uisition)?(?:\s+ID)?|Reference)\s*[:#]?\s*([A-Z0-9-]+)",
        re.I,
    )

    @staticmethod
    def _card_for(anchor):
        node = anchor
        for _ in range(6):
            if node is None:
                break
            text = node.get_text(" ", strip=True)
            if len(text) > len(anchor.get_text(" ", strip=True)) + 10 and len(text) < 5000:
                return node
            node = node.parent
        return anchor.parent or anchor

    @classmethod
    def _listing_items(cls, content: str, base_url: str) -> list[dict[str, Any]]:
        soup = BeautifulSoup(content, "html.parser")
        items: dict[str, dict[str, Any]] = {}
        for anchor in soup.select("a[href]"):
            detail_url = urljoin(base_url, anchor["href"])
            if not cls._job_link.search(urlsplit(detail_url).path):
                continue
            title = anchor.get_text(" ", strip=True)
            card = cls._card_for(anchor)
            text = card.get_text(" ", strip=True)
            if not title or title.casefold() in {"view job", "apply", "learn more"}:
                heading = card.select_one("h2, h3, h4")
                title = heading.get_text(" ", strip=True) if heading else title
            if not title:
                continue
            ref_match = cls._job_ref.search(text)
            job_ref = ref_match.group(1) if ref_match else ""
            # The visible Job Ref is the public requisition identity. Retain a URL fallback.
            path_id = re.search(r"/(\d+)(?:/|$)", urlsplit(detail_url).path)
            job_id = job_ref or (path_id.group(1) if path_id else detail_url)
            location_node = card.select_one(".job-location, [class*=location i], [data-location]")
            location = location_node.get_text(" ", strip=True) if location_node else ""
            if not location:
                location_match = re.search(
                    r"(?:Location|Locations)\s*:?\s*(.*?)(?=\s+(?:Category|Job Category|Job Type|Shift|Pay Range|Job Ref|Description)\s*:?|$)",
                    text,
                    re.I,
                )
                location = location_match.group(1).strip(" |·") if location_match else ""
            remote = bool(cls._remote.search(text))
            if remote:
                location = re.sub(cls._remote, "United States", location or text).strip()

            def field(*labels: str) -> str:
                pattern = (
                    r"(?:"
                    + "|".join(labels)
                    + r")\s*:?\s*(.*?)(?=\s+(?:Category|Job Category|Job Type|Shift|Pay Range|Compensation|Job Ref|Location|Description)\s*:?|$)"
                )
                match = re.search(pattern, text, re.I)
                return match.group(1).strip(" |·") if match else ""

            excerpt_node = card.select_one(
                ".job-description, .job-excerpt, [class*=description i], p"
            )
            excerpt = excerpt_node.get_text(" ", strip=True) if excerpt_node else text
            category = field("Category", "Job Category")
            job_type = field("Job Type", "Employment Type")
            shift = field("Shift")
            pay_match = re.search(
                r"\$[\d,]+(?:\.\d{2})?\s*(?:-|to)\s*\$?[\d,]+(?:\.\d{2})?(?:\s*/?\s*(?:hr|hour|year|yr))?",
                text,
                re.I,
            )
            pay = (
                pay_match.group(0).strip()
                if pay_match
                else field("Pay Range", "Compensation", "Hourly Pay")
            )
            listing_hash = hashlib.sha256(
                "|".join(
                    (title, job_ref, location, category, job_type, shift, pay, excerpt)
                ).encode()
            ).hexdigest()
            items.setdefault(
                job_id,
                {
                    "id": job_id,
                    "job_ref": job_ref,
                    "title": title,
                    "url": detail_url,
                    "location": location,
                    "remote": remote,
                    "category": category,
                    "job_type": job_type,
                    "shift": shift,
                    "pay_range": pay,
                    "excerpt": excerpt,
                    "listing_hash": listing_hash,
                },
            )
        return list(items.values())

    @staticmethod
    def _next_page_url(endpoint: str, page: int) -> str:
        parts = urlsplit(endpoint)
        query = [(key, value) for key, value in parse_qsl(parts.query) if key != "page_jobs"]
        query.append(("page_jobs", str(page)))
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        retry=retry_if_exception_type(httpx.HTTPError),
        reraise=True,
    )
    async def _get(self, url: str) -> httpx.Response:
        response = await self.client.get(url, timeout=20, follow_redirects=True)
        response.raise_for_status()
        return response

    async def fetch(self) -> list[RawJob]:
        endpoint = str(self.company.ats_config["listing_endpoint"])
        jobs: list[RawJob] = []
        seen: set[str] = set()
        for page_number in range(1, 101):
            url = endpoint if page_number == 1 else self._next_page_url(endpoint, page_number)
            response = await self._get(url)
            items = self._listing_items(response.text, url)
            if not items:
                break
            for item in items:
                if item["id"] in seen:
                    continue
                seen.add(item["id"])
                remote_evidence = "United States (This is a remote job)" if item["remote"] else ""
                metadata = {
                    "city_of_hope": {
                        "job_ref": item["job_ref"],
                        "detail_url": item["url"],
                        "category": item["category"],
                        "job_type": item["job_type"],
                        "shift": item["shift"],
                        "pay_range": item["pay_range"],
                        "listing_excerpt": item["excerpt"],
                        "listing_hash": item["listing_hash"],
                        "remote_status": "remote" if item["remote"] else "",
                    },
                    "eligibility": {"work_arrangement": "remote"} if item["remote"] else {},
                }
                _append_raw_job(
                    jobs,
                    "City of Hope",
                    self.company.slug,
                    item,
                    source_company=self.company.slug,
                    external_job_id=item["id"],
                    title=item["title"],
                    location_raw="; ".join(x for x in (item["location"], remote_evidence) if x),
                    description_raw=item["excerpt"],
                    posted_at=None,
                    url=item["url"],
                    metadata=metadata,
                )
            if len(items) < 20:
                break
        return jobs

    async def hydrate(self, raw: RawJob) -> RawJob:
        city_data = raw.metadata.get("city_of_hope", {})
        detail_url = city_data.get("detail_url")
        if not detail_url or city_data.get("detail_checked"):
            return raw
        metadata = dict(raw.metadata)
        city_data = dict(city_data)
        try:
            response = await self.client.get(detail_url, timeout=20, follow_redirects=True)
        except httpx.HTTPError:
            city_data["detail_checked"] = True
            metadata["city_of_hope"] = city_data
            metadata["active_status"] = ActiveStatus.UNKNOWN.value
            metadata["active_status_page_checked"] = True
            metadata["active_status_evidence"] = {}
            return raw.model_copy(update={"metadata": metadata})
        if response.status_code >= 400:
            city_data["detail_checked"] = True
            metadata["city_of_hope"] = city_data
            metadata["active_status"] = ActiveStatus.UNKNOWN.value
            metadata["active_status_page_checked"] = True
            metadata["active_status_evidence"] = {}
            return raw.model_copy(update={"metadata": metadata})
        soup = BeautifulSoup(response.text, "html.parser")
        description = soup.select_one(".job-description, #job-description, [itemprop=description]")
        if description is None:
            description = soup.select_one("main") or soup.body or soup
        body = description.get_text(" ", strip=True)
        status = page_status(response.text)
        city_data.update({"detail_checked": True, "description": body})
        metadata.update(
            {
                "city_of_hope": city_data,
                "active_status": status.value,
                "active_status_page_checked": True,
                "active_status_evidence": {"official_detail_checked": True},
            }
        )
        return raw.model_copy(
            update={"description_raw": body or raw.description_raw, "metadata": metadata}
        )


class CharterResearchSource(JobSource):
    """Charter's same-domain HTML job table; details are hydrated for candidates only."""

    _job_path = re.compile(r"/careers/job/(\d+)/?$", re.I)

    @classmethod
    def _listing_items(cls, content: str, base_url: str) -> list[dict[str, Any]]:
        soup = BeautifulSoup(content, "html.parser")
        jobs: dict[str, dict[str, Any]] = {}
        for anchor in soup.select("a[href]"):
            detail_url = urljoin(base_url, anchor["href"])
            match = cls._job_path.fullmatch(urlsplit(detail_url).path)
            title = anchor.get_text(" ", strip=True)
            if not match or not title:
                continue
            row = anchor.find_parent("tr")
            cells = row.find_all("td", recursive=False) if row else []
            location = ""
            if cells:
                title_cell = anchor.find_parent("td")
                other_cells = [
                    cell.get_text(" ", strip=True) for cell in cells if cell is not title_cell
                ]
                location = next((value for value in other_cells if value and value != title), "")
            job_id = match.group(1)
            listing_hash = hashlib.sha256(
                "|".join((job_id, title, location, detail_url)).encode()
            ).hexdigest()
            jobs.setdefault(
                job_id,
                {
                    "id": job_id,
                    "title": title,
                    "location": location,
                    "url": detail_url,
                    "listing_hash": listing_hash,
                },
            )
        return list(jobs.values())

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        retry=retry_if_exception_type(httpx.HTTPError),
        reraise=True,
    )
    async def _get(self, url: str) -> httpx.Response:
        response = await self.client.get(url, timeout=20, follow_redirects=True)
        response.raise_for_status()
        return response

    async def fetch(self) -> list[RawJob]:
        endpoint = str(self.company.ats_config["listing_endpoint"])
        response = await self._get(endpoint)
        jobs: list[RawJob] = []
        for item in self._listing_items(response.text, endpoint):
            metadata = {
                "charter_research": {
                    "detail_url": item["url"],
                    "listing_hash": item["listing_hash"],
                }
            }
            _append_raw_job(
                jobs,
                "Charter Research",
                self.company.slug,
                item,
                source_company=self.company.slug,
                external_job_id=item["id"],
                title=item["title"],
                location_raw=item["location"],
                description_raw="",
                posted_at=None,
                url=item["url"],
                metadata=metadata,
            )
        return jobs

    async def hydrate(self, raw: RawJob) -> RawJob:
        charter_data = raw.metadata.get("charter_research", {})
        detail_url = charter_data.get("detail_url")
        if not detail_url or charter_data.get("detail_checked"):
            return raw
        metadata = dict(raw.metadata)
        charter_data = dict(charter_data)
        try:
            response = await self.client.get(detail_url, timeout=20, follow_redirects=True)
        except httpx.HTTPError:
            charter_data["detail_checked"] = True
            metadata.update(
                {
                    "charter_research": charter_data,
                    "active_status": ActiveStatus.UNKNOWN.value,
                    "active_status_page_checked": True,
                    "active_status_evidence": {},
                }
            )
            return raw.model_copy(update={"metadata": metadata})

        if response.status_code in {404, 410}:
            status = ActiveStatus.INACTIVE
            body = raw.description_raw
        elif not response.is_success:
            status = ActiveStatus.UNKNOWN
            body = raw.description_raw
        else:
            soup = BeautifulSoup(response.text, "html.parser")
            description = soup.select_one(
                ".job-description, #job-description, [itemprop=description]"
            )
            if description is None:
                description = soup.select_one("main") or soup.body or soup
            body = description.get_text(" ", strip=True)
            status = page_status(response.text)
        charter_data.update({"detail_checked": True, "description": body})
        metadata.update(
            {
                "charter_research": charter_data,
                "active_status": status.value,
                "active_status_page_checked": True,
                "active_status_evidence": {"official_detail_checked": True},
            }
        )
        return raw.model_copy(update={"description_raw": body, "metadata": metadata})


class TalentBrewSource(JobSource):
    """Public TalentBrew/Radancy inventory with a configured company facet."""

    _job_path = re.compile(r"/job/[^?#]+", re.I)
    _req = re.compile(
        r"\b(?:requisition|job\s*(?:id|reference|ref)|req(?:uisition)?\s*(?:id|number|#)?)\s*[:#]?\s*(R-?\d{5,}|\d{5,})\b",
        re.I,
    )
    _date_formats = (
        "%m/%d/%Y",
        "%m/%d/%y",
        "%b %d, %Y",
        "%B %d, %Y",
        "%Y-%m-%d",
        "%d %b %Y",
        "%d %B %Y",
    )

    @classmethod
    def _parse_date(cls, text: str | None) -> datetime | None:
        if not text:
            return None
        value = text.strip()
        iso = _parse_datetime(value)
        if iso:
            return iso
        for fmt in cls._date_formats:
            try:
                return datetime.strptime(value, fmt).replace(tzinfo=UTC)
            except ValueError:
                continue
        return None

    @classmethod
    def _card(cls, anchor):
        node = anchor
        for _ in range(7):
            if node is None:
                break
            classes = " ".join(node.get("class", []))
            if node.name in {"li", "article"} or re.search(r"job|result|posting", classes, re.I):
                return node
            node = node.parent
        return anchor.parent or anchor

    @classmethod
    def _parse_listings(
        cls, content: str, base_url: str, company_name: str, company_filter: str = ""
    ) -> list[dict[str, Any]]:
        soup = BeautifulSoup(content, "html.parser")
        found: dict[str, dict[str, Any]] = {}
        for anchor in soup.select("a[href]"):
            url = urljoin(base_url, anchor["href"])
            if not cls._job_path.search(urlsplit(url).path):
                continue
            card = cls._card(anchor)
            title = anchor.get_text(" ", strip=True)
            if not title or title.casefold() in {"view job", "read more", "apply"}:
                heading = card.select_one("h1, h2, h3, h4")
                title = heading.get_text(" ", strip=True) if heading else title
            if not title:
                continue
            text = card.get_text(" ", strip=True)
            company_node = card.select_one("[class*=company i], [data-company]")
            company = company_node.get_text(" ", strip=True) if company_node else ""
            if company and company_filter and company_filter.casefold() not in company.casefold():
                continue
            ref_match = cls._req.search(text)
            requisition = ref_match.group(1).upper() if ref_match else ""
            if requisition and not requisition.startswith("R-"):
                requisition = f"R-{requisition.lstrip('R-')}"
            if not requisition:
                requisition = re.search(r"\b(R-?\d{5,})\b", url + " " + text, re.I)
                requisition = requisition.group(1).upper() if requisition else ""
                if requisition and not requisition.startswith("R-"):
                    requisition = "R-" + requisition.removeprefix("R")
            if not requisition:
                # Do not merge unidentified postings by slug; use URL as the last identity fallback.
                requisition = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
            location_node = card.select_one("[class*=location i], [data-location], .job-location")
            location = location_node.get_text(" ", strip=True) if location_node else ""
            if not location:
                match = re.search(
                    r"\bLocation\s*:?\s*(.*?)(?=\s+(?:Company|Category|Date Posted|Requisition|Job ID)\s*:?|$)",
                    text,
                    re.I,
                )
                location = match.group(1).strip() if match else ""
            posted_match = re.search(
                r"(?:Date Posted|Posted)\s*:?\s*([^|·]+?)(?=\s+(?:Closing Date|Company|Category|Location)\s*:?|$)",
                text,
                re.I,
            )
            category_node = card.select_one("[class*=category i], [class*=department i]")
            category = category_node.get_text(" ", strip=True) if category_node else ""
            arrangement = cls._arrangement(location)
            found.setdefault(
                requisition,
                {
                    "id": requisition,
                    "title": title,
                    "url": url,
                    "location": location,
                    "company": company or company_name,
                    "category": category,
                    "posted_at": cls._parse_date(posted_match.group(1) if posted_match else None),
                    "arrangement": arrangement,
                    "listing_text": text,
                },
            )
        return list(found.values())

    @staticmethod
    def _arrangement(text: str) -> str:
        if re.search(r"\bremote\b", text, re.I):
            return "Remote"
        if re.search(r"\bhybrid\b", text, re.I):
            return "Hybrid"
        if re.search(r"\b(?:onsite|on-site|office based)\b", text, re.I):
            return "Onsite"
        return ""

    @staticmethod
    def _query_url(endpoint: str, params: dict[str, Any]) -> str:
        parts = urlsplit(endpoint)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query.update({str(key): str(value) for key, value in params.items() if value is not None})
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        retry=retry_if_exception_type(httpx.HTTPError),
        reraise=True,
    )
    async def _get(self, url: str) -> str:
        response = await self.client.get(url, timeout=20, follow_redirects=True)
        response.raise_for_status()
        return response.text

    @staticmethod
    def _detail_fields(content: str, detail_url: str, company_name: str) -> dict[str, Any]:
        soup = BeautifulSoup(content, "html.parser")
        text = soup.get_text(" ", strip=True)
        title_node = soup.select_one("h1, [itemprop=title]")
        title = title_node.get_text(" ", strip=True) if title_node else ""
        ref_match = TalentBrewSource._req.search(text)
        requisition = ref_match.group(1).upper() if ref_match else ""
        if requisition and not requisition.startswith("R-"):
            requisition = "R-" + requisition.removeprefix("R")
        canonical = soup.select_one("link[rel=canonical][href]")
        url = urljoin(detail_url, canonical.get("href")) if canonical else detail_url
        description_node = soup.select_one(
            "[itemprop=description], .job-description, #job-description, .job-details"
        )
        if description_node is None:
            description_node = soup.select_one("main") or soup.body or soup
        location_node = soup.select_one(
            "[itemprop=jobLocation], [class*=location i], [data-location]"
        )
        location = location_node.get_text(" ", strip=True) if location_node else ""

        def labeled(label: str) -> str:
            match = re.search(
                rf"\b{label}\s*:?\s*(.*?)(?=\s+(?:Date Posted|Closing Date|Location|Company|Category|Requisition|Job ID|Salary|Compensation)\s*:?|$)",
                text,
                re.I,
            )
            return match.group(1).strip() if match else ""

        posted = TalentBrewSource._parse_date(
            labeled("Date Posted") or labeled("Posting Date") or labeled("Posted")
        )
        closing = TalentBrewSource._parse_date(
            labeled("Closing Date") or labeled("Apply By") or labeled("Application Deadline")
        )
        company_node = soup.select_one("[class*=company i], [data-company]")
        company = company_node.get_text(" ", strip=True) if company_node else company_name
        category_node = soup.select_one("[class*=category i], [class*=department i]")
        category = category_node.get_text(" ", strip=True) if category_node else labeled("Category")
        salary = labeled("Salary") or labeled("Compensation")
        arrangement = TalentBrewSource._arrangement(location)
        apply_action = None
        for node in soup.select("a[href], button, input[type=submit]"):
            label = " ".join(
                (node.get_text(" ", strip=True), node.get("aria-label", ""), node.get("value", ""))
            ).strip()
            if re.fullmatch(r"apply(?: now)?", label, re.I):
                href = node.get("href", "").strip()
                if href and href != "#" and not href.casefold().startswith("javascript:"):
                    apply_action = urljoin(detail_url, href)
                elif node.name in {"button", "input"} and not node.has_attr("disabled"):
                    apply_action = "enabled-action"
                if apply_action:
                    break
        page_status_value = page_status(content)
        if closing and closing.date() < datetime.now(UTC).date():
            status = ActiveStatus.INACTIVE
        elif page_status_value is ActiveStatus.INACTIVE:
            status = ActiveStatus.INACTIVE
        elif apply_action and page_status_value is ActiveStatus.ACTIVE:
            status = ActiveStatus.ACTIVE
        else:
            status = ActiveStatus.UNKNOWN
        return {
            "title": title,
            "requisition_id": requisition,
            "url": url,
            "location": location,
            "company": company,
            "category": category,
            "description": description_node.get_text(" ", strip=True),
            "posted_at": posted,
            "closing_date": closing,
            "salary": salary,
            "arrangement": arrangement,
            "apply_action": apply_action,
            "active_status": status,
        }

    async def fetch(self) -> list[RawJob]:
        config = self.company.ats_config
        endpoint = str(config["listing_endpoint"])
        filter_config = config["company_filter"]
        params = {filter_config["parameter"]: filter_config["value"]}
        params.update(config.get("search_params", {}))
        page_parameter = config.get("page_parameter", "page")
        page_size = int(config.get("page_size", 15))
        found: dict[str, dict[str, Any]] = {}
        for page_number in range(1, 101):
            page_params = dict(params)
            if page_number > 1:
                page_params[page_parameter] = page_number
            html = await self._get(self._query_url(endpoint, page_params))
            rows = self._parse_listings(
                html,
                endpoint,
                str(config.get("brand_label", self.company.name)),
                str(filter_config["value"]),
            )
            if not rows:
                break
            for row in rows:
                found.setdefault(row["id"], row)
            if len(rows) < page_size:
                break

        jobs_by_requisition: dict[str, RawJob] = {}
        seen_detail_urls: set[str] = set()
        for row in found.values():
            if row["url"] in seen_detail_urls:
                continue
            seen_detail_urls.add(row["url"])
            try:
                detail = await self._get(row["url"])
                fields = self._detail_fields(
                    detail, row["url"], str(config.get("brand_label", self.company.name))
                )
                detail_url = fields["url"]
                title = fields["title"] or row["title"]
                external_id = fields["requisition_id"] or row["id"]
                location = fields["location"] or row["location"]
                arrangement = fields["arrangement"] or row["arrangement"]
                status = fields["active_status"]
                metadata = {
                    "talentbrew": {
                        "requisition_id": external_id,
                        "company": fields["company"] or row["company"],
                        "category": fields["category"] or row["category"],
                        "closing_date": fields["closing_date"].isoformat()
                        if fields["closing_date"]
                        else None,
                        "salary": fields["salary"],
                        "apply_url": fields["apply_action"],
                        "work_arrangement": arrangement,
                    },
                    "active_status": status.value,
                    "active_status_page_checked": True,
                    "active_status_evidence": {"apply_url": fields["apply_action"]}
                    if fields["apply_action"] and status is ActiveStatus.ACTIVE
                    else {},
                    "eligibility": {"work_arrangement": arrangement} if arrangement else {},
                }
                normalized: list[RawJob] = []
                _append_raw_job(
                    normalized,
                    "TalentBrew",
                    self.company.slug,
                    row,
                    source_company=self.company.slug,
                    external_job_id=external_id,
                    title=title,
                    location_raw=location,
                    description_raw=fields["description"] or row["listing_text"],
                    posted_at=fields["posted_at"] or row["posted_at"],
                    url=detail_url,
                    metadata=metadata,
                )
                if normalized:
                    jobs_by_requisition.setdefault(external_id, normalized[0])
            except (httpx.HTTPError, SourceError) as exc:
                logger.warning(
                    "Unavailable TalentBrew detail for %s %s: %s", self.company.slug, row["id"], exc
                )
                external_id = row["id"]
                metadata = {
                    "talentbrew": {
                        "requisition_id": external_id,
                        "company": row["company"],
                        "category": row["category"],
                        "work_arrangement": row["arrangement"],
                    },
                    "active_status": ActiveStatus.UNKNOWN.value,
                    "active_status_page_checked": True,
                    "active_status_evidence": {},
                    "eligibility": {"work_arrangement": row["arrangement"]}
                    if row["arrangement"]
                    else {},
                }
                fallback: list[RawJob] = []
                _append_raw_job(
                    fallback,
                    "TalentBrew",
                    self.company.slug,
                    row,
                    source_company=self.company.slug,
                    external_job_id=external_id,
                    title=row["title"],
                    location_raw=row["location"],
                    description_raw=row["listing_text"],
                    posted_at=row["posted_at"],
                    url=row["url"],
                    metadata=metadata,
                )
                if fallback:
                    jobs_by_requisition.setdefault(external_id, fallback[0])
        return list(jobs_by_requisition.values())


class JobAdderWidgetSource(JobSource):
    """Public JSONP/HTML inventory with candidate-only description hydration."""

    PAGE_SIZE = 6
    CALLBACK = "radar"

    @classmethod
    def _decode(cls, body: str) -> str:
        match = re.fullmatch(r"\s*" + cls.CALLBACK + r"\((.*)\);?\s*", body, re.S)
        if not match:
            raise SourceError("JobAdder: malformed JSONP wrapper")
        try:
            fragment = json.loads(match[1])
        except ValueError as exc:
            raise SourceError("JobAdder: malformed JSONP payload") from exc
        if not isinstance(fragment, str) or not fragment.strip():
            raise SourceError("JobAdder: missing HTML fragment")
        return fragment

    async def _fragment(self, endpoint: str, **params: Any) -> BeautifulSoup:
        response = await self.client.get(
            str(self.company.ats_config[endpoint]),
            params={"key": self.company.ats_config["key"], "callback": self.CALLBACK, **params},
        )
        response.raise_for_status()
        return BeautifulSoup(self._decode(response.text), "html.parser")

    async def _page(self, number: int) -> tuple[list, int]:
        soup = await self._fragment(
            "listing_endpoint",
            pageNumber=number,
            jobsPerPage=self.PAGE_SIZE,
            showHotJobsOnly="false",
            showPagerSummary="true",
            alwaysShowPager="true",
            showDatePosted="true",
            dateFormat="yyyy-MM-dd",
            showClassifications="true",
            titleIsLink="true",
        )
        containers = soup.select(".ja-job-list-container")
        if len(containers) != 1:
            raise SourceError("JobAdder: missing inventory container")
        container = containers[0]
        jobs = container.select(".ja-job-list > .job")
        summaries = container.select(".ja-pager-summary")
        if not jobs:
            empty = container.select_one(".no-jobs-content")
            if empty is None or not empty.get_text(strip=True) or summaries:
                raise SourceError("JobAdder: malformed empty inventory")
            return [], 0
        if container.select_one(".no-jobs-content") or len(summaries) != 1:
            raise SourceError("JobAdder: missing or conflicting pagination")
        match = re.fullmatch(r"Page (\d+) of (\d+)", summaries[0].get_text(" ", strip=True))
        if not match or int(match[1]) != number or not number <= int(match[2]) <= 1000:
            raise SourceError("JobAdder: inconsistent pagination")
        pages = int(match[2])
        if len(jobs) > self.PAGE_SIZE or (number < pages and len(jobs) != self.PAGE_SIZE):
            raise SourceError("JobAdder: incomplete inventory page")
        return jobs, pages

    def _raw(self, item: Any) -> RawJob:
        title = item.select_one(".title [data-job-id]")
        job_id = title.get("data-job-id", "") if title else ""
        if not re.fullmatch(r"[0-9]+", job_id) or not title.get_text(strip=True):
            raise SourceError("JobAdder: missing job ID or title")
        if any(node.get("data-job-id") != job_id for node in item.select("[data-job-id]")):
            raise SourceError("JobAdder: conflicting job IDs")
        summary = item.select_one(".summary")
        if summary is None:
            raise SourceError("JobAdder: missing summary")
        classifications: dict[str, list[str]] = {}
        for node in item.select(".classifications li"):
            key, value = node.get("data-id"), node.get_text(" ", strip=True)
            if not key or not value:
                raise SourceError("JobAdder: malformed classification")
            classifications.setdefault(key, []).append(value)
        date = item.select_one(".date-posted")
        date_text = date.get_text(strip=True) if date else ""
        posted_at = _parse_datetime(date_text)
        if date_text and posted_at is None:
            raise SourceError("JobAdder: malformed posted date")
        parts = urlsplit(str(self.company.careers_url))
        query = [(k, v) for k, v in parse_qsl(parts.query) if k != "ja-job"]
        query.append(("ja-job", job_id))
        raw = RawJob(
            source_company=self.company.slug,
            external_job_id=job_id,
            title=title.get_text(" ", strip=True),
            location_raw="; ".join(
                dict.fromkeys(
                    classifications.get(
                        str(self.company.ats_config["location_classification_id"]), []
                    )
                )
            ),
            description_raw=" ".join(summary.get_text(" ", strip=True).split()),
            posted_at=posted_at,
            url=urlunsplit(parts._replace(query=urlencode(query), fragment="")),
        )
        fingerprint = json.dumps(
            [raw.content_hash, date_text, classifications],
            sort_keys=True,
        )
        raw.metadata = {
            "jobadder_widget": {
                "classifications": classifications,
                "listing_hash": hashlib.sha256(fingerprint.encode()).hexdigest(),
            }
        }
        return raw

    async def fetch(self) -> list[RawJob]:
        items, pages = await self._page(1)
        if not pages:
            return []
        jobs: list[RawJob] = []
        seen: set[str] = set()
        for number in range(1, pages + 1):
            if number > 1:
                items, current_pages = await self._page(number)
                if current_pages != pages:
                    raise SourceError("JobAdder: changing page count")
            for item in items:
                raw = self._raw(item)
                if raw.stable_external_id in seen:
                    raise SourceError("JobAdder: repeated job ID/page")
                seen.add(raw.stable_external_id)
                jobs.append(raw)
        terminal, terminal_pages = await self._page(pages + 1)
        if terminal or terminal_pages:
            raise SourceError("JobAdder: unexpected terminal page")
        return jobs

    async def hydrate(self, raw: RawJob) -> RawJob:
        data = raw.metadata["jobadder_widget"]
        if data.get("detail_checked"):
            return raw
        soup = await self._fragment("detail_endpoint", jobID=raw.stable_external_id)
        description = soup.select_one(".ja-job-details .description")
        title = soup.select_one(".ja-job-details .title")
        if title is None or title.get_text(" ", strip=True) != raw.title:
            raise SourceError("JobAdder: detail title does not match inventory")
        body = _html_text(str(description)) if description is not None else ""
        if not body:
            raise SourceError("JobAdder: missing full description")
        return raw.model_copy(
            update={
                "description_raw": body,
                "metadata": {
                    **raw.metadata,
                    "jobadder_widget": {**data, "detail_checked": True},
                    **_eligibility_metadata({"description": body}),
                },
            }
        )


class DynamicsAtsSource(JobSource):
    """Complete Dynamics ATS feed; the public board paginates locally."""

    async def fetch(self) -> list[RawJob]:
        endpoint = str(self.company.ats_config["listing_endpoint"])
        form_id = str(self.company.ats_config["form_id"])
        response = await self.client.post(endpoint, data={"formId": form_id})
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("Errors") or payload.get("ErrorMessage"):
            raise SourceError("Dynamics ATS: invalid or unsuccessful inventory response")
        items, total = payload.get("Data"), payload.get("Total")
        if not isinstance(items, list) or type(total) is not int or total < 0:
            raise SourceError("Dynamics ATS: missing inventory data or total")
        if len(items) != total:
            raise SourceError("Dynamics ATS: incomplete inventory; Data count differs from Total")
        jobs: list[RawJob] = []
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                raise SourceError("Dynamics ATS: malformed posting")
            try:
                job_id = str(UUID(str(item.get("Id"))))
            except ValueError as exc:
                raise SourceError("Dynamics ATS: missing or invalid posting ID") from exc
            if job_id in seen:
                raise SourceError("Dynamics ATS: repeated posting ID")
            seen.add(job_id)
            title, description = item.get("name"), item.get("description")
            if not _usable_text(title) or not _usable_text(description):
                raise SourceError("Dynamics ATS: missing title or full description")
            title, description = _html_text(title), _html_text(description)
            if not title or not description:
                raise SourceError("Dynamics ATS: empty title or full description")
            detail_url = urljoin(endpoint, f"/JobListing/Details/{form_id}/{job_id}")
            if not _usable_text(item.get("JobUrl")) or item["JobUrl"].rstrip("/") != detail_url:
                raise SourceError("Dynamics ATS: detail URL does not match source and posting ID")
            locations = []
            for key in ("dcrs_location", "dcrs_city", "dcrs_state", "dcrs_country"):
                value = item.get(key)
                if value is not None and not isinstance(value, str):
                    raise SourceError("Dynamics ATS: malformed location")
                value = (value or "").strip()
                if value and value.casefold() not in {"n/a", "na"}:
                    locations.append(value)
            jobs.append(
                RawJob(
                    source_company=self.company.slug,
                    external_job_id=job_id,
                    title=title,
                    location_raw="; ".join(dict.fromkeys(locations)),
                    description_raw=description,
                    url=detail_url,
                    metadata={
                        "dynamics_ats": {
                            key: item.get(key)
                            for key in (
                                "dcrs_category",
                                "dcrs_type",
                                "dcrs_location",
                                "dcrs_city",
                                "dcrs_state",
                                "dcrs_country",
                            )
                        },
                        **_eligibility_metadata(item),
                    },
                )
            )
        return jobs


class OracleSource(JobSource):
    """Candidate Experience inventory; full descriptions only for candidate hydration."""

    def _params(self, offset: int, limit: int) -> dict[str, str]:
        site = self.company.ats_config["site_number"]
        return {
            "onlyData": "true",
            "expand": "requisitionList.secondaryLocations",
            "finder": f"findReqs;siteNumber={site},facetsList=NONE,limit={limit},offset={offset}",
        }

    @staticmethod
    def _inventory(payload: Any) -> dict:
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
            raise SourceError("Oracle: expected one nested inventory object")
        return items[0]

    def _normalize(self, item: dict) -> RawJob:
        job_id = str(item.get("Id", ""))
        if not job_id.isascii() or not job_id.isdigit() or not _usable_text(item.get("Title")):
            raise SourceError("Oracle: missing numeric requisition ID or title")
        secondary = item.get("secondaryLocations") or []
        if not isinstance(secondary, list) or any(not isinstance(x, dict) for x in secondary):
            raise SourceError("Oracle: malformed secondary locations")
        primary = item.get("PrimaryLocation") or ""
        locations = [primary] + [x.get("LocationName") or "" for x in secondary]
        code = item.get("WorkplaceTypeCode")
        arrangement = {
            "ORA_REMOTE": "remote",
            "ORA_HYBRID": "hybrid",
            "ORA_ON_SITE": "onsite",
        }.get(code, item.get("WorkplaceType") or "")
        endpoint = str(self.company.ats_config["listing_endpoint"])
        site = self.company.ats_config["site_number"]
        board = urljoin(endpoint, f"/hcmUI/CandidateExperience/en/sites/{site}")
        raw = RawJob(
            source_company=self.company.slug,
            external_job_id=job_id,
            title=item["Title"],
            location_raw="; ".join(dict.fromkeys(x for x in locations if x)),
            description_raw=_html_text(item.get("ShortDescriptionStr")),
            posted_at=_parse_datetime(item.get("PostedDate")),
            url=f"{board}/job/{job_id}",
            metadata={
                "oracle": {
                    "primary_location": primary,
                    "primary_location_country": item.get("PrimaryLocationCountry"),
                    "secondary_locations": secondary,
                    "workplace_type_code": code,
                    "workplace_type": item.get("WorkplaceType"),
                },
                "eligibility": {"work_arrangement": arrangement} if arrangement else {},
            },
        )
        # Match RawJob hashing conventions while retaining secondary-location changes.
        basis = raw.content_hash + "|" + json.dumps(raw.metadata["oracle"], sort_keys=True)
        raw.metadata["oracle"]["listing_hash"] = hashlib.sha256(basis.encode()).hexdigest()
        return raw

    async def fetch(self) -> list[RawJob]:
        endpoint = str(self.company.ats_config["listing_endpoint"])
        limit = self.company.ats_config.get("limit", 25)
        if type(limit) is not int or limit <= 0:
            raise SourceError("Oracle: page size must be a positive integer")
        jobs: list[RawJob] = []
        seen: set[str] = set()
        offset, expected_total = 0, None
        for _ in range(1000):
            inventory = self._inventory(
                await self.get_json(endpoint, params=self._params(offset, limit))
            )
            page_offset, page_limit, total = (
                inventory.get(key) for key in ("Offset", "Limit", "TotalJobsCount")
            )
            if any(type(x) is not int for x in (page_offset, page_limit, total)):
                raise SourceError("Oracle: missing or invalid nested pagination metadata")
            if page_offset != offset or not 0 < page_limit <= limit or total < offset:
                raise SourceError("Oracle: non-advancing or inconsistent nested pagination")
            if expected_total is not None and total != expected_total:
                raise SourceError("Oracle: inventory total changed during pagination")
            expected_total = total
            items = inventory.get("requisitionList")
            if not isinstance(items, list) or any(not isinstance(x, dict) for x in items):
                raise SourceError("Oracle: missing or invalid requisition list")
            if not items and offset < total:
                raise SourceError("Oracle: unexpected empty page before inventory completion")
            for item in items:
                raw = self._normalize(item)
                if raw.stable_external_id in seen:
                    raise SourceError("Oracle: repeated requisition ID during pagination")
                seen.add(raw.stable_external_id)
                jobs.append(raw)
            if len(items) != min(page_limit, total - offset):
                raise SourceError("Oracle: page count does not match nested inventory metadata")
            next_offset = page_offset + page_limit
            if next_offset >= total:
                if len(seen) != total:
                    raise SourceError("Oracle: incomplete inventory")
                return jobs
            offset = next_offset
        raise SourceError("Oracle: pagination exceeded 1000 pages")

    async def hydrate(self, raw: RawJob) -> RawJob:
        if raw.metadata.get("oracle", {}).get("detail_checked"):
            return raw
        endpoint = urljoin(
            str(self.company.ats_config["listing_endpoint"]), "recruitingCEJobRequisitionDetails"
        )
        site = self.company.ats_config["site_number"]
        payload = await self.get_json(
            endpoint,
            params={
                "onlyData": "true",
                "expand": "all",
                "finder": f"ById;Id={raw.stable_external_id},siteNumber={site}",
            },
        )
        detail = self._inventory(payload)
        if str(detail.get("Id")) != raw.stable_external_id:
            raise SourceError("Oracle: detail identity does not match listing")
        if not _usable_text(detail.get("ExternalDescriptionStr")):
            raise SourceError("Oracle: full candidate description is missing")
        description = "\n".join(
            _html_text(detail.get(key))
            for key in (
                "ExternalDescriptionStr",
                "ExternalResponsibilitiesStr",
                "ExternalQualificationsStr",
                "OrganizationDescriptionStr",
                "CorporateDescriptionStr",
            )
            if _usable_text(detail.get(key))
        )
        eligibility = {
            **raw.metadata.get("eligibility", {}),
            **_eligibility_metadata({"description": description}).get("eligibility", {}),
        }
        return raw.model_copy(
            update={
                "description_raw": description,
                "metadata": {
                    **raw.metadata,
                    "eligibility": eligibility,
                    "oracle": {**raw.metadata["oracle"], "detail_checked": True},
                },
            }
        )


SOURCE_CLASSES: dict[AtsType, type[JobSource]] = {
    AtsType.JOBADDER_WIDGET: JobAdderWidgetSource,
    AtsType.DYNAMICS_ATS: DynamicsAtsSource,
    AtsType.ORACLE: OracleSource,
    AtsType.GREENHOUSE: GreenhouseSource,
    AtsType.LEVER: LeverSource,
    AtsType.ASHBY: AshbySource,
    AtsType.SMARTRECRUITERS: SmartRecruitersSource,
    AtsType.WORKDAY: WorkdaySource,
    AtsType.TALEMETRY: TalemetrySource,
    AtsType.JIBE: JibeSource,
    AtsType.JSONLD: JsonLdSource,
    AtsType.EIGHTFOLD: EightfoldSource,
    AtsType.SUCCESSFACTORS: SuccessFactorsSource,
    AtsType.TEAMTAILOR: TeamtailorSource,
    AtsType.CITY_OF_HOPE: CityOfHopeSource,
    AtsType.CHARTER_RESEARCH: CharterResearchSource,
    AtsType.TALENTBREW: TalentBrewSource,
}


class SourceRunner:
    def __init__(self, client: httpx.AsyncClient, max_concurrency: int = 5):
        self.client = client
        self.semaphore = asyncio.Semaphore(max_concurrency)
        self.domain_locks: dict[str, asyncio.Lock] = {}
        self.workday_controller = WorkdayRequestController()

    async def fetch_with_warnings(
        self, company: CompanyConfig
    ) -> tuple[list[RawJob], list[dict[str, str]]]:
        domain = httpx.URL(str(company.careers_url)).host or company.slug
        lock = self.domain_locks.setdefault(domain, asyncio.Lock())
        async with self.semaphore, lock:
            if company.ats_type is AtsType.WORKDAY:
                source = WorkdaySource(company, self.client, self.workday_controller)
            else:
                source = SOURCE_CLASSES[company.ats_type](company, self.client)
            jobs = await source.fetch()
            return jobs, list(getattr(source, "warnings", []))

    async def fetch(self, company: CompanyConfig) -> list[RawJob]:
        jobs, _warnings = await self.fetch_with_warnings(company)
        return jobs

    async def hydrate_candidate(self, company: CompanyConfig, raw: RawJob) -> RawJob:
        source_class = SOURCE_CLASSES[company.ats_type]
        source = source_class(company, self.client)
        hydrate = getattr(source, "hydrate", None)
        return await hydrate(raw) if hydrate else raw
