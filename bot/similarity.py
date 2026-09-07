"""Dependency-free text similarity for dedup and conflict triage.

Token and word-shingle Jaccard only. No ML, no embeddings, so results are
reproducible byte-for-byte across runners, which the single-comment update in
the PR workflow depends on.
"""

from __future__ import annotations

import re
from typing import Iterable

_WORD = re.compile(r"[a-z0-9]+(?:[._-][a-z0-9]+)*")


def normalize_text(text: str) -> str:
    """Lowercase, collapse whitespace and punctuation into single spaces."""
    return " ".join(_WORD.findall(text.lower()))


def tokens(text: str) -> tuple[str, ...]:
    return tuple(_WORD.findall(text.lower()))


def shingles(seq: tuple[str, ...], size: int = 3) -> frozenset[tuple[str, ...]]:
    if len(seq) < size:
        return frozenset([seq]) if seq else frozenset()
    return frozenset(seq[i : i + size] for i in range(len(seq) - size + 1))


def jaccard(a: Iterable, b: Iterable) -> float:
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def text_similarity(a: str, b: str, shingle_size: int = 3) -> float:
    """Blend of token-set Jaccard and word-shingle Jaccard, rounded to 4dp.

    Token overlap captures "same vocabulary"; shingles capture "same phrasing".
    Blending keeps re-ordered sentences similar without letting two entries
    that merely share jargon score as duplicates.
    """
    ta, tb = tokens(a), tokens(b)
    if not ta and not tb:
        return 0.0
    token_j = jaccard(ta, tb)
    shingle_j = jaccard(shingles(ta, shingle_size), shingles(tb, shingle_size))
    return round(0.5 * token_j + 0.5 * shingle_j, 4)


def normalize_fingerprints(items: Iterable[str]) -> frozenset[str]:
    """Apply the fingerprint canonicalization from docs/federation.md step 2."""
    out = set()
    for item in items:
        norm = " ".join(str(item).lower().split())
        if norm:
            out.add(norm)
    return frozenset(out)


def fingerprint_similarity(a: Iterable[str], b: Iterable[str]) -> tuple[float, tuple[str, ...]]:
    """Jaccard over normalized fingerprints plus the sorted shared set."""
    fa, fb = normalize_fingerprints(a), normalize_fingerprints(b)
    shared = tuple(sorted(fa & fb))
    return round(jaccard(fa, fb), 4), shared
