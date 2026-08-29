# Reddit pain point scanner

Sweeps Reddit for people describing problems in their daily life or work,
filters out the noise, clusters repeated complaints, and produces a short
ranked list of app ideas with the evidence attached.

The output is not "here are 100 ideas." It is "here are five problems that came
up 40+ times this month, here is what people already tried, here is where the
existing tools fall short, and here are the exact threads to go read."

## Architecture

Four pieces, deliberately separate so any one can fail without taking the
others down:

```
[Collector]   -> raw posts + comments into the database        (cron, 6-hourly)
[Classifier]  -> is this a real pain point? cheap filter, then LLM   (cron, 6-hourly)
[Clusterer]   -> group similar pains, count them, track over time    (weekly)
[Reporter]    -> ranked digest + MCP server for querying in chat     (weekly / on demand)
```

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env          # fill in the Reddit and Anthropic credentials

export DATABASE_URL=sqlite:///painpoints.db   # Postgres in production
python -m painpoint initdb
python -m painpoint collect
python -m painpoint classify
python -m painpoint cluster
python -m painpoint report --no-llm --stdout
```

The pipeline runs end to end with only Reddit credentials and an Anthropic key.
Postgres and an OpenAI key are both production upgrades, not prerequisites.

### Credentials

Create a **script**-type app at <https://reddit.com/prefs/apps>, then fill in
`.env` (never commit it; `.gitignore` already covers it):

| Variable | Needed for |
|---|---|
| `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET` / `REDDIT_USER_AGENT` | the collector |
| `ANTHROPIC_API_KEY` | classification, and the digest's narrative sections |
| `DATABASE_URL` | everything (`sqlite:///painpoints.db` or a Postgres URL) |
| `OPENAI_API_KEY` | only if you switch the clusterer to semantic embeddings |

## Commands

| Command | What it does |
|---|---|
| `painpoint initdb` | Create the tables |
| `painpoint collect` | One sweep of the configured subreddits |
| `painpoint classify` | Score everything that passed stage 1 |
| `painpoint classify --review 50` | Print recent scores for hand-checking |
| `painpoint filter-stats` | Stage 1 survival rate and drop reasons |
| `painpoint cluster` | Embed pain summaries and rebuild clusters |
| `painpoint report` | Write the weekly digest to `reports/` |
| `painpoint status` | Corpus counts |
| `painpoint mcp` | Run the MCP server on stdio |

Useful flags: `--vertical` to limit a sweep, `--limit` to cap a classify run,
`--no-llm` for a digest with no synthesis, `--stdout` to print it instead of
writing a file.

## How it works

### 1. Collector

Configured in `config/subreddits.yaml`: subreddits grouped by vertical, plus the
complaint phrases rotated through search. Pick verticals you actually know
something about -- domain knowledge is what makes the output readable.

Each sweep walks `/new` for the configured window and runs a rotating slice of
the search terms. Comments are pulled only for posts that survive stage 1, since
that is where competitors and workarounds actually get named.

Two limits are worth knowing: listing endpoints cap out near 1000 items, and
Pushshift has been moderator-only since 2023. There is no historical backfill.
This is a forward-looking collector, and the dataset gets more valuable the
longer it runs. PRAW handles OAuth refresh and the ~100 requests/minute limit;
don't add throttling on top of it.

### 2. Classifier

**Stage 1 is free and kills most of the volume.** Hard rules drop bots,
self-promo, memes, link-only bodies and posts under 100 characters. Soft rules
drop low-engagement posts and lookup questions *unless* the post contains
first-person struggle language -- a detailed account of a daily grind that
landed at zero points is exactly what this pipeline exists to find.

Tune it with `painpoint filter-stats` and aim for roughly 10% surviving. Well
above that and stage 2 is paying to read noise; well below and real complaints
are being thrown away.

**Stage 2 runs on Haiku**, 20 posts per call, because this is a labelling job
rather than a reasoning job. Two things are deliberate: the model returns five
sub-scores and never the total (arithmetic is not what you pay a model for, and
a hallucinated total silently corrupts every ranking downstream), and a post is
scored once ever, so re-running costs nothing and a crashed run can be
restarted.

`pain_summary` must describe the problem, not the app -- "I lose track of which
invoices I sent", not "needs an invoice tracker". The same problem gets proposed
with ten different solutions, and solution-shaped summaries never cluster.

### 3. Scoring rubric

Five dimensions, 0-5 each, 25 max.

| Dimension | 5 looks like |
|---|---|
| **Specificity** | A named workflow with steps. "Everything is broken" is 0. |
| **Workflow frequency** | Daily. Weekly is 3, annual is 1 -- people don't pay for annual annoyances. |
| **Money signal** | Paying for a tool that doesn't work, or paying a person to do it by hand. Wanting something free scores 0 and is a red flag. |
| **Incumbent gap** | Incumbents exist *and* people complain about them in-thread: validated demand plus an opening. No incumbent scores 2 -- unproven, not a win. A well-loved product that owns the space scores 0. |
| **Buildability** | A small team ships v1 in under 8 weeks. Score down hard for hardware, day-one marketplace liquidity, an integration partner who must say yes, regulatory approval, or data you can't legally get. |

The classifier also records any competitor named in the post or its comments.
That name is your search term for the next round of listening.

### 4. Clusterer

Ranking happens per cluster, never per post:

```
cluster_score = median(total_score) * log10(member_count + 1) * recency_weight
```

`recency_weight` is 1.0 for anything mentioned in the last 90 days and then
decays with a 45-day half life. A pain that stopped being mentioned either got
solved or was never real.

Two embedding backends. `hashing` is the default: pure Python, no key, no
network, deterministic. It is *lexical* similarity -- it groups "I lose track of
sent invoices" with "losing track of which invoices went out", but not with "my
billing follow-ups fall through the cracks". Switch to `openai`
(`text-embedding-3-small`, under $2/month here) once you have real volume. The
two produce different similarity distributions and therefore carry different
default thresholds; a threshold tuned on one is meaningless on the other.

Clustering is scoped per vertical and recomputed from scratch each run, so
membership counts stay honest as the corpus grows.

### 5. Reporter

The digest keeps two halves strictly apart. Everything countable -- mention
counts, month-over-month change, workaround and competitor tallies, permalinks
-- comes from the database, and no model touches those numbers. Only the
narrative (the problem statement, why now, the app concept, the riskiest
assumption) is synthesized. Run `--no-llm` and you still get a complete report;
you just lose the prose.

The thread links at the end of each entry are the most important part. The
model's job is to route your attention, not to make your decision.

### MCP server

`painpoint mcp` exposes the database to Claude over stdio:

- `search_pains(vertical, min_score, since, limit)` -- ranked clusters
- `get_cluster(cluster_id)` -- one cluster with every member post and permalink
- `get_trending(days, limit)` -- steepest growth, recent window vs the one before
- `add_subreddit(name, vertical)` -- add a target to the config
- `get_status()` -- corpus counts

`since` accepts a relative window (`30d`, `6w`) or an ISO date. Register it with:

```json
{
  "mcpServers": {
    "painpoint": {
      "command": "python",
      "args": ["-m", "painpoint", "mcp"],
      "cwd": "/path/to/painpoint-scanner",
      "env": { "DATABASE_URL": "postgresql://..." }
    }
  }
}
```

Needs `mcp >= 2.0`, where `FastMCP` was renamed to `MCPServer`.

## Deployment

Collector and classifier on a small VPS or Railway box, every 6 hours. Not
Vercel -- its cron functions time out well before a full sweep finishes. Postgres
for the database (Supabase's free tier is fine to start), and the reporter on a
weekly cron on the same box. `deploy/crontab.example` has the schedule.

| Item | Monthly |
|---|---|
| VPS or Railway | $6-20 |
| Postgres | $0-25 |
| Haiku classification (~5k posts/mo after stage 1) | $10-30 |
| Embeddings | under $2 |
| Reddit API | $0 at this volume |
| **Total** | **$20-80** |

Reddit's API is free under 100 queries/minute for non-commercial use. If this
ever becomes something you sell, re-read their terms -- the commercial tier is a
different conversation.

## Build order

1. **Collector plus database.** Run it for a week and just look at the raw data.
   You will change your subreddit list after seeing what actually comes in.
2. **Stage 1 filter.** Tune with `painpoint filter-stats` until roughly 10%
   survives.
3. **Stage 2 classifier.** Hand-check 50 classifications against your own
   judgment (`painpoint classify --review 50`) before trusting any of it. Your
   calibration is the ground truth here, not the model's.
4. Clustering. 5. Reporter. 6. MCP server.

Do not build 3 through 6 until 1 and 2 are producing output you agree with.

## Tests

```bash
python -m pytest
```

The suite runs against a temporary SQLite database with fake Reddit and
Anthropic clients: no network, no services, no API keys.

## What this will get wrong

- **Reddit is not the market.** It skews young, technical, English-speaking and
  complaint-prone. A pain that is loud here may be quiet everywhere else, and
  vice versa.
- **Complaining is free, paying is not.** The money signal is the best proxy in
  the rubric and it is still only a proxy. Nothing in this pipeline is evidence
  that anyone will pay you.
- **Scores are a filter, not a verdict.** This gets you from thousands of posts
  to five conversations. The five conversations are the actual validation.
- **The real next step:** DM ten people from the threads and ask what they tried
  and what they spend on it now. A week of that beats another month of scraping.
