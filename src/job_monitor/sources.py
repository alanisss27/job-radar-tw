from __future__ import annotations

import asyncio
import email.utils
import json
import logging
import random
import re
import time
from abc import ABC, abstractmethod
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup
from pydantic import ValidationError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .models import AtsType, CompanyConfig, RawJob
from .eligibility import credential_clauses

logger = logging.getLogger(__name__)


def _eligibility_metadata(item: dict) -> dict:
    """Retain explicit ATS requirement facts otherwise lost during normalization."""
    def texts(value):
        if isinstance(value, str):
            return [value]
        if isinstance(value, list):
            return [text for entry in value for text in texts(entry)]
        if isinstance(value, dict):
            return [text for key in ("name", "value", "text", "content", "addressRegion", "addressCountry")
                    for text in texts(value.get(key))]
        return []

    facts = {}
    credential_evidence = [clause for key in (
        "content", "descriptionHtml", "descriptionPlain", "description", "jobDescription"
    ) for text in texts(item.get(key)) for clause in credential_clauses(text)]
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
        if isinstance(entry, dict) and any(word in entry.get("text", "").lower()
                                           for word in ("qualification", "requirement")):
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
                metadata={"departments": item.get("departments", []), **_eligibility_metadata(item)},
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
                metadata={"categories": categories, **_eligibility_metadata(item)},
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
                metadata={"department": item.get("department"), **_eligibility_metadata(item)},
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
                        **_eligibility_metadata(detail),
                        "smartrecruiters": {
                            "country_code": str(location.get("country", "")).strip().lower()
                        }
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

    def _record_exclusion(self, item: Mapping[str, Any], title: str, path: str, reason: str) -> None:
        self.warnings.append({
            "company": self.company.name,
            "title": str(title),
            "location": str(item.get("locationsText") or ""),
            "reason": reason,
            "url": self.company.ats_config.get("detail_base_url", "").rstrip("/") + path,
        })

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
        validate_locations = cfg.get("validate_location_facets", False)
        facet_country = cfg.get("location_facet_country")
        if validate_locations and ("locations" not in patterns or not cfg.get("detail_api_base")):
            raise SourceError("Workday location validation requires locations pattern and detail API")
        if facet_country is not None and (not validate_locations or not _usable_text(facet_country)):
            raise SourceError("Workday location_facet_country requires validated location facets")
        if patterns:
            response = await self._request(
                "POST",
                endpoint,
                json={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""},
            )
            payload = response.json()
            resolved = {key: [] for key in patterns}

            def collect_facets(nodes):
                if not isinstance(nodes, list):
                    return
                for node in nodes:
                    if not isinstance(node, dict):
                        continue
                    key = node.get("facetParameter")
                    values = node.get("values", [])
                    if key in patterns and isinstance(values, list):
                        for value in values:
                            if (
                                isinstance(value, dict)
                                and isinstance(value.get("descriptor"), str)
                                and _usable_text(value.get("id"))
                                and patterns[key].search(value["descriptor"])
                            ):
                                resolved[key].append(value["id"])
                    collect_facets(values)

            collect_facets(payload.get("facets") if isinstance(payload, dict) else None)
            if any(not ids for ids in resolved.values()):
                raise SourceError("Workday facet patterns resolved no IDs; refusing unscoped fetch")
            applied_facets.update({key: list(dict.fromkeys(ids)) for key, ids in resolved.items()})
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
                    if cfg.get("detail_api_base"):
                        try:
                            detail_response = await self._request(
                                "GET", cfg["detail_api_base"].rstrip("/") + external_path
                            )
                            detail = detail_response.json().get("jobPostingInfo", {})
                            if validate_locations:
                                primary = detail.get("location")
                                additional = detail.get("additionalLocations") or []
                                if (
                                    not _usable_text(primary)
                                    or not isinstance(additional, list)
                                    or any(not _usable_text(value) for value in additional)
                                    or not any(patterns["locations"].search(value)
                                               for value in [primary, *additional])
                                ):
                                    raise WorkdayLocationValidationError(
                                        "Workday detail does not confirm scoped location for "
                                        f"{self.company.slug}{external_path}"
                                    )
                                scoped_additional = [value for value in additional
                                                     if patterns["locations"].search(value)]
                                primary_country = (detail.get("country") or {}).get("descriptor")
                                if (facet_country and not scoped_additional
                                        and primary_country != facet_country):
                                    raise WorkdayLocationValidationError(
                                        "Workday primary country does not confirm location facet "
                                        f"for {self.company.slug}{external_path}"
                                    )
                                # Country belongs to the primary location, never to an
                                # additional location merely selected by a search facet.
                                location_metadata = {"workday_locations": {
                                    "primary": primary,
                                    "additional": additional,
                                    "primary_country": detail.get("country"),
                                    "requisition_location": detail.get("jobRequisitionLocation"),
                                    "listing": item.get("locationsText"),
                                    "facet_country": facet_country,
                                    "scoped_additional": scoped_additional,
                                }}
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
                                    .get("country", {}).get("alpha2Code"),
                                    *(f"{value}, {facet_country}"
                                      if facet_country and value in scoped_additional else value
                                      for value in additional),
                                ]
                            for location in detail_locations:
                                if location and location.casefold() not in {
                                    value.casefold() for value in location_parts if value
                                }:
                                    location_parts.append(location)
                        except WorkdayRequestError as exc:
                            if validate_locations:
                                self._record_exclusion(item, title, external_path, "detail request failed after retries")
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
                                self._record_exclusion(item, title, external_path, "location validation failed")
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
                                self._record_exclusion(item, title, external_path, "detail HTTP failure")
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
                                self._record_exclusion(item, title, external_path, "detail evidence malformed or unavailable")
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
                        metadata={"workday": item, **location_metadata, **eligibility_metadata},
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
        page = 1
        jobs: list[RawJob] = []
        while True:
            payload = await self.get_json(cfg["endpoint"], params={"page": page, "limit": limit})
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
                    metadata={"jibe": item, **_eligibility_metadata(item)},
                )
            total = payload.get("totalCount") if isinstance(payload, dict) else None
            if not entries or (isinstance(total, int) and page * limit >= total) or len(entries) < limit:
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


SOURCE_CLASSES: dict[AtsType, type[JobSource]] = {
    AtsType.GREENHOUSE: GreenhouseSource,
    AtsType.LEVER: LeverSource,
    AtsType.ASHBY: AshbySource,
    AtsType.SMARTRECRUITERS: SmartRecruitersSource,
    AtsType.WORKDAY: WorkdaySource,
    AtsType.TALEMETRY: TalemetrySource,
    AtsType.JIBE: JibeSource,
    AtsType.JSONLD: JsonLdSource,
}


class SourceRunner:
    def __init__(self, client: httpx.AsyncClient, max_concurrency: int = 5):
        self.client = client
        self.semaphore = asyncio.Semaphore(max_concurrency)
        self.domain_locks: dict[str, asyncio.Lock] = {}
        self.workday_controller = WorkdayRequestController()

    async def fetch_with_warnings(self, company: CompanyConfig) -> tuple[list[RawJob], list[dict[str, str]]]:
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
