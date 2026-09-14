"""Regression tests use only temporary SQLite databases and mocked network clients."""

import pytest
from sqlalchemy import event, insert, select, update

from job_monitor import pipeline
from job_monitor.config import ProfileConfig, SearchPreferences, Settings
from job_monitor.models import MatchResult
from job_monitor.storage import MatchDecision, Storage, jobs, match_results, notification_outbox
from test_storage import company, raw, raw_job, semantic_snapshot


def decision(eligible=True, score=0.9, version="1", message="old", profile="tech"):
    return MatchDecision(
        version,
        MatchResult(
            profile=profile,
            eligible=eligible,
            score=score,
            tier="strong" if eligible else "filtered",
            reasons=[message] if eligible else [],
            filtered_reason=None if eligible else "location",
        ),
        message if eligible else None,
    )


@pytest.fixture(params=[False, True], ids=["single", "batch"])
def store(tmp_path, request):
    db = Storage(f"sqlite:///{tmp_path / 'isolated.db'}", create_schema=True)
    cid = db.sync_company(company())
    rid = db.start_run("test")

    def persist(item, decisions):
        plan = db.plan_job(cid, item)
        if request.param:
            return db.persist_job_decisions_batch(cid, rid, [(item, plan, decisions)])[0]
        return db.persist_job_decisions(cid, rid, item, plan, decisions)

    yield db, cid, rid, persist
    db.engine.dispose()


def rows(db, table=match_results):
    with db.engine.connect() as conn:
        return [dict(row) for row in conn.execute(select(table)).mappings()]


def assert_invisible(db):
    assert db.list_handoff_jobs() == []
    assert db.list_dashboard_jobs() == []
    snapshot = db.dashboard_snapshot()
    assert snapshot["kpis"]["recommended"] == 0
    assert snapshot["queue"] == []
    assert snapshot["industries"] == []
    assert snapshot["sources"] == []
    assert snapshot["stages"] == []


@pytest.mark.parametrize("claimed", [False, True])
def test_rejection_replaces_result_and_cancels_alert(store, claimed):
    db, _, rid, persist = store
    persist(raw(), [decision()])
    before = rows(db)[0]
    assert len(db.list_handoff_jobs()) == 1
    assert len(db.list_dashboard_jobs()) == 1
    claim = db.claim_pending_notifications(rid, 1)[0] if claimed else None
    persist(raw(), [decision(False, 0)])
    after = rows(db)[0]
    assert after["id"] == before["id"]
    assert after["created_at"] == before["created_at"]
    assert after["eligible"] is False
    assert after["details"]["filtered_reason"] == "location"
    assert after["details"]["reasons"] == []
    assert rows(db, jobs)[0]["status"] == "active"
    assert_invisible(db)
    assert rows(db, notification_outbox) == []
    assert db.pending_notification_count() == 0
    assert db.list_pending_notifications(5) == []
    assert db.claim_pending_notifications(rid, 5) == []
    if claim:
        assert not db.notification_claim_is_valid(rid, claim["id"], claim["claim_token"])
        assert not db.mark_notification_sent(rid, claim["id"], claim["claim_token"])


@pytest.mark.parametrize("suppressed", [False, True])
def test_changed_eligible_payload_replaces_claim_without_resending_history(store, suppressed):
    db, _, rid, persist = store
    persist(raw(), [decision()])
    original = rows(db)[0]
    claim = db.claim_pending_notifications(rid, 1)[0]
    changed = decision(score=0.75, message="new")
    changed.result.tier = "potential"
    if suppressed:
        changed = MatchDecision(changed.profile_version, changed.result, None)
    persist(raw(), [changed])
    current = rows(db)[0]
    assert current["id"] == original["id"]
    assert current["score"] == 0.75
    assert current["tier"] == "potential"
    assert current["details"] == changed.result.model_dump(mode="json")
    assert db.list_dashboard_jobs()[0]["score"] == 0.75
    handoff = db.list_handoff_jobs()[0]
    assert (handoff["score"], handoff["tier"], handoff["reasons"]) == (0.75, "potential", ["new"])
    assert not db.notification_claim_is_valid(rid, claim["id"], claim["claim_token"])
    queued = db.list_pending_notifications(5)
    assert len(queued) == (0 if suppressed else 1)
    if queued:
        assert (queued[0]["message"], queued[0]["score"]) == ("new", 0.75)
    db.record_notification(current["job_id"], "tech", raw().content_hash)
    persist(raw(), [decision(score=0.8, message="another")])
    assert rows(db, notification_outbox) == []
    assert db.was_notified(current["job_id"], "tech", raw().content_hash)


def test_supersession_preserves_other_profiles_and_old_content(store):
    db, _, _, persist = store
    persist(raw("historical"), [decision()])
    persist(raw(), [decision(), decision(profile="other", score=0.8)])
    # Include a legacy duplicate that predates the invariant.
    existing = next(row for row in rows(db) if row["content_hash"] == raw().content_hash)
    with db.engine.begin() as conn:
        conn.execute(
            insert(match_results).values(**dict(existing, id="legacy", profile_version="0"))
        )
    persist(raw(), [decision(False, 0, version="2")])
    current = [row for row in rows(db) if row["content_hash"] == raw().content_hash]
    tech = [row for row in current if row["profile"] == "tech"]
    assert len(tech) == 1 and tech[0]["profile_version"] == "2"
    assert tech[0]["eligible"] is False
    assert len(rows(db)) == 3  # Historical content plus two current profiles.
    assert db.list_handoff_jobs()[0]["profile"] == "other"
    assert db.list_dashboard_jobs()[0]["score"] == 0.8
    assert [row["profile"] for row in rows(db, notification_outbox)] == ["other"]
    persist(raw(), [decision(False, 0, profile="other")])
    assert_invisible(db)


def test_empty_decisions_do_not_reconcile_and_old_hash_never_surfaces(store):
    db, _, _, persist = store
    persist(raw(), [decision()])
    before, outbox = rows(db), rows(db, notification_outbox)
    persist(raw(), [])
    assert rows(db) == before
    assert rows(db, notification_outbox) == outbox
    persist(raw("changed content"), [])
    assert rows(db) == before
    assert_invisible(db)


@pytest.mark.parametrize("invalid", ["missing", "ineligible", "old_hash", "other_profile"])
def test_pending_queries_reject_legacy_invalid_alerts(store, invalid):
    db, _, rid, persist = store
    persisted = persist(raw(), [decision()])
    with db.engine.begin() as conn:
        if invalid == "missing":
            conn.execute(match_results.delete())
        elif invalid == "ineligible":
            conn.execute(update(match_results).values(eligible=False))
        elif invalid == "old_hash":
            conn.execute(update(match_results).values(content_hash="old"))
        else:
            conn.execute(update(match_results).values(profile="other"))
    assert persisted.job_id
    assert db.list_pending_notifications(5) == []
    assert db.pending_notification_count() == 0
    assert db.claim_pending_notifications(rid, 5) == []
    assert rows(db, notification_outbox) == []


def test_direct_record_match_uses_authoritative_semantics(store):
    db, _, _, persist = store
    persisted = persist(raw(), [decision()])
    original = rows(db)[0]
    db.record_match(persisted.job_id, "1", raw().content_hash, decision(False, 0).result)
    assert rows(db)[0]["id"] == original["id"]
    assert rows(db, notification_outbox) == []
    db.record_match(persisted.job_id, "2", raw().content_hash, decision(False, 0).result)
    assert len(rows(db)) == 1
    assert rows(db)[0]["profile_version"] == "2"
    assert_invisible(db)


def test_reconciliation_rolls_back_with_transaction(store):
    db, _, _, persist = store
    persist(raw(), [decision()])
    before, pending = rows(db), rows(db, notification_outbox)

    def fail_update(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("UPDATE match_results"):
            raise RuntimeError("injected failure after cancellation")

    event.listen(db.engine, "before_cursor_execute", fail_update)
    try:
        with pytest.raises(RuntimeError, match="injected failure"):
            persist(raw(), [decision(False, 0)])
    finally:
        event.remove(db.engine, "before_cursor_execute", fail_update)
    assert rows(db) == before
    assert rows(db, notification_outbox) == pending


@pytest.mark.asyncio
async def test_suppressed_smartrecruiters_backfill_uses_real_storage(store, monkeypatch, request):
    db, _, rid, persist = store
    item = raw().model_copy(
        update={
            "location_raw": "Remote, REMOTE, ca",
            "metadata": {"smartrecruiters": {"country_code": "ca"}},
        }
    )
    persist(item, [decision()])  # A result from the old matching logic.
    db.finish_run(rid, {"sources_succeeded": 1}, [])

    class Runner:
        def __init__(self, *args):
            pass

        async def fetch(self, company):
            return [item]

    def forbidden(*args, **kwargs):
        raise AssertionError("No network or Telegram allowed in this regression")

    monkeypatch.setattr(pipeline, "Storage", lambda *args, **kwargs: db)
    monkeypatch.setattr(
        pipeline,
        "_supports_batch_persistence",
        lambda storage: request.node.callspec.params["store"],
    )
    monkeypatch.setattr(pipeline, "SourceRunner", Runner)
    monkeypatch.setattr(pipeline, "TelegramNotifier", forbidden)
    monkeypatch.setattr(pipeline.httpx.AsyncClient, "send", forbidden)
    report = await pipeline.run_pipeline(
        Settings(
            _env_file=None,
            database_url="sqlite://",
            llm_enabled=False,
            resume_path=None,
            telegram_bot_token=None,
            telegram_chat_id=None,
        ),
        [company()],
        {
            "tech": ProfileConfig(
                name="tech",
                allow_other_job_family=True,
                weights={"title": 1},
                title_terms=["data analyst"],
                domain_terms=[],
                skills=[],
            )
        },
        SearchPreferences(),
        backfill=True,
        suppress_notifications=True,
        run_key="foreign-backfill",
    )
    assert report.errors == []
    assert report.sources_succeeded == 1
    assert report.matches == report.notifications == 0
    assert rows(db)[0]["details"]["filtered_reason"] == "location"
    assert_invisible(db)
    assert rows(db, notification_outbox) == []


def test_bulk_reconciliation_matches_sequential_results(tmp_path):
    snapshots = []
    items = [raw_job(str(index)) for index in range(5)]
    for batched in (False, True):
        db = Storage(f"sqlite:///{tmp_path / f'bulk-{batched}.db'}", create_schema=True)
        cid = db.sync_company(company())
        rid = db.start_run("bulk")

        def persist(decisions):
            entries = [
                (item, db.plan_job(cid, item), value) for item, value in zip(items, decisions)
            ]
            if batched:
                return db.persist_job_decisions_batch(cid, rid, entries)
            return [db.persist_job_decisions(cid, rid, *entry) for entry in entries]

        persist([[decision()] for _ in items])
        result = persist(
            [
                [decision(False, 0)],
                [decision(score=0.7, message="changed")],
                [decision(False, 0, version="2")],
                [],
                [decision(version="2")],
            ]
        )
        snapshots.append(
            (
                semantic_snapshot(db),
                sorted((row["score"], row["message"]) for row in rows(db, notification_outbox)),
                [item.notifications_enqueued for item in result],
                sorted(item["score"] for item in db.list_dashboard_jobs()),
                sorted(item["score"] for item in db.list_handoff_jobs()),
            )
        )
        db.engine.dispose()
    assert snapshots[0] == snapshots[1]


def test_identical_explicit_decision_preserves_claim(store):
    db, _, rid, persist = store
    persist(raw(), [decision()])
    claim = db.claim_pending_notifications(rid, 1)[0]
    persist(raw(), [decision()])
    assert db.notification_claim_is_valid(rid, claim["id"], claim["claim_token"])
    assert rows(db, notification_outbox)[0]["id"] == claim["id"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("score", 0.8),
        ("tier", "potential"),
        ("reasons", ["revised reason"]),
    ],
)
def test_each_result_change_invalidates_pending_payload(store, field, value):
    db, _, rid, persist = store
    persist(raw(), [decision()])
    claim = db.claim_pending_notifications(rid, 1)[0]
    changed = decision()
    setattr(changed.result, field, value)
    persist(raw(), [MatchDecision("1", changed.result, None)])
    assert rows(db)[0]["details"][field] == value
    assert rows(db, notification_outbox) == []
    assert not db.notification_claim_is_valid(rid, claim["id"], claim["claim_token"])


def test_duplicate_batch_decisions_follow_input_order(store):
    db, cid, rid, persist = store
    persist(raw(), [decision()])
    plan = db.plan_job(cid, raw())
    db.persist_job_decisions_batch(
        cid,
        rid,
        [
            (raw(), plan, [decision(score=0.8, message="replacement")]),
            (raw(), plan, [decision(False, 0, version="2")]),
        ],
    )
    assert len(rows(db)) == 1
    assert rows(db)[0]["profile_version"] == "2"
    assert rows(db, notification_outbox) == []
    assert_invisible(db)


def test_batch_fallback_reconciles_existing_match(store, monkeypatch):
    db, cid, rid, persist = store
    persist(raw(), [decision()])
    validate = db._validate_job_plan
    calls = []

    def mismatch_once(*args):
        calls.append(True)
        if len(calls) == 1:
            raise RuntimeError("snapshot mismatch")
        return validate(*args)

    monkeypatch.setattr(db, "_validate_job_plan", mismatch_once)
    db.persist_job_decisions_batch(
        cid,
        rid,
        [
            (raw(), db.plan_job(cid, raw()), [decision(False, 0)]),
        ],
    )
    assert len(calls) == 2
    assert rows(db, notification_outbox) == []
    assert_invisible(db)


@pytest.mark.asyncio
async def test_pipeline_does_not_send_cancelled_in_memory_claim(store, monkeypatch):
    db, _, rid, persist = store
    persisted = persist(raw(), [decision()])
    db.finish_run(rid, {"sources_succeeded": 1}, [])
    claim = db.claim_pending_notifications

    def claim_then_cancel(*args):
        claimed = claim(*args)
        assert len(claimed) == 1
        db.record_match(persisted.job_id, "1", raw().content_hash, decision(False, 0).result)
        return claimed

    class Runner:
        def __init__(self, *args):
            pass

        async def fetch(self, company):
            return [raw()]

    sent = []

    class Notifier:
        def __init__(self, *args):
            pass

        async def send(self, message):
            sent.append(message)

    def forbidden(*args, **kwargs):
        raise AssertionError("Network forbidden")

    monkeypatch.setattr(db, "claim_pending_notifications", claim_then_cancel)
    monkeypatch.setattr(pipeline, "Storage", lambda *args, **kwargs: db)
    monkeypatch.setattr(pipeline, "SourceRunner", Runner)
    monkeypatch.setattr(pipeline, "TelegramNotifier", Notifier)
    monkeypatch.setattr(pipeline.httpx.AsyncClient, "send", forbidden)
    report = await pipeline.run_pipeline(
        Settings(
            _env_file=None,
            database_url="sqlite://",
            llm_enabled=False,
            resume_path=None,
            telegram_bot_token="fake",
            telegram_chat_id="fake",
        ),
        [company()],
        {},
        SearchPreferences(),
        run_key="cancelled-claim",
    )
    assert report.errors == []
    assert report.notifications == 0
    assert "old" not in sent
    assert rows(db, notification_outbox) == []
