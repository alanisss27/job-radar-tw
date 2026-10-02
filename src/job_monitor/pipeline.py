from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from time import perf_counter

import httpx

from .active_status import (
    ActiveStatus,
    manual_verification_label,
    notification_status_allows,
    verify_if_actionable,
)
from .config import CandidateProfile, ProfileConfig, SearchPreferences, Settings
from .llm import LLMEnricher
from .matching import match_job, parse_job
from .eligibility import candidate_rejections
from .models import CompanyConfig, MatchedJob, MatchResult, ParsedJob, RawJob
from .notifier import (
    TelegramNotifier,
    render_failure_alert,
    render_job_message,
    render_run_summary,
)
from .resume import load_resume
from .schedule import local_run_key, notification_delay
from .sources import SourceRunner
from .storage import PERSIST_CHUNK_SIZE, JobIndexRow, JobPlan, MatchDecision, Storage

logger = logging.getLogger(__name__)

PERSIST_PROGRESS_INTERVAL = 1000


def _supports_batch_persistence(storage: Storage) -> bool:
    return hasattr(storage, "prefetch_job_index") and hasattr(
        storage, "persist_job_decisions_batch"
    )


def _potential_city_of_hope_candidate(results: list[MatchResult]) -> bool:
    return any(
        result.eligible
        or result.discovery_eligible
        or result.needs_eligibility_review
        or any(
            reason.startswith((
                "clinical coordination:",
                "clinical project support:",
                "clinical trial manager:",
                "transferable life-science PM:",
                "discovery title family:",
                "responsibilities:",
            ))
            for reason in result.reasons
        )
        for result in results
    )


@dataclass
class RunReport:
    run_key: str
    sources_attempted: int = 0
    sources_succeeded: int = 0
    jobs_fetched: int = 0
    jobs_new: int = 0
    jobs_changed: int = 0
    matches: int = 0
    eligibility_reviews: int = 0
    notifications: int = 0
    immediate_candidates: int = 0
    notifications_suppressed: int = 0
    notifications_pending: int = 0
    jobs_closed: int = 0
    errors: list[dict[str, str]] = field(default_factory=list)
    source_warnings: list[dict[str, str]] = field(default_factory=list)
    matched_jobs: list[MatchedJob] = field(default_factory=list)
    dry_run_matches: list[MatchedJob] = field(default_factory=list)
    zero_job_sources: list[str] = field(default_factory=list)
    skipped_reason: str | None = None

    def stats(self) -> dict[str, int]:
        return {key: value for key, value in self.__dict__.items() if isinstance(value, int)}


@dataclass
class CompanyBatchPersistence:
    storage: Storage
    company_id: str
    run_id: str
    company_slug: str
    jobs_fetched: int
    report: RunReport
    items: list[tuple[RawJob, JobPlan, list[MatchDecision]]] = field(default_factory=list)
    eligible_matches: list[list[MatchedJob]] = field(default_factory=list)
    persisted_jobs: int = 0
    jobs_new: int = 0
    jobs_changed: int = 0
    jobs_unchanged: int = 0

    def add(
        self,
        raw: RawJob,
        plan: JobPlan,
        decisions: list[MatchDecision],
        matches: list[MatchedJob],
    ) -> None:
        self.items.append((raw, plan, decisions))
        self.eligible_matches.append(matches)
        if len(self.items) >= PERSIST_CHUNK_SIZE:
            self.flush()

    def flush(self) -> None:
        if not self.items:
            return
        persisted = self.storage.persist_job_decisions_batch(
            self.company_id,
            self.run_id,
            self.items,
        )
        for (_, _plan, _), result, eligible_matches in zip(
            self.items,
            persisted,
            self.eligible_matches,
            strict=True,
        ):
            self.report.immediate_candidates += result.notifications_enqueued
            self.report.jobs_new += int(result.is_new)
            self.report.jobs_changed += int(result.changed and not result.is_new)
            self.report.matches += sum(m.result.notification_eligible for m in eligible_matches)
            self.report.eligibility_reviews += sum(
                m.result.needs_eligibility_review for m in eligible_matches
            )
            self.report.matched_jobs.extend(
                replace(
                    match,
                    first_seen_at=result.first_seen_at,
                    is_new=result.is_new,
                    changed=result.changed,
                )
                for match in eligible_matches
            )
            self.jobs_new += int(result.is_new)
            self.jobs_changed += int(result.changed and not result.is_new)
            self.jobs_unchanged += int(not result.changed)
        self.persisted_jobs += len(persisted)
        if self.persisted_jobs % PERSIST_PROGRESS_INTERVAL == 0 or (
            self.persisted_jobs == self.jobs_fetched
        ):
            logger.info(
                "Persistence progress for %s: %d/%d jobs",
                self.company_slug,
                self.persisted_jobs,
                self.jobs_fetched,
            )
        self.items.clear()
        self.eligible_matches.clear()


@dataclass(frozen=True)
class CompanyRunContext:
    company: CompanyConfig
    company_id: str
    baseline: bool


@dataclass(frozen=True)
class SourceFetchResult:
    context: CompanyRunContext
    raw_jobs: list[RawJob]
    warnings: list[dict[str, str]] = field(default_factory=list)
    error: Exception | None = None


async def _try_send_notification(
    notifier: TelegramNotifier | None,
    text: str,
    report: RunReport,
    *,
    context: str,
) -> bool:
    if notifier is None:
        return False
    try:
        await notifier.send(text)
        return True
    except Exception as exc:
        logger.exception("Telegram notification failed during %s", context)
        report.errors.append({"company": "telegram", "error": f"{context}: {str(exc)[:450]}"})
        return False


async def _fetch_company_source(
    runner: SourceRunner,
    context: CompanyRunContext,
) -> SourceFetchResult:
    try:
        if hasattr(runner, "fetch_with_warnings"):
            raw_jobs, warnings = await runner.fetch_with_warnings(context.company)
        else:  # compatibility for lightweight test/dry-run runners
            raw_jobs, warnings = await runner.fetch(context.company), []
        return SourceFetchResult(context=context, raw_jobs=raw_jobs, warnings=warnings)
    except Exception as exc:
        return SourceFetchResult(context=context, raw_jobs=[], error=exc)


def _qualifies_for_immediate_notification(
    parsed: ParsedJob,
    result: MatchResult,
    first_seen_at: datetime,
    settings: Settings,
    *,
    is_new: bool,
    backfill: bool = False,
) -> bool:
    if not result.notification_eligible:
        return False
    if result.bucket != "target":
        return False
    if not is_new and not backfill:
        return False
    if result.tier != "strong" or result.score < settings.immediate_notification_min_score:
        return False
    return True


def _safe_error(exc: BaseException, settings: Settings) -> str:
    message = str(exc) or type(exc).__name__
    secret_values = [
        settings.database_url,
        settings.telegram_bot_token,
        settings.openai_api_key,
        settings.resume_text.get_secret_value() if settings.resume_text else None,
    ]
    for value in secret_values:
        if value:
            message = message.replace(value, "[redacted]")
    return message[:500]


async def run_clinical_title_backfill(
    settings: Settings,
    companies: list[CompanyConfig],
    profiles: dict[str, ProfileConfig],
    preferences: SearchPreferences,
    candidate: CandidateProfile | None,
    target_run_key: str,
) -> dict[str, int | str]:
    """Re-evaluate stored current inventory once, without fetching any sources."""
    if not settings.database_url:
        raise ValueError("DATABASE_URL is required")
    profile_name = "clinical-discovery"
    if profile_name not in profiles:
        raise ValueError("clinical-discovery profile is not configured")
    storage = Storage(settings.database_url, create_schema=False)
    source_run_id = storage.completed_run_id(target_run_key)
    if source_run_id is None:
        raise ValueError(f"No successful stored source run found: {target_run_key}")
    marker_key = f"clinical-title-backfill-v57547c6-{target_run_key}"
    claim = storage.claim_run(marker_key)
    if claim is None:
        return {"status": "already_completed_or_running", "jobs_re_evaluated": 0,
                "newly_recovered_matches": 0, "actionable_candidates": 0,
                "active": 0, "inactive": 0, "unknown": 0}

    counters = {"jobs_re_evaluated": 0, "newly_recovered_matches": 0,
                "actionable_candidates": 0, "active": 0, "inactive": 0, "unknown": 0}
    recovered_titles: list[str] = []
    errors: list[dict[str, str]] = []
    try:
        company_by_id: dict[str, CompanyConfig] = {}
        selected_companies = [company for company in companies if profile_name in company.profiles]
        company_ids = storage.company_ids_for_slugs({company.slug for company in selected_companies})
        company_by_id = {
            company_id: company
            for company in selected_companies
            if (company_id := company_ids.get(company.slug)) is not None
        }
        inventory = storage.clinical_backfill_inventory(
            source_run_id, set(company_by_id), profile_name
        )
        resume = load_resume(
            settings.resume_path,
            settings.resume_text.get_secret_value() if settings.resume_text else None,
        )
        timeout = httpx.Timeout(settings.request_timeout_seconds)
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=True,
            headers={"User-Agent": "JobRadarTW/0.1"},
        ) as client:
            active_status_cache: dict[str, ActiveStatus] = {}
            for row in inventory:
                counters["jobs_re_evaluated"] += 1
                raw = RawJob.model_validate(row["payload"])
                company = company_by_id[row["company_id"]]
                parsed = parse_job(raw)
                result = match_job(
                    parsed, profiles[profile_name], preferences, resume,
                    visa_sponsorship_required=settings.visa_sponsorship_required,
                    company_visa_support=company.visa_support,
                    candidate=candidate, company_ndx_member=company.ndx_member,
                )
                storage.record_match(
                    row["job_id"], profiles[profile_name].version,
                    row["content_hash"], result,
                )
                if (
                    not result.notification_eligible
                    or row["previously_matched"]
                    or row["previously_notified"]
                ):
                    continue
                counters["newly_recovered_matches"] += 1
                recovered_titles.append(raw.title)
                if not _qualifies_for_immediate_notification(
                    parsed, result, datetime.now(UTC), settings, is_new=True
                ):
                    continue
                status = await verify_if_actionable(True, raw, client, active_status_cache)
                counters[status.value] += 1
                if not notification_status_allows(status):
                    continue
                message = render_job_message(
                    company.name, parsed, result, datetime.now(UTC),
                    display_timezone=settings.monitor_timezone,
                )
                label = manual_verification_label(status)
                if label:
                    message += "\n\n" + label
                if storage.queue_notification(
                    row["job_id"], profile_name, row["content_hash"],
                    result.score, message,
                ):
                    counters["actionable_candidates"] += 1
        storage.finish_run(claim.run_id, counters, errors)
        return {"status": "completed", **counters, "recovered_titles": recovered_titles}
    except BaseException as exc:
        errors.append({"company": "clinical-title-backfill", "error": _safe_error(exc, settings)})
        storage.finish_run(claim.run_id, counters, errors)
        raise


async def run_pipeline(
    settings: Settings,
    companies: list[CompanyConfig],
    profiles: dict,
    preferences: SearchPreferences,
    candidate: CandidateProfile | None = None,
    *,
    dry_run: bool = False,
    backfill: bool = False,
    suppress_notifications: bool = False,
    run_key: str | None = None,
    scheduled: bool = False,
) -> RunReport:
    key = run_key or local_run_key(timezone=settings.monitor_timezone)
    report = RunReport(run_key=key)
    if not dry_run and not settings.database_url:
        raise ValueError("DATABASE_URL is required outside dry-run")
    resume = load_resume(
        settings.resume_path,
        settings.resume_text.get_secret_value() if settings.resume_text else None,
    )
    storage = None if dry_run else Storage(settings.database_url or "", create_schema=False)
    run_id = "dry-run"
    if storage:
        claim = storage.claim_run(key, **({"stale_after_minutes": 120} if scheduled else {}))
        if claim is None:
            report.skipped_reason = "duplicate_run_key"
            logger.warning("Run %s already completed or is in progress; skipping fetch", key)
            return report
        run_id = claim.run_id

    try:
        timeout = httpx.Timeout(settings.request_timeout_seconds)
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": "JobRadarTW/0.1"},
        ) as client:
            runner = SourceRunner(client, settings.max_concurrency)
            active_status_cache: dict[str, ActiveStatus] = {}
            notifier = None
            if (
                settings.telegram_bot_token
                and settings.telegram_chat_id
                and not dry_run
                and not suppress_notifications
            ):
                notifier = TelegramNotifier(
                    settings.telegram_bot_token, settings.telegram_chat_id, client
                )
            enricher = None
            if settings.llm_enabled:
                enricher = LLMEnricher(settings.openai_api_key or "", settings.openai_model or "")
            source_contexts: list[CompanyRunContext] = []
            for company in companies:
                if not company.enabled:
                    continue
                report.sources_attempted += 1
                company_id = storage.sync_company(company) if storage else company.slug
                baseline = storage is not None and not storage.is_baseline_completed(company_id)
                source_contexts.append(CompanyRunContext(company, company_id, baseline))

            source_results = await asyncio.gather(
                *(_fetch_company_source(runner, context) for context in source_contexts)
            )

            for source_result in source_results:
                if storage:
                    storage.assert_active_run(run_id)
                company = source_result.context.company
                company_id = source_result.context.company_id
                baseline = source_result.context.baseline
                if source_result.error is not None:
                    logger.exception(
                        "Source failed: %s", company.slug, exc_info=source_result.error
                    )
                    report.errors.append(
                        {"company": company.slug, "error": str(source_result.error)[:500]}
                    )
                    failures = storage.source_failed(company_id, run_id) if storage else 1
                    if failures >= 3:
                        await _try_send_notification(
                            notifier,
                            render_failure_alert(company.name, failures, str(source_result.error)),
                            report,
                            context=f"source failure alert for {company.slug}",
                        )
                    continue

                raw_jobs = source_result.raw_jobs
                report.source_warnings.extend(source_result.warnings)
                report.jobs_fetched += len(raw_jobs)
                if company.source_verified and not raw_jobs:
                    report.zero_job_sources.append(company.name)
                company_started = perf_counter()
                use_batch = storage is not None and _supports_batch_persistence(storage)
                job_index = storage.prefetch_job_index(company_id) if use_batch else {}
                # Only current official-source results can enter this migration.
                age_claim = None
                age_candidates = set()
                if use_batch and company.source_verified and not baseline and notifier:
                    age_claim = storage.claim_run(f"posting-age-v1-{company_id}")
                    if age_claim:
                        age_candidates = storage.age_suppressed_job_ids(
                            company_id,
                            settings.immediate_notification_max_source_age_days,
                            settings.immediate_notification_min_score,
                        )
                batch = (
                    CompanyBatchPersistence(
                        storage=storage,
                        company_id=company_id,
                        run_id=run_id,
                        company_slug=company.slug,
                        jobs_fetched=len(raw_jobs),
                        report=report,
                    )
                    if use_batch
                    else None
                )

                for raw in raw_jobs:
                    parsed = parse_job(raw)
                    observed_at = datetime.now(UTC)
                    plan = (
                        storage.plan_job_from_index(
                            raw,
                            job_index.get(raw.stable_external_id),
                        )
                        if use_batch
                        else storage.plan_job(company_id, raw)
                        if storage
                        else JobPlan(
                            job_id=raw.stable_external_id,
                            is_new=True,
                            changed=True,
                            first_seen_at=observed_at,
                            previous_content_hash=None,
                        )
                    )
                    if use_batch:
                        job_index[raw.stable_external_id] = JobIndexRow(
                            job_id=plan.job_id,
                            content_hash=raw.content_hash,
                            first_seen_at=plan.first_seen_at,
                        )
                    age_backfill = plan.job_id in age_candidates
                    if not plan.changed and not backfill and not age_backfill:
                        if use_batch:
                            batch.add(raw, plan, [], [])
                            continue
                        if storage:
                            storage.persist_job_decisions(
                                company_id,
                                run_id,
                                raw,
                                plan,
                                [],
                            )
                        continue

                    if (
                        raw.metadata.get("city_of_hope")
                        or raw.metadata.get("charter_research")
                        or raw.metadata.get("oracle")
                        or raw.metadata.get("jobadder_widget")
                        or raw.metadata.get("teamtailor")
                    ) and hasattr(runner, "hydrate_candidate"):
                        preliminary = [
                            match_job(
                                parsed,
                                profiles[profile_name],
                                preferences,
                                resume,
                                visa_sponsorship_required=settings.visa_sponsorship_required,
                                company_visa_support=company.visa_support,
                                candidate=candidate,
                                company_ndx_member=company.ndx_member,
                            )
                            for profile_name in company.profiles
                        ]
                        potential = _potential_city_of_hope_candidate(preliminary)
                        if potential:
                            raw = await runner.hydrate_candidate(company, raw)
                            parsed = parse_job(raw)
                            # These sources hash listing fields for stable change detection;
                            # recompute the plan so the hydrated payload is still persisted.
                            plan = (
                                storage.plan_job_from_index(raw, job_index.get(raw.stable_external_id))
                                if use_batch
                                else storage.plan_job(company_id, raw)
                                if storage
                                else plan
                            )

                    decisions: list[MatchDecision] = []
                    eligible_matches: list[MatchedJob] = []
                    for profile_name in company.profiles:
                        profile: ProfileConfig = profiles[profile_name]
                        result = match_job(
                            parsed,
                            profile,
                            preferences,
                            resume,
                            visa_sponsorship_required=settings.visa_sponsorship_required,
                            company_visa_support=company.visa_support,
                            candidate=candidate,
                            company_ndx_member=company.ndx_member,
                        )
                        if (
                            0.45 <= result.score < profile.strong_threshold
                            and parsed.ambiguities
                            and enricher
                        ):
                            try:
                                parsed = await enricher.enrich(parsed)
                                result = match_job(
                                    parsed,
                                    profile,
                                    preferences,
                                    resume,
                                    visa_sponsorship_required=settings.visa_sponsorship_required,
                                    company_visa_support=company.visa_support,
                                    candidate=candidate,
                                    company_ndx_member=company.ndx_member,
                                )
                                result.used_llm = True
                            except Exception as exc:
                                logger.warning("LLM fallback for %s: %s", raw.title, exc)
                        matched = None
                        notification_message = None
                        if result.notification_eligible or result.needs_eligibility_review:
                            matched = MatchedJob(
                                company_name=company.name,
                                job=parsed,
                                result=result,
                                first_seen_at=plan.first_seen_at,
                                is_new=plan.is_new,
                                changed=plan.changed,
                            )
                            eligible_matches.append(matched)
                            should_notify = (
                                storage is not None
                                and notifier is not None
                                and (not baseline or backfill)
                                and _qualifies_for_immediate_notification(
                                    parsed,
                                    result,
                                    plan.first_seen_at,
                                    settings,
                                    is_new=plan.is_new,
                                    backfill=backfill or age_backfill,
                                )
                            )
                            if should_notify:
                                status = await verify_if_actionable(
                                    should_notify, raw, client, active_status_cache
                                )
                                if status is not None and notification_status_allows(status):
                                    notification_message = render_job_message(
                                        matched.company_name,
                                        matched.job,
                                        matched.result,
                                        matched.first_seen_at,
                                        is_new=matched.is_new,
                                        changed=matched.changed,
                                        display_timezone=settings.monitor_timezone,
                                    )
                                    label = manual_verification_label(status)
                                    if label:
                                        notification_message += "\n\n" + label
                        decisions.append(
                            MatchDecision(
                                profile_version=profile.version,
                                result=result,
                                notification_message=notification_message,
                            )
                        )

                    if storage:
                        if use_batch:
                            batch.add(raw, plan, decisions, eligible_matches)
                            continue
                        persisted = storage.persist_job_decisions(
                            company_id,
                            run_id,
                            raw,
                            plan,
                            decisions,
                        )
                        report.immediate_candidates += persisted.notifications_enqueued
                    report.jobs_new += int(plan.is_new)
                    report.jobs_changed += int(plan.changed and not plan.is_new)
                    report.matches += sum(m.result.notification_eligible for m in eligible_matches)
                    report.eligibility_reviews += sum(m.result.needs_eligibility_review for m in eligible_matches)
                    report.matched_jobs.extend(eligible_matches)
                    if dry_run:
                        report.dry_run_matches.extend(eligible_matches)
                if storage:
                    if use_batch:
                        batch.flush()
                    report.jobs_closed += storage.mark_missing(company_id, run_id)
                    storage.source_succeeded(company_id, run_id)
                    if age_claim:
                        storage.finish_run(age_claim.run_id, {}, [])
                    logger.info(
                        "Persisted company %s: jobs_fetched=%d new=%d changed=%d "
                        "unchanged=%d elapsed_seconds=%.2f",
                        company.slug,
                        len(raw_jobs),
                        batch.jobs_new if batch else 0,
                        batch.jobs_changed if batch else 0,
                        batch.jobs_unchanged if batch else 0,
                        perf_counter() - company_started,
                    )
                report.sources_succeeded += 1

            if notifier and scheduled:
                await asyncio.sleep(notification_delay(timezone=settings.monitor_timezone))

            if notifier and storage:
                pending_before_delivery = storage.pending_notification_count()
                queued = storage.claim_pending_notifications(
                    run_id, settings.immediate_notification_max_per_run,
                    **({"eligibility_check": lambda raw: candidate_rejections(raw, preferences)}
                       if preferences.candidate_eligibility is not None else {}),
                )
                report.notifications_suppressed = max(
                    0,
                    pending_before_delivery - len(queued),
                )
                for item in queued:
                    raw = (
                        storage.notification_job(item["job_id"], item["version_hash"])
                        if hasattr(storage, "notification_job")
                        else None
                    )
                    if preferences.candidate_eligibility is not None:
                        rejected = (
                            candidate_rejections(raw, preferences) if raw else
                            ["eligibility_unknown: queued posting payload unavailable"]
                        )
                        if rejected:
                            storage.release_notification_claim(
                                run_id, item["id"], item["claim_token"], "; ".join(rejected)
                            )
                            continue
                    if raw is not None:
                        status = await verify_if_actionable(
                            True, raw, client, active_status_cache
                        )
                        if status is ActiveStatus.INACTIVE:
                            storage.suppress_notification_claim(
                                run_id, item["id"], item["claim_token"]
                            )
                            continue
                        if status is ActiveStatus.UNKNOWN and manual_verification_label(status) not in item["message"]:
                            item["message"] += "\n\n" + manual_verification_label(status)
                    elif preferences.candidate_eligibility is None:
                        item["message"] += (
                            "\n\nActive status not confirmed — manual verification needed"
                        )
                    if not storage.notification_claim_is_valid(
                        run_id, item["id"], item["claim_token"]
                    ):
                        continue
                    sent = await _try_send_notification(
                        notifier,
                        item["message"],
                        report,
                        context=f"job alert for {item['job_id']}",
                    )
                    if not sent:
                        storage.release_notification_claim(
                            run_id,
                            item["id"],
                            item["claim_token"],
                            report.errors[-1]["error"],
                        )
                        continue
                    if storage.mark_notification_sent(
                        run_id,
                        item["id"],
                        item["claim_token"],
                    ):
                        report.notifications += 1
                report.notifications_pending = storage.pending_notification_count()

            if notifier and report.sources_attempted and not report.sources_succeeded:
                await _try_send_notification(
                    notifier,
                    "🚨 職缺雷達本次執行全部來源失敗\n"
                    + "\n".join(
                        f"{item['company']}: {item['error'][:200]}" for item in report.errors
                    ),
                    report,
                    context="all sources failed alert",
                )
            if notifier:
                await _try_send_notification(
                    notifier,
                    render_run_summary(
                        run_key=report.run_key,
                        stats=report.stats(),
                        errors=report.errors,
                        source_warnings=report.source_warnings,
                        matched_jobs=report.matched_jobs,
                        zero_job_sources=report.zero_job_sources,
                        max_matches=settings.daily_summary_max_matches,
                        max_reviews=settings.daily_summary_max_reviews,
                        display_timezone=settings.monitor_timezone,
                    ),
                    report,
                    context="daily summary",
                )
    except BaseException as exc:
        report.errors.append({"company": "pipeline", "error": _safe_error(exc, settings)})
        raise
    finally:
        if storage:
            storage.finish_run(run_id, report.stats(), report.errors)
    return report
