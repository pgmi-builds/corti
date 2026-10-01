"""The query side of BM25 must share the index's token space.

``episode_tokens_tsv`` is a generated column over space-joined tokenizer
output, so a query has to be tokenized by that same tokenizer before it
reaches Postgres. The recallers took a tokenizer in ``RecallerDeps`` and
documented it as "tokenizer for BM25 query" — and then never used it, so the
raw string went to ``plainto_tsquery`` and Postgres' own parser invented
tokens the index never had.

The live symptom: an identifier stored verbatim returned zero hits.

    query 'CORTI-ITER-20261002-7Q4M'   -> 0 hits
    query 'CORTI ITER 20261002 7Q4M'   -> 2 hits

These tests pin the normalisation, not the SQL.
"""

from __future__ import annotations

from corti.memory.search.recall.base import RecallerDeps, tsquery_text


class _Tokenizer:
    """Stand-in that splits on hyphens, like the jieba provider does."""

    def tokenize(self, text: str) -> list[str]:
        return [t for t in text.lower().replace("-", " ").split() if t]

    def tokenize_batch(self, texts: list[str]) -> list[list[str]]:
        return [self.tokenize(t) for t in texts]


def _deps() -> RecallerDeps:
    return RecallerDeps(tokenizer=_Tokenizer())  # type: ignore[arg-type]


def test_hyphenated_identifier_is_split_into_indexed_tokens() -> None:
    """The stored form and its spaced form must normalise identically."""
    assert tsquery_text(_deps(), "CORTI-ITER-20261002-7Q4M") == (
        "corti iter 20261002 7q4m"
    )
    assert tsquery_text(_deps(), "CORTI-ITER-20261002-7Q4M") == tsquery_text(
        _deps(), "CORTI ITER 20261002 7Q4M"
    )


def test_plain_query_is_left_recognisable() -> None:
    assert tsquery_text(_deps(), "deployment conventions") == "deployment conventions"


def test_tokenizer_returning_nothing_falls_back_to_the_raw_query() -> None:
    """A degenerate tokenizer output must not become an empty tsquery."""

    class _Empty:
        def tokenize(self, text: str) -> list[str]:
            return []

        def tokenize_batch(self, texts: list[str]) -> list[list[str]]:
            return [[] for _ in texts]

    deps = RecallerDeps(tokenizer=_Empty())  # type: ignore[arg-type]
    assert tsquery_text(deps, "!!!") == "!!!"


def test_real_tokenizer_agrees_with_the_indexed_form() -> None:
    """The shipped tokenizer is the one the index was built with."""
    from corti.component.tokenizer import build_tokenizer

    deps = RecallerDeps(tokenizer=build_tokenizer())
    assert tsquery_text(deps, "CORTI-ITER-20261002-7Q4M") == (
        "corti iter 20261002 7q4m"
    )
