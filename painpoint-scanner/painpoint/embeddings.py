"""Embedding backends for clustering pain summaries.

Two backends:

* `openai` -- `text-embedding-3-small`. Semantic, and what the similarity
  threshold in the spec (0.82) was written for. Costs well under $2/month at
  this volume. Needs `OPENAI_API_KEY`.
* `hashing` -- pure Python, no key, no network, deterministic. Hashes word
  unigrams and character n-grams into a fixed-width vector. This is lexical similarity,
  not semantic: it groups "I lose track of which invoices I sent" with "I keep
  losing track of sent invoices", but not with "my billing follow-ups fall
  through the cracks". It is the default so the pipeline runs end to end out of
  the box, and it is the wrong choice once you have real volume.

Because the two backends produce different similarity distributions, they carry
different default thresholds. A threshold tuned on one is meaningless on the
other. Python's built-in `hash()` is salted per process, so the hashing backend
uses blake2b -- vectors must stay stable across runs to be worth caching.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
from typing import Protocol, Sequence

TOKEN_PATTERN = re.compile(r"[a-z0-9']+")

# Words that appear in nearly every pain summary and would otherwise dominate
# the lexical signal.
STOPWORDS = frozenset(
    """a an and are as at be been but by can cant do does dont for from get gets
    had has have how i im in into is it its me my no not of on or our so than
    that the their them then there these they this to too us was we were what
    when where which who why will with you your""".split()
)


class Embedder(Protocol):
    name: str
    dim: int
    default_threshold: float

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


CHAR_NGRAM = 4


def tokenize(text: str) -> list[str]:
    """Word unigrams plus padded character 4-grams.

    The character grams are what make this backend usable at all: without them
    "I lose track of sent invoices" and "losing track of invoices sent" share
    almost no tokens, because inflected forms of the same word never match. With
    them, measured on paraphrase pairs, similarity runs 0.39-0.74 while
    unrelated summaries stay under 0.15 -- a wide enough gap for a threshold to
    sit in.
    """
    words = [w for w in TOKEN_PATTERN.findall((text or "").lower()) if w not in STOPWORDS]
    tokens = list(words)
    for word in words:
        padded = f"^{word}$"
        tokens += [
            padded[i : i + CHAR_NGRAM]
            for i in range(max(1, len(padded) - CHAR_NGRAM + 1))
        ]
    return tokens


def _bucket(token: str, dim: int) -> tuple[int, float]:
    """Map a token to a bucket and a sign, stably across processes and runs."""
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    value = int.from_bytes(digest, "big")
    return value % dim, 1.0 if (value >> 63) & 1 else -1.0


def l2_normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vector))
    return [v / norm for v in vector] if norm else vector


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class HashingEmbedder:
    name = "hashing"
    # Calibrated on paraphrase pairs vs unrelated summaries: paraphrases score
    # 0.39-0.74, unrelated pairs stay under 0.15. Well clear of both edges.
    default_threshold = 0.35

    def __init__(self, dim: int = 512):
        self.dim = dim

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed_one(t) for t in texts]

    def _embed_one(self, text: str) -> list[float]:
        counts: dict[str, int] = {}
        for token in tokenize(text):
            counts[token] = counts.get(token, 0) + 1

        vector = [0.0] * self.dim
        for token, count in counts.items():
            index, sign = _bucket(token, self.dim)
            # Sublinear term frequency: a word repeated ten times is not ten
            # times as informative as one used once.
            vector[index] += sign * (1.0 + math.log(count))
        return l2_normalize(vector)


class OpenAIEmbedder:
    name = "openai"
    default_threshold = 0.82

    def __init__(self, model: str = "text-embedding-3-small", dim: int = 1536):
        self.model = model
        self.dim = dim
        self._client = None

    @property
    def client(self):
        if self._client is None:
            from openai import OpenAI

            if not os.environ.get("OPENAI_API_KEY"):
                raise RuntimeError(
                    "OPENAI_API_KEY is not set. Either set it or switch "
                    "clusterer.embedding_backend to 'hashing' in the config."
                )
            self._client = OpenAI()
        return self._client

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        # The endpoint accepts batches; 128 keeps request bodies comfortable.
        for start in range(0, len(texts), 128):
            chunk = [t or " " for t in texts[start : start + 128]]
            response = self.client.embeddings.create(model=self.model, input=chunk)
            vectors.extend(item.embedding for item in response.data)
        if vectors:
            self.dim = len(vectors[0])
        return vectors


def get_embedder(backend: str = "hashing", openai_model: str = "text-embedding-3-small"):
    if backend == "openai":
        return OpenAIEmbedder(openai_model)
    if backend == "hashing":
        return HashingEmbedder()
    raise ValueError(f"Unknown embedding backend: {backend!r} (use 'hashing' or 'openai')")
