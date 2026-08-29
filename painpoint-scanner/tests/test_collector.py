"""Collector sweep, driven against a fake Reddit client."""

from __future__ import annotations

import time

from painpoint.collector import Collector

LONG_COMPLAINT = (
    "Every time I book a venue I have to re-key the same details into three "
    "different places and then chase the client for a signature by email. It "
    "takes me most of an afternoon and something always gets missed."
)


class FakeAuthor:
    def __init__(self, name):
        self.name = name


class FakeComment:
    def __init__(self, cid, body, score=5):
        self.id = cid
        self.fullname = f"t1_{cid}"
        self.body = body
        self.author = FakeAuthor("commenter")
        self.score = score
        self.created_utc = time.time()


class FakeComments(list):
    def replace_more(self, limit=0):
        self.replaced = True


class FakeSubmission:
    def __init__(
        self,
        sid,
        title=LONG_COMPLAINT[:40],
        body=LONG_COMPLAINT,
        score=20,
        num_comments=6,
        author="planner",
        age_hours=1.0,
        comments=(),
    ):
        self.id = sid
        self.fullname = f"t3_{sid}"
        self.title = title
        self.selftext = body
        self.score = score
        self.num_comments = num_comments
        self.author = FakeAuthor(author) if author else None
        self.created_utc = time.time() - age_hours * 3600
        self.permalink = f"/r/eventplanning/comments/{sid}/"
        self.comments = FakeComments(comments)


class FakeSubreddit:
    def __init__(self, new_items, search_items=None, fail=False):
        self._new = new_items
        self._search = search_items or []
        self._fail = fail
        self.searches = []

    def new(self, limit=None):
        if self._fail:
            raise RuntimeError("private subreddit")
        return list(self._new)[:limit]

    def search(self, query, sort=None, time_filter=None, limit=None):
        self.searches.append(query)
        return list(self._search)[:limit]


class FakeReddit:
    def __init__(self, subs):
        self.subs = subs

    def subreddit(self, name):
        return self.subs[name]


def test_sweep_stores_posts_and_comments(db, config):
    submission = FakeSubmission(
        "aaa", comments=[FakeComment("c1", "We use Honeybook and hate it")]
    )
    reddit = FakeReddit(
        {
            "eventplanning": FakeSubreddit([submission]),
            "HVAC": FakeSubreddit([]),
        }
    )

    stats = Collector(db, config, reddit).sweep(sweep_index=0)

    assert stats.posts_written == 1
    assert stats.posts_kept == 1
    assert stats.comments_written == 1

    post = db.query_one("SELECT * FROM posts")
    assert post["id"] == "t3_aaa"
    assert post["vertical"] == "events"
    assert post["stage1_keep"] == 1
    assert post["permalink"].startswith("https://www.reddit.com/")

    comment = db.query_one("SELECT * FROM comments")
    assert comment["post_id"] == "t3_aaa"
    assert "Honeybook" in comment["body"]


def test_comments_are_not_fetched_for_filtered_posts(db, config):
    """Comment fetching is a request budget; spend it only on live candidates."""
    junk = FakeSubmission("bbb", body="k", comments=[FakeComment("c9", "irrelevant")])
    reddit = FakeReddit(
        {"eventplanning": FakeSubreddit([junk]), "HVAC": FakeSubreddit([])}
    )

    stats = Collector(db, config, reddit).sweep(sweep_index=0)

    assert stats.posts_written == 1
    assert stats.posts_kept == 0
    assert stats.comments_written == 0
    assert db.query_one("SELECT stage1_reason FROM posts")["stage1_reason"] == "too_short"


def test_posts_outside_the_window_are_skipped(db, config):
    old = FakeSubmission("ccc", age_hours=config.collector.window_hours + 5)
    reddit = FakeReddit(
        {"eventplanning": FakeSubreddit([old]), "HVAC": FakeSubreddit([])}
    )

    stats = Collector(db, config, reddit).sweep(sweep_index=0)

    assert stats.posts_seen == 1
    assert stats.posts_written == 0


def test_duplicate_across_listings_written_once(db, config):
    submission = FakeSubmission("ddd")
    reddit = FakeReddit(
        {
            "eventplanning": FakeSubreddit([submission], search_items=[submission]),
            "HVAC": FakeSubreddit([]),
        }
    )

    stats = Collector(db, config, reddit).sweep(sweep_index=0)

    assert stats.posts_written == 1
    assert db.scalar("SELECT COUNT(*) FROM posts") == 1


def test_rerunning_a_sweep_updates_engagement_and_refilters(db, config):
    """A post that gains comments can pass a filter it previously failed."""
    quiet = FakeSubmission(
        "eee",
        title="Thoughts on venue software",
        body=(
            "Curious what the general feeling is on the venue management tools out "
            "there right now, since there are a lot of them and the opinions I have "
            "read so far seem to point in every possible direction at once."
        ),
        score=0,
        num_comments=0,
    )
    reddit = FakeReddit(
        {"eventplanning": FakeSubreddit([quiet]), "HVAC": FakeSubreddit([])}
    )
    collector = Collector(db, config, reddit)

    collector.sweep(sweep_index=0)
    assert db.query_one("SELECT stage1_keep FROM posts")["stage1_keep"] == 0

    quiet.score, quiet.num_comments = 25, 14
    collector.sweep(sweep_index=0)

    row = db.query_one("SELECT stage1_keep, score, num_comments FROM posts")
    assert row["stage1_keep"] == 1
    assert row["score"] == 25
    assert row["num_comments"] == 14


def test_one_dead_subreddit_does_not_end_the_sweep(db, config):
    reddit = FakeReddit(
        {
            "eventplanning": FakeSubreddit([], fail=True),
            "HVAC": FakeSubreddit([FakeSubmission("fff")]),
        }
    )

    stats = Collector(db, config, reddit).sweep(sweep_index=0)

    assert stats.posts_written == 1
    assert len(stats.errors) == 1
    assert "r/eventplanning" in stats.errors[0]


def test_query_terms_rotate_across_sweeps(db, config):
    collector = Collector(db, config, FakeReddit({}))

    first = collector.terms_for_sweep(0)
    second = collector.terms_for_sweep(1)

    assert first == ["is there an app", "wish there was"]
    assert second == ["manually", "workaround"]
    assert not set(first) & set(second)

    # Five terms taken two at a time must wrap, not run off the end.
    assert collector.terms_for_sweep(2) == ["so annoying", "is there an app"]


def test_search_runs_the_rotated_terms(db, config):
    sub = FakeSubreddit([])
    reddit = FakeReddit({"eventplanning": sub, "HVAC": FakeSubreddit([])})

    Collector(db, config, reddit).sweep(sweep_index=0)

    assert sub.searches == ['"is there an app"', '"wish there was"']
