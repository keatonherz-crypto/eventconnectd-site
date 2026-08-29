"""Query layer and weekly digest."""

from __future__ import annotations

import json
from datetime import timedelta

from painpoint.clusterer import Clusterer
from painpoint.db import utcnow
from painpoint.queries import corpus_stats, get_cluster, get_trending, search_pains, tally
from painpoint.reporter import Reporter
from tests.conftest import FakeAnthropic, insert_classification, insert_post

INVOICE_PAINS = [
    "I lose track of which invoices I have sent to clients",
    "I keep losing track of which invoices were sent to which client",
    "Losing track of sent client invoices every single month",
]
SCHEDULING_PAIN = "Crew scheduling changes never reach the technicians in the field"

NARRATIVE = {
    "problem": "Freelancers cannot tell which invoices are outstanding.",
    "why_now": "Payment rails got faster but reconciliation did not.",
    "app_concept": "A single ledger fed by email forwarding.",
    "riskiest_assumption": "That people will forward email at all.",
    "competitor_notes": [{"name": "QuickBooks", "complaint": "priced for accountants"}],
}


def build_corpus(db, config):
    """Three paraphrases of one pain plus one unrelated pain, clustered."""
    for i, summary in enumerate(INVOICE_PAINS):
        post_id = f"t3_inv{i}"
        insert_post(db, post_id, title=f"Invoice trouble {i}", days_ago=i + 1, score=10 + i)
        insert_classification(db, post_id, summary=summary, total=20 - i)

    insert_post(db, "t3_sched", title="Scheduling", vertical="trades", subreddit="HVAC")
    insert_classification(db, "t3_sched", summary=SCHEDULING_PAIN, total=12)

    Clusterer(db, config).run()


# -- queries ------------------------------------------------------------


def test_search_pains_ranks_clusters(db, config):
    build_corpus(db, config)

    results = search_pains(db)

    assert len(results) == 2
    assert results[0]["member_count"] == 3
    assert results[0]["cluster_score"] >= results[1]["cluster_score"]


def test_search_pains_filters_by_vertical_and_score(db, config):
    build_corpus(db, config)

    assert all(c["vertical"] == "trades" for c in search_pains(db, vertical="trades"))
    assert search_pains(db, min_score=19)[0]["vertical"] == "events"
    assert search_pains(db, min_score=99) == []


def test_search_pains_filters_by_recency(db, config):
    build_corpus(db, config)

    assert search_pains(db, since=utcnow() - timedelta(days=365))
    assert search_pains(db, since=utcnow() + timedelta(days=1)) == []


def test_get_cluster_returns_evidence_and_links(db, config):
    build_corpus(db, config)
    cluster_id = search_pains(db)[0]["id"]

    cluster = get_cluster(db, cluster_id)

    assert len(cluster["members"]) == 3
    assert all(m["permalink"].startswith("https://") for m in cluster["members"])
    assert cluster["workarounds"] == [("a spreadsheet", 3)]
    assert cluster["competitors"] == [("QuickBooks", 3)]
    # Members come back best-first, so the top ones are worth reading.
    scores = [m["total_score"] for m in cluster["members"]]
    assert scores == sorted(scores, reverse=True)


def test_get_cluster_returns_none_for_unknown_id(db):
    assert get_cluster(db, 999) is None


def test_get_trending_prefers_growth_over_size(db, config):
    # A big flat cluster against a small one that only appeared this month.
    for i in range(6):
        post_id = f"t3_flat{i}"
        insert_post(db, post_id, days_ago=40 if i < 3 else 5)
        insert_classification(db, post_id, summary=INVOICE_PAINS[0], total=15)
    for i in range(2):
        post_id = f"t3_new{i}"
        insert_post(db, post_id, days_ago=3)
        insert_classification(db, post_id, summary=SCHEDULING_PAIN, total=15)
    Clusterer(db, config).run()

    trending = get_trending(db, days=30)

    assert trending[0]["canonical_pain"] == SCHEDULING_PAIN
    assert trending[0]["growth_ratio"] >= trending[1]["growth_ratio"]


def test_get_trending_ignores_clusters_with_no_recent_mentions(db, config):
    insert_post(db, "t3_old", days_ago=200)
    insert_classification(db, "t3_old", summary=INVOICE_PAINS[0])
    Clusterer(db, config).run()

    assert get_trending(db, days=30) == []


def test_tally_counts_and_ignores_empty_values():
    assert tally(["a", "a", "b", None, "", "  ", "null"]) == [("a", 2), ("b", 1)]


def test_corpus_stats_counts_each_stage(db, config):
    build_corpus(db, config)

    stats = corpus_stats(db)

    assert stats["posts"] == 4
    assert stats["classified"] == 4
    assert stats["painpoints"] == 4
    assert stats["clusters"] == 2


# -- digest -------------------------------------------------------------


def test_digest_without_llm_still_carries_the_evidence(db, config):
    build_corpus(db, config)

    markdown = Reporter(db, config, FakeAnthropic(lambda r: "")).build(use_llm=False)

    assert "# Pain point digest" in markdown
    assert "Go read these threads:" in markdown
    assert "https://www.reddit.com/" in markdown
    assert "a spreadsheet (3)" in markdown
    assert "QuickBooks (3)" in markdown
    assert "_not synthesized_" in markdown
    assert "DM ten people" in markdown


def test_digest_with_llm_includes_the_narrative(db, config):
    build_corpus(db, config)
    client = FakeAnthropic(lambda request: json.dumps(NARRATIVE))

    markdown = Reporter(db, config, client).build(use_llm=True)

    assert NARRATIVE["problem"] in markdown
    assert NARRATIVE["app_concept"] in markdown
    assert NARRATIVE["riskiest_assumption"] in markdown
    assert "**QuickBooks** -- priced for accountants" in markdown
    assert client.messages.calls[0]["model"] == config.reporter.synthesis_model


def test_synthesis_failure_degrades_to_a_facts_only_entry(db, config):
    build_corpus(db, config)

    def explode(_request):
        raise RuntimeError("model unavailable")

    markdown = Reporter(db, config, FakeAnthropic(explode)).build(use_llm=True)

    assert "Go read these threads:" in markdown
    assert "_not synthesized_" in markdown


def test_digest_respects_the_per_vertical_cap(db, config):
    distinct_pains = [
        "I lose track of which invoices I have sent to clients",
        "Crew scheduling changes never reach the technicians in the field",
        "Finding a venue that permits outside catering takes weeks of phone calls",
        "Deposit refunds get approved verbally and then nobody records them",
        "Guest dietary requirements arrive by text and never make the final headcount",
        "Vendor certificates of insurance expire without any warning",
    ]
    for i, summary in enumerate(distinct_pains):
        post_id = f"t3_p{i}"
        insert_post(db, post_id, days_ago=1)
        insert_classification(db, post_id, summary=summary)
    Clusterer(db, config).run()
    assert db.scalar("SELECT COUNT(*) FROM clusters") == len(distinct_pains)
    config.reporter.top_n_per_vertical = 2

    markdown = Reporter(db, config, FakeAnthropic(lambda r: "")).build(use_llm=False)

    assert markdown.count("Go read these threads:") == 2


def test_month_over_month_change_is_reported(db, config):
    for i in range(2):
        insert_post(db, f"t3_now{i}", days_ago=3)
        insert_classification(db, f"t3_now{i}", summary=INVOICE_PAINS[0])
    insert_post(db, "t3_then", days_ago=45)
    insert_classification(db, "t3_then", summary=INVOICE_PAINS[1])
    Clusterer(db, config).run()

    markdown = Reporter(db, config, FakeAnthropic(lambda r: "")).build(use_llm=False)

    assert "2 in the last 30 days, up from 1" in markdown


def test_digest_on_an_empty_database_says_what_to_run(db, config):
    markdown = Reporter(db, config, FakeAnthropic(lambda r: "")).build(use_llm=False)
    assert "No clusters yet" in markdown


def test_write_creates_a_dated_file(db, config, tmp_path):
    build_corpus(db, config)

    path = Reporter(db, config, FakeAnthropic(lambda r: "")).write(
        tmp_path / "reports", use_llm=False
    )

    assert path.exists()
    assert path.name.startswith("digest-")
    assert "Pain point digest" in path.read_text()


def test_change_wording_covers_every_direction():
    from painpoint.reporter import _fmt_change

    assert _fmt_change(2, 1) == "2 in the last 30 days, up from 1"
    assert _fmt_change(1, 4) == "1 in the last 30 days, down from 4"
    assert _fmt_change(3, 0) == "3 in the last 30 days, none the month before"
    assert _fmt_change(0, 2) == "none in the last 30 days, 2 the month before"
    assert _fmt_change(0, 0) == "nothing in the last two months"
