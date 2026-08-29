"""Stage 1 filter behaviour."""

from painpoint.heuristics import has_struggle_language, is_bot, stage1_filter, summarize

LONG_COMPLAINT = (
    "Every time I close out a job I have to retype the same line items into "
    "QuickBooks by hand, and then again into the scheduling app. It takes me "
    "about an hour a day and I keep making mistakes on the tax lines."
)


def make(**overrides):
    post = {
        "title": "Double entry between two systems is killing me",
        "body": LONG_COMPLAINT,
        "author": "some_contractor",
        "score": 12,
        "num_comments": 8,
    }
    post.update(overrides)
    return post


def test_detailed_complaint_survives():
    result = stage1_filter(make())
    assert result.keep and result.reason == "kept" and not result.rescued


def test_short_body_dropped():
    assert stage1_filter(make(body="ugh")).reason == "too_short"


def test_link_only_body_dropped():
    assert stage1_filter(make(body="https://example.com/some/article")).reason == "link_only"
    assert (
        stage1_filter(make(body="[read this](https://example.com/a)")).reason == "link_only"
    )


def test_bot_and_deleted_authors_dropped():
    assert stage1_filter(make(author="AutoModerator")).reason == "bot_author"
    assert stage1_filter(make(author="some_helpful_bot")).reason == "bot_author"
    assert stage1_filter(make(author="[deleted]")).reason == "bot_author"
    assert stage1_filter(make(author=None)).reason == "bot_author"


def test_bot_detection_does_not_catch_ordinary_names():
    assert not is_bot("bottlerocket")
    assert not is_bot("u/robotics_fan")


def test_self_promo_dropped_even_when_detailed():
    promo = make(title="Just launched my new app for contractors", body=LONG_COMPLAINT)
    assert stage1_filter(promo).reason == "self_promo"


def test_meme_titles_dropped():
    assert stage1_filter(make(title="[meme] HVAC life")).reason == "meme"


def test_low_engagement_dropped_without_struggle_language():
    quiet = make(
        title="Thoughts on scheduling software",
        body=(
            "Posting to see what the general feeling is about the scheduling tools "
            "on the market at the moment, since there seem to be a lot of them and "
            "opinions vary quite a bit from what I can tell so far."
        ),
        score=0,
        num_comments=0,
    )
    assert stage1_filter(quiet).reason == "low_engagement"


def test_struggle_language_rescues_a_post_nobody_upvoted():
    """A detailed account of a daily grind at zero points is the whole point."""
    result = stage1_filter(make(score=0, num_comments=0))
    assert result.keep and result.rescued


def test_struggle_language_does_not_rescue_a_hard_drop():
    assert stage1_filter(make(body="short", score=0, num_comments=0)).reason == "too_short"


def test_lookup_question_dropped():
    lookup = make(
        title="What time does the supply house open on Saturdays",
        body=(
            "Trying to plan the morning run and the website has not been updated in "
            "a while, so I am asking here instead. Anyone been by recently and knows "
            "the current weekend hours for the counter?"
        ),
    )
    assert stage1_filter(lookup).reason == "factual_question"


def test_struggle_language_detection():
    assert has_struggle_language("I have to redo this every week")
    assert has_struggle_language("we currently track it in a spreadsheet")
    assert not has_struggle_language("here is a link to the manual")


def test_summarize_reports_survival_rate():
    posts = [make(), make(body="tiny"), make(author="AutoModerator"), make(body="x")]
    stats = summarize(posts)
    assert stats.total == 4
    assert stats.kept == 1
    assert stats.survival_rate == 0.25
    assert stats.reasons["too_short"] == 2
    assert "drops by reason" in stats.format()
