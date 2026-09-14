import assert from "node:assert/strict";
import { test } from "node:test";
import { fetchMatchedJobs, normalizeJob } from "../functions/_lib/supabase.js";
import { onRequestGet as getJobs } from "../functions/api/jobs/index.js";
import { onRequestGet as getDashboard } from "../functions/api/dashboard.js";

const env = { SUPABASE_URL: "https://example.invalid", SUPABASE_SERVICE_ROLE_KEY: "fake" };

function job(id = "current", matches) {
  return {
    id, title: "Analyst", location_raw: "USA", canonical_url: "https://example.invalid/job",
    first_seen_at: "2026-09-01T00:00:00Z", source_posted_at: null, status: "active",
    content_hash: "current-hash",
    companies: { name: "PSI CRO", industry: "clinical", ats_type: "smartrecruiters" },
    applications: [],
    match_results: matches || [
      { job_id: id, content_hash: "current-hash", score: 0.8, eligible: true },
    ],
  };
}

function staleJob(id = "stale") {
  return job(id, [
    { job_id: id, content_hash: "old-hash", score: 0.99, eligible: true },
    { job_id: id, content_hash: "current-hash", score: 0, eligible: false },
  ]);
}

function mockPages(t, pages) {
  const urls = [];
  t.mock.method(globalThis, "fetch", async (input, init) => {
    const url = new URL(input);
    assert.equal(url.origin, env.SUPABASE_URL);
    assert.equal(url.pathname, "/rest/v1/jobs");
    assert.equal(init.method || "GET", "GET");
    assert.ok(urls.length < pages.length, "unexpected request / pagination did not terminate");
    const page = pages[urls.length];
    urls.push(url);
    return Response.json(page);
  });
  return urls;
}

test("historical eligible plus current ineligible does not qualify", () => {
  assert.equal(normalizeJob(staleJob()), null);
  // The REST eligible filter normally omits the ineligible child entirely.
  const row = staleJob();
  row.match_results = row.match_results.filter((match) => match.eligible);
  assert.equal(normalizeJob(row), null);
});

test("only current eligible scores count, across profiles without version ordering", () => {
  const row = staleJob();
  row.match_results.push(
    { job_id: row.id, content_hash: row.content_hash, eligible: true, score: 0.65,
      profile: "one", profile_version: "99" },
    { job_id: row.id, content_hash: row.content_hash, eligible: true, score: 0.8,
      profile: "two", profile_version: "1" },
  );
  assert.equal(normalizeJob(row).score, 0.8);
});

test("current eligible job preserves public response shape and metadata", () => {
  assert.deepEqual(normalizeJob(job()), {
    id: "current", title: "Analyst", location_raw: "USA",
    canonical_url: "https://example.invalid/job",
    first_seen_at: "2026-09-01T00:00:00Z", source_posted_at: null, status: "active",
    company: "PSI CRO", industry: "clinical", source: "smartrecruiters", score: 0.8,
    stage: "recommended", notes: null, first_applied_at: null, first_interview_at: null,
    application_updated_at: null,
  });
});

for (const [name, change] of [
  ["wrong job_id", (row) => { row.match_results[0].job_id = "another"; }],
  ["missing parent hash", (row) => { delete row.content_hash; }],
  ["empty parent hash", (row) => { row.content_hash = ""; }],
  ["non-string parent hash", (row) => { row.content_hash = 123; }],
  ["missing child hash", (row) => { delete row.match_results[0].content_hash; }],
  ["ineligible child", (row) => { row.match_results[0].eligible = false; }],
  ["non-boolean eligibility", (row) => { row.match_results[0].eligible = "true"; }],
  ["no matches", (row) => { row.match_results = []; }],
]) {
  test(`${name} cannot qualify`, () => {
    const row = job();
    change(row);
    assert.equal(normalizeJob(row), null);
  });
}

test("stale first page does not consume limit; current scores determine final ordering", async (t) => {
  const lower = job("lower");
  lower.match_results[0].score = 0.7;
  lower.match_results.push({ job_id: lower.id, content_hash: "old", eligible: true, score: 1 });
  const urls = mockPages(t, [[staleJob("a"), staleJob("b")], [lower, job("higher")]]);
  const rows = await fetchMatchedJobs(env, 30, 2);
  assert.deepEqual(rows.map((row) => [row.id, row.score]), [["higher", 0.8], ["lower", 0.7]]);
  assert.deepEqual(urls.map((url) => url.searchParams.get("offset")), ["0", "2"]);
  for (const url of urls) {
    const params = url.searchParams;
    assert.equal(params.get("order"), "first_seen_at.desc,id.asc");
    assert.equal(params.get("limit"), "2");
    assert.equal(params.get("status"), "eq.active");
    assert.equal(params.get("match_results.eligible"), "eq.true");
    assert.match(params.get("select"), /status,content_hash,companies/);
    assert.match(params.get("select"), /match_results!inner\(job_id,content_hash,score,eligible\)/);
    assert.equal(params.get("first_seen_at"), urls[0].searchParams.get("first_seen_at"));
  }
});

test("short pages advance by raw count and empty page terminates", async (t) => {
  const urls = mockPages(t, [[staleJob()], [job()], []]);
  assert.equal((await fetchMatchedJobs(env, 30, 3)).length, 1);
  assert.deepEqual(urls.map((url) => url.searchParams.get("offset")), ["0", "1", "2"]);
});

test("empty source terminates immediately", async (t) => {
  const urls = mockPages(t, [[]]);
  assert.deepEqual(await fetchMatchedJobs(env, 30, 2), []);
  assert.equal(urls.length, 1);
});

test("invalid or zero limits do not fetch", async (t) => {
  mockPages(t, []);
  for (const limit of [0, -1, NaN, Infinity, 0.5]) {
    assert.deepEqual(await fetchMatchedJobs(env, 30, limit), []);
  }
});

test("jobs handler excludes stale candidates and retains current candidates", async (t) => {
  mockPages(t, [[staleJob(), job()], []]);
  const response = await getJobs({ env, request: new Request("https://app.invalid/api/jobs?limit=2") });
  assert.equal(response.status, 200);
  assert.deepEqual((await response.json()).jobs.map((row) => row.id), ["current"]);
});

test("dashboard KPIs, queue and all groups exclude stale-only candidates", async (t) => {
  const stale = staleJob();
  stale.companies = { name: "Foreign", industry: "stale-industry", ats_type: "stale-source" };
  stale.applications = [{ stage: "interview", first_applied_at: "2026-09-01",
    first_interview_at: "2026-09-02" }];
  mockPages(t, [[stale, job()], []]);
  const response = await getDashboard({ env, request: new Request("https://app.invalid/api/dashboard") });
  assert.equal(response.status, 200);
  const body = await response.json();
  assert.deepEqual(body.kpis, {
    recommended: 1, applied: 0, interviews: 0, apply_rate: 0, interview_rate: 0, total_rate: 0,
  });
  assert.deepEqual(body.queue.map((row) => row.id), ["current"]);
  for (const [field, key, value] of [
    ["industries", "industry", "clinical"], ["sources", "source", "smartrecruiters"],
    ["stages", "stage", "recommended"],
  ]) {
    assert.deepEqual(body[field], [{ [key]: value, recommended: 1, applied: 0, interviews: 0 }]);
  }
});
