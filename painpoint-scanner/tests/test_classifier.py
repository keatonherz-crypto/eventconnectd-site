"""Stage 2: score parsing, batching, persistence."""

from __future__ import annotations

import json
import re

import anthropic
import httpx2
import pytest

from painpoint.classifier import (
    MAX_TOTAL_SCORE,
    Classifier,
    clamp_scores,
    extract_json,
    review_sample,
    total_score,
)
from painpoint.db import Database
from tests.conftest import FakeAnthropic, insert_classification, insert_post


def result_for(post_id, **overrides):
    payload = {
        "id": post_id,
        "is_painpoint": True,
        "pain_summary": f"pain for {post_id}",
        "vertical": "events",
        "current_workaround": "a spreadsheet",
        "competitors": ["Honeybook"],
        "scores": {
            "specificity": 4,
            "workflow_frequency": 5,
            "money_signal": 3,
            "incumbent_gap": 4,
            "buildability": 4,
        },
        "evidence_quote": "I retype everything by hand",
    }
    payload.update(overrides)
    return payload


def ids_in(request):
    """The post ids the prompt actually asked about, in prompt order."""
    prompt = request["messages"][0]["content"]
    return re.findall(r"^id: (\S+)$", prompt, re.MULTILINE)


def echo_responder(skip=(), fail_calls=()):
    """Answer whatever ids the prompt contains, as a real model would.

    `skip` omits results for those ids; `fail_calls` returns unparseable text on
    those 1-based call numbers.
    """
    calls = {"n": 0}

    def respond(request):
        calls["n"] += 1
        if calls["n"] in fail_calls:
            return "this is not json"
        results = [result_for(i) for i in ids_in(request) if i not in skip]
        return json.dumps({"results": results})

    return respond


# -- score handling -----------------------------------------------------


def test_clamp_scores_bounds_every_dimension():
    scores = clamp_scores(
        {
            "specificity": 9,
            "workflow_frequency": -3,
            "money_signal": "4",
            "incumbent_gap": 2.6,
            "buildability": None,
        }
    )
    assert scores == {
        "specificity": 5,
        "workflow_frequency": 0,
        "money_signal": 4,
        "incumbent_gap": 3,
        "buildability": 0,
    }


def test_clamp_scores_handles_missing_object():
    assert total_score(clamp_scores(None)) == 0
    assert total_score(clamp_scores("nonsense")) == 0


def test_total_score_caps_at_the_rubric_maximum():
    assert total_score(clamp_scores({d: 9 for d in clamp_scores({})})) == MAX_TOTAL_SCORE


# -- response parsing ---------------------------------------------------


def test_extract_json_accepts_bare_json():
    assert extract_json('{"results": []}') == {"results": []}


def test_extract_json_strips_markdown_fences():
    assert extract_json('```json\n{"results": [1]}\n```') == {"results": [1]}


def test_extract_json_recovers_from_preamble():
    text = 'Sure, here you go:\n{"results": [{"id": "t3_a"}]}\nHope that helps.'
    assert extract_json(text)["results"][0]["id"] == "t3_a"


def test_extract_json_raises_on_garbage():
    with pytest.raises(ValueError):
        extract_json("no json at all")


# -- the run ------------------------------------------------------------


def test_run_classifies_pending_posts(db, config):
    insert_post(db, "t3_a")
    insert_post(db, "t3_b")
    client = FakeAnthropic(echo_responder())

    stats = Classifier(db, config, client).run()

    assert stats.written == 2
    assert stats.painpoints == 2
    row = db.query_one("SELECT * FROM classifications WHERE post_id = ?", ("t3_a",))
    assert row["pain_summary"] == "pain for t3_a"
    assert row["total_score"] == 20
    assert Database.load_json(row["competitors"]) == ["Honeybook"]
    assert row["model"] == config.classifier.model


def test_total_is_recomputed_not_taken_from_the_model(db, config):
    """The model's arithmetic is never trusted; a bogus total must be ignored."""
    insert_post(db, "t3_a")
    lying = result_for("t3_a", total_score=25, scores={d: 1 for d in [
        "specificity", "workflow_frequency", "money_signal", "incumbent_gap", "buildability"
    ]})
    client = FakeAnthropic(lambda request: json.dumps({"results": [lying]}))

    Classifier(db, config, client).run()

    assert db.scalar("SELECT total_score FROM classifications") == 5


def test_only_stage1_survivors_are_sent(db, config):
    insert_post(db, "t3_keep", stage1_keep=1)
    insert_post(db, "t3_drop", stage1_keep=0)
    client = FakeAnthropic(echo_responder())

    classifier = Classifier(db, config, client)
    assert [p["id"] for p in classifier.pending_posts()] == ["t3_keep"]

    classifier.run()
    assert db.scalar("SELECT COUNT(*) FROM classifications") == 1


def test_already_classified_posts_are_not_resent(db, config):
    insert_post(db, "t3_a")
    insert_classification(db, "t3_a")
    client = FakeAnthropic(echo_responder())

    stats = Classifier(db, config, client).run()

    assert stats.posts_sent == 0
    assert client.messages.calls == []


def test_posts_are_sent_in_batches(db, config):
    for i in range(5):
        insert_post(db, f"t3_{i}")
    config.classifier.batch_size = 2
    client = FakeAnthropic(echo_responder())

    stats = Classifier(db, config, client).run()

    assert stats.batches == 3
    assert stats.written == 5


def test_a_failed_batch_does_not_lose_the_rest(db, config):
    for i in range(4):
        insert_post(db, f"t3_{i}")

    stats = Classifier(db, config, FakeAnthropic(echo_responder(fail_calls=(1,)))).run()

    assert len(stats.errors) == 1
    assert stats.written == 2


def test_missing_results_are_reported_and_left_for_retry(db, config):
    insert_post(db, "t3_a")
    insert_post(db, "t3_b")
    client = FakeAnthropic(echo_responder(skip=("t3_b",)))

    stats = Classifier(db, config, client).run()

    assert stats.written == 1
    assert stats.missing == ["t3_b"]
    assert Classifier(db, config, client).pending_posts()[0]["id"] == "t3_b"


def test_hallucinated_ids_are_discarded(db, config):
    insert_post(db, "t3_a")
    client = FakeAnthropic(
        lambda request: json.dumps({"results": [result_for("t3_never_collected")]})
    )

    Classifier(db, config, client).run()

    assert db.scalar("SELECT COUNT(*) FROM classifications") == 0


def test_top_comments_are_included_in_the_prompt(db, config):
    """Competitors surface in replies more often than in the post itself."""
    insert_post(db, "t3_a")
    db.execute(
        "INSERT INTO comments (id, post_id, body, author, score, created_utc, fetched_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("t1_c", "t3_a", "We tried Honeybook and it was awful", "u", 30, None, None),
    )

    prompt = Classifier(db, config, FakeAnthropic(lambda r: "")).build_prompt(
        db.query("SELECT * FROM posts")
    )

    assert "Honeybook" in prompt
    assert "t3_a" in prompt


def test_body_is_truncated_before_sending(db, config):
    insert_post(db, "t3_a", body="x" * 5000)
    config.classifier.max_body_chars = 100

    prompt = Classifier(db, config, FakeAnthropic(lambda r: "")).build_prompt(
        db.query("SELECT * FROM posts")
    )

    assert "x" * 100 in prompt
    assert "x" * 101 not in prompt


def test_structured_output_is_requested_then_dropped_if_rejected(db, config):
    """A model without output_config support must degrade, not fail every batch."""
    insert_post(db, "t3_a")
    calls = {"n": 0}

    def respond(request):
        calls["n"] += 1
        if calls["n"] == 1:
            assert "output_config" in request
            raise anthropic.BadRequestError(
                "output_config unsupported",
                response=httpx2.Response(
                    400, request=httpx2.Request("POST", "https://api.anthropic.com")
                ),
                body=None,
            )
        assert "output_config" not in request
        return json.dumps({"results": [result_for(i) for i in ids_in(request)]})

    classifier = Classifier(db, config, FakeAnthropic(respond))
    classifier.run()

    assert classifier.structured_output is False
    assert db.scalar("SELECT COUNT(*) FROM classifications") == 1


def test_review_sample_renders_scores_for_hand_checking(db, config):
    insert_post(db, "t3_a", title="Double entry every single job")
    insert_classification(db, "t3_a")

    output = review_sample(db, 10)

    assert "Double entry every single job" in output
    assert "money_signal=4" in output
    assert "https://www.reddit.com/" in output


def test_review_sample_handles_an_empty_database(db):
    assert review_sample(db) == "No classifications yet."
