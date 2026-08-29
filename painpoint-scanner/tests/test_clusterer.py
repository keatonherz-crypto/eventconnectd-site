"""Clustering, embeddings and cluster ranking."""

from __future__ import annotations

from datetime import timedelta

from painpoint.clusterer import (
    RECENCY_FULL_DAYS,
    Clusterer,
    cluster_score,
    recency_weight,
)
from painpoint.db import utcnow
from painpoint.embeddings import HashingEmbedder, cosine, get_embedder, tokenize
from tests.conftest import insert_classification, insert_post

INVOICE_PAINS = [
    "I lose track of which invoices I have sent to clients",
    "I keep losing track of which invoices were sent to which client",
    "Losing track of sent client invoices every single month",
]
SCHEDULING_PAIN = "Crew scheduling changes never reach the technicians in the field"


# -- embeddings ---------------------------------------------------------


def test_hashing_embeddings_are_deterministic():
    """Vectors are cached in the database, so they must be stable across runs."""
    first = HashingEmbedder().embed(["I lose track of invoices"])[0]
    second = HashingEmbedder().embed(["I lose track of invoices"])[0]
    assert first == second


def test_hashing_embeddings_are_normalized():
    vector = HashingEmbedder().embed(["some pain about invoices"])[0]
    assert abs(sum(v * v for v in vector) - 1.0) < 1e-9


def test_tokenizer_drops_stopwords_and_adds_char_ngrams():
    tokens = tokenize("I lose track of the invoices")
    assert "lose" in tokens
    assert "i" not in tokens and "the" not in tokens
    # Character grams are what let inflected forms of a word overlap at all.
    assert "^los" in tokens
    assert set(tokenize("losing")) & set(tokens)


def test_paraphrases_score_higher_than_unrelated_text():
    embedder = HashingEmbedder()
    a, b, c = embedder.embed([INVOICE_PAINS[0], INVOICE_PAINS[1], SCHEDULING_PAIN])
    assert cosine(a, b) > embedder.default_threshold
    assert cosine(a, c) < embedder.default_threshold


def test_cosine_handles_degenerate_input():
    assert cosine([], [1.0]) == 0.0
    assert cosine([0.0, 0.0], [1.0, 0.0]) == 0.0


def test_unknown_backend_is_rejected():
    try:
        get_embedder("word2vec")
    except ValueError as exc:
        assert "word2vec" in str(exc)
    else:
        raise AssertionError("expected ValueError")


# -- ranking maths ------------------------------------------------------


def test_recency_weight_is_full_inside_the_window():
    now = utcnow()
    assert recency_weight(now, now) == 1.0
    assert recency_weight(now - timedelta(days=RECENCY_FULL_DAYS - 1), now) == 1.0


def test_recency_weight_decays_after_the_window():
    now = utcnow()
    weight = recency_weight(now - timedelta(days=RECENCY_FULL_DAYS + 45), now)
    assert 0.49 < weight < 0.51
    assert recency_weight(None) == 0.0


def test_cluster_score_rewards_repetition():
    """Forty mentions of a mediocre pain beat one mention of a great one."""
    lone_great = cluster_score([25], 1, 1.0)
    repeated_good = cluster_score([18] * 40, 40, 1.0)
    assert repeated_good > lone_great


def test_cluster_score_is_zero_without_members():
    assert cluster_score([], 0, 1.0) == 0.0


# -- the clustering run -------------------------------------------------


def seed(db, summaries, vertical="events", days_ago=1, total=18):
    for i, summary in enumerate(summaries):
        post_id = f"t3_{vertical}_{i}_{days_ago}"
        insert_post(db, post_id, vertical=vertical, days_ago=days_ago)
        insert_classification(db, post_id, summary=summary, total=total)


def test_paraphrases_land_in_one_cluster(db, config):
    seed(db, INVOICE_PAINS)

    stats = Clusterer(db, config).run()

    assert stats.painpoints == 3
    assert stats.clusters == 1
    cluster = db.query_one("SELECT * FROM clusters")
    assert cluster["member_count"] == 3
    assert cluster["canonical_pain"] in INVOICE_PAINS


def test_distinct_pains_stay_apart(db, config):
    seed(db, [INVOICE_PAINS[0], SCHEDULING_PAIN])

    Clusterer(db, config).run()

    assert db.scalar("SELECT COUNT(*) FROM clusters") == 2


def test_verticals_never_merge(db, config):
    """Shared vocabulary across industries is not the same problem."""
    seed(db, [INVOICE_PAINS[0]], vertical="events")
    seed(db, [INVOICE_PAINS[1]], vertical="trades")

    Clusterer(db, config).run()

    verticals = {r["vertical"] for r in db.query("SELECT vertical FROM clusters")}
    assert verticals == {"events", "trades"}


def test_non_painpoints_are_excluded(db, config):
    insert_post(db, "t3_a")
    insert_classification(db, "t3_a", summary="not really a pain", is_painpoint=0)

    stats = Clusterer(db, config).run()

    assert stats.painpoints == 0
    assert stats.clusters == 0


def test_embeddings_are_cached_between_runs(db, config):
    seed(db, INVOICE_PAINS)
    clusterer = Clusterer(db, config)

    first = clusterer.run()
    second = clusterer.run()

    assert first.embedded == 3 and first.cached == 0
    assert second.embedded == 0 and second.cached == 3


def test_recompute_replaces_previous_clusters(db, config):
    seed(db, INVOICE_PAINS)
    Clusterer(db, config).run()
    first_ids = [r["id"] for r in db.query("SELECT id FROM clusters")]

    Clusterer(db, config).run()

    rows = db.query("SELECT id FROM clusters")
    assert len(rows) == 1
    assert rows[0]["id"] not in first_ids
    assert db.scalar("SELECT COUNT(*) FROM cluster_members") == 1 * 3


def test_month_over_month_counts_are_recorded(db, config):
    seed(db, INVOICE_PAINS[:2], days_ago=5)
    seed(db, INVOICE_PAINS[2:], days_ago=45)

    Clusterer(db, config).run()

    cluster = db.query_one("SELECT * FROM clusters")
    assert cluster["this_month_count"] == 2
    assert cluster["prev_month_count"] == 1


def test_canonical_label_is_the_highest_scoring_member(db, config):
    insert_post(db, "t3_low", days_ago=1)
    insert_classification(db, "t3_low", summary=INVOICE_PAINS[0], total=8)
    insert_post(db, "t3_high", days_ago=1)
    insert_classification(db, "t3_high", summary=INVOICE_PAINS[1], total=24)

    Clusterer(db, config).run()

    assert db.scalar("SELECT canonical_pain FROM clusters") == INVOICE_PAINS[1]


def test_threshold_from_config_overrides_the_backend_default(db, config):
    config.clusterer.similarity_threshold = 0.999
    seed(db, INVOICE_PAINS)

    clusterer = Clusterer(db, config)
    assert clusterer.threshold == 0.999
    clusterer.run()

    assert db.scalar("SELECT COUNT(*) FROM clusters") == 3


def test_empty_database_produces_no_clusters(db, config):
    stats = Clusterer(db, config).run()
    assert stats.clusters == 0
    assert "clusters:" in stats.format()
