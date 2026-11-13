"""Regression tests for depth-shortening races in the citation crawler.

The bug being pinned down: a page is claimed at some depth D, and while its
request is in flight a late shorter path lowers it to d < D.  If the response
is then expanded from the stale D, its citations are registered too deep, and
papers that should now be in range are recorded ``out_of_range`` and never
collected (in the demo corpus: X at 2, Y at 3, Z at 4, but Y leaked to 5).

Every test below uses only the public surface: the API for end-to-end cases
and ``Database`` operations in the exact order the two worker slots perform
them for the deterministic case.
"""

import time

import pytest
from fastapi.testclient import TestClient

from app.db import (
    DONE,
    FETCHING,
    OUT_OF_RANGE,
    PENDING,
    Database,
)


def _crawl_until_settled(client: TestClient, job_id: int, timeout: float = 20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/jobs/{job_id}").json()
        if job["status"] != "running":
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not settle within {timeout}s")


def _papers_by_name(client: TestClient, job_id: int) -> dict:
    return {
        p["url"].rsplit("/", 1)[-1]: p
        for p in client.get(f"/jobs/{job_id}/papers").json()["papers"]
    }


# --------------------------------------------------------------- unit level


def test_shortened_in_flight_page_expands_from_current_depth(tmp_path):
    """Deterministic replay of the two-slot interleaving.

    Slot A holds slow.html (depth 1) while slot B races down c1->c2->c3->X.
    X is claimed at depth 4; slow then lands and lowers X to 2; X's own
    response must use depth 2, not the depth captured when it was claimed.
    """
    db = Database(str(tmp_path / "race.db"))
    origin = "http://papers"
    seed = origin + "/p0"
    job = db.create_job(seed, origin, 4)

    def advance(url, title, links):
        paper = db.claim_next(job)
        assert paper is not None and paper["url"] == url
        db.record_success(job, paper["id"], paper["url"], title, links, 200)
        return paper

    advance(seed, "P0", [origin + "/slow", origin + "/c1"])
    slow = db.claim_next(job)  # slot A is now blocked on the slow request
    assert slow["url"] == origin + "/slow"

    advance(origin + "/c1", "C1", [origin + "/c2"])
    advance(origin + "/c2", "C2", [origin + "/c3"])
    advance(origin + "/c3", "C3", [origin + "/X"])

    x_claim = db.claim_next(job)  # slot B claims X at the long-chain depth
    assert x_claim["url"] == origin + "/X"
    assert x_claim["depth"] == 4

    # the late short path lands while X's response is still in flight
    db.record_success(job, slow["id"], slow["url"], "Slow", [origin + "/X"], 200)
    x_row = next(p for p in db.list_papers(job) if p["url"] == origin + "/X")
    assert (x_row["depth"], x_row["status"]) == (2, FETCHING)

    # X's in-flight response: its citation of Y must be registered at 3,
    # not at the stale claim depth + 1 = 5.
    db.record_success(job, x_claim["id"], x_claim["url"], "X", [origin + "/Y"], 200)
    papers = {p["url"].rsplit("/", 1)[-1]: p for p in db.list_papers(job)}
    assert (papers["X"]["depth"], papers["X"]["status"]) == (2, DONE)
    assert (papers["Y"]["depth"], papers["Y"]["status"]) == (3, PENDING)

    # Y is fetched, then Z at the frontier, and the job settles completed.
    y = db.claim_next(job)
    assert (y["url"], y["depth"]) == (origin + "/Y", 3)
    db.record_success(job, y["id"], y["url"], "Y", [origin + "/Z"], 200)
    z = db.claim_next(job)
    assert (z["url"], z["depth"]) == (origin + "/Z", 4)
    db.record_success(job, z["id"], z["url"], "Z", [], 200)

    assert db.get_job(job)["status"] == "completed"
    assert db.list_failures(job) == []
    assert db.claim_next(job) is None


def test_already_fetched_children_follow_depth_decrease(tmp_path):
    """A depth decrease propagates through stored edges of fetched pages:
    out_of_range children come back into range and are queued, done pages are
    not refetched, and the propagation terminates on citation cycles."""
    db = Database(str(tmp_path / "prop.db"))
    origin = "http://papers"
    seed = origin + "/p0"
    job = db.create_job(seed, origin, 4)

    def claim(url):
        paper = db.claim_next(job)
        assert paper is not None and paper["url"] == url
        return paper

    def record(paper, links):
        db.record_success(job, paper["id"], paper["url"], paper["url"], links, 200)

    def get(url):
        return next(p for p in db.list_papers(job) if p["url"] == url)

    p0 = claim(seed)
    record(p0, [origin + "/c1", origin + "/slow"])
    # both slots are busy: the chain is fetched while slow.html is in flight
    c1, slow = claim(origin + "/c1"), claim(origin + "/slow")

    record(c1, [origin + "/c2"])
    record(claim(origin + "/c2"), [origin + "/c3"])
    record(claim(origin + "/c3"), [origin + "/X"])
    x = claim(origin + "/X")  # long chain: X is claimed at depth 4
    assert x["depth"] == 4
    record(x, [origin + "/Y"])  # Y overshoots the budget at depth 5
    assert (get(origin + "/Y")["depth"], get(origin + "/Y")["status"]) == (5, OUT_OF_RANGE)

    # the late short path lands after X was fetched: X 4 -> 2, then the
    # decrease propagates through X's stored edge and Y 5 -> 3 becomes pending
    record(slow, [origin + "/X"])
    assert (get(origin + "/X")["depth"], get(origin + "/X")["status"]) == (2, DONE)
    y = get(origin + "/Y")
    assert (y["depth"], y["status"]) == (3, PENDING)
    assert len(db.list_citations(job, src=slow["url"])) == 1  # recorded once

    # backfilled Y is fetched (X is not refetched), and its cycle back to X
    # must terminate without changing the already-shortest X
    y_paper = claim(origin + "/Y")
    record(y_paper, [origin + "/Z", origin + "/X"])
    assert (get(origin + "/X")["depth"], get(origin + "/X")["status"]) == (2, DONE)
    z = claim(origin + "/Z")
    assert z["depth"] == 4
    record(z, [])

    assert db.get_job(job)["status"] == "completed"
    assert db.claim_next(job) is None
    # every URL registered exactly once: p0, c1, slow, c2, c3, X, Y, Z
    assert len(db.list_papers(job)) == 8
    assert all(p["status"] != FETCHING for p in db.list_papers(job))


# --------------------------------------------------------- end-to-end tests


def test_late_short_path_backfills_y_and_z(site, app_module):
    """The documented demo corpus: X must end at depth 2, Y at 3, Z at 4."""
    with TestClient(app_module.app) as client:
        created = client.post("/jobs", json={"seed_url": site, "max_depth": 4})
        assert created.status_code == 201, created.text
        job_id = created.json()["id"]

        job = _crawl_until_settled(client, job_id)
        papers = _papers_by_name(client, job_id)

        assert papers["X.html"]["depth"] == 2
        assert papers["Y.html"]["depth"] == 3
        assert papers["Z.html"]["depth"] == 4
        assert papers["Y.html"]["status"] == "done"
        assert papers["Z.html"]["status"] == "done"
        assert all(p["status"] != OUT_OF_RANGE for p in papers.values())

        # broken.html fails once, so the job must not claim completion
        assert job["status"] == "failed"
        assert job["counts"] == {"done": 13, "failed": 1, "external": 1, "non_html": 1}
        assert job["papers_by_depth"] == {"0": 1, "1": 3, "2": 6, "3": 3, "4": 3}

        failures = client.get(f"/jobs/{job_id}/failures").json()["failures"]
        assert [(f["url"].rsplit("/", 1)[-1], f["http_status"]) for f in failures] == [
            ("broken.html", 500)
        ]

        # retry re-queues only the failed page and completes the job
        retried = client.post(f"/jobs/{job_id}/retry")
        assert retried.status_code == 200
        assert retried.json()["requeued"] == 1
        job = _crawl_until_settled(client, job_id)
        assert job["status"] == "completed"
        assert job["counts"]["done"] == 14
        assert client.get(f"/jobs/{job_id}/failures").json()["failures"] == []
        assert len(client.get(f"/jobs/{job_id}/papers").json()["papers"]) == 16


def test_two_slots_fetch_concurrently(site, app_module):
    """A slow page must not block the other slot: the whole long chain is
    fetched while slow.html is still in flight."""
    with TestClient(app_module.app) as client:
        job_id = client.post("/jobs", json={"seed_url": site, "max_depth": 4}).json()["id"]
        _crawl_until_settled(client, job_id)

        citations = {
            (c["src_url"].rsplit("/", 1)[-1], c["dst_url"].rsplit("/", 1)[-1])
            for c in client.get(f"/jobs/{job_id}/citations").json()["citations"]
        }
        # the long chain reached X and the slow page also cites X
        assert ("c3.html", "X.html") in citations
        assert ("slow.html", "X.html") in citations
        papers = _papers_by_name(client, job_id)
        assert papers["slow.html"]["status"] == DONE
        assert papers["X.html"]["depth"] == 2


def test_restart_resumes_running_job(site, app_module, tmp_path, monkeypatch):
    """Simulated restart: in-flight pages go back to pending, the job resumes
    and nothing is registered or fetched twice."""
    db_path = tmp_path / "restart.db"

    with TestClient(app_module.app) as client:
        job_id = client.post("/jobs", json={"seed_url": site, "max_depth": 4}).json()["id"]
        # wait for the exact bug interleaving: X claimed at depth 4 on the long
        # chain and still in flight, slow.html done and having lowered X to 2
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            papers = _papers_by_name(client, job_id)
            x = papers.get("X.html", {})
            if x.get("status") == FETCHING and x.get("depth") == 2:
                break
            time.sleep(0.02)
        else:
            pytest.fail("X never entered fetching at a lowered depth")

        # X was claimed at 4: prove the stale depth race was live before the kill
        assert any(p["status"] == FETCHING for p in papers.values())

        # the process dies while X is in flight; nothing about X is recorded
        interrupted = app_module.db.list_papers(job_id)
        assert any(
            p["url"].endswith("/X.html") and p["status"] == FETCHING
            for p in interrupted
        )
        done_before = {p["url"] for p in interrupted if p["status"] == DONE}

    # a brand-new process over the same database file
    monkeypatch.setenv("DATABASE_PATH", app_module.DB_PATH)
    import importlib

    import app.main as reloaded

    reloaded = importlib.reload(reloaded)
    with TestClient(reloaded.app) as client:
        job = _crawl_until_settled(client, job_id, timeout=30)
        assert job["status"] == "failed"  # broken.html still fails once
        papers = _papers_by_name(client, job_id)
        assert papers["slow.html"]["status"] == DONE
        assert papers["X.html"]["depth"] == 2
        assert papers["Y.html"]["depth"] == 3
        assert papers["Z.html"]["depth"] == 4

        citations = client.get(f"/jobs/{job_id}/citations").json()["citations"]
        keys = {(c["src_url"], c["dst_url"]) for c in citations}
        assert len(keys) == len(citations)  # no duplicate edges
        # already-finished pages are still done exactly once
        assert done_before <= {p["url"] for p in papers.values() if p["status"] == DONE}

        # a retry still finishes the job cleanly after the restart
        client.post(f"/jobs/{job_id}/retry")
        assert _crawl_until_settled(client, job_id)["status"] == "completed"


def test_api_docs_and_validation(app_module):
    with TestClient(app_module.app) as client:
        assert client.get("/docs").status_code == 200
        spec = client.get("/openapi.json").json()
        assert {
            "/jobs",
            "/jobs/{job_id}",
            "/jobs/{job_id}/retry",
            "/jobs/{job_id}/failures",
            "/jobs/{job_id}/papers",
            "/jobs/{job_id}/citations",
        } <= set(spec["paths"])

        assert client.post("/jobs", json={"seed_url": "not-a-url", "max_depth": 4}).status_code == 422
        assert client.post("/jobs", json={"seed_url": "http://x/p0", "max_depth": 5}).status_code == 422
        assert client.get("/jobs/999").status_code == 404
