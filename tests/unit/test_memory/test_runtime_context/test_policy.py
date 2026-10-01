"""Unit tests for the runtime-interop policy primitives.

Pure functions only — no I/O, no DB, no wall clock. The seeded-RNG case pins
``sample_random`` to the exact permutation ``random.Random(0)`` produces, so a
change to the draw strategy fails loudly instead of passing on an
order-insensitive set comparison.
"""

from __future__ import annotations

import random

import pytest

from corti.memory.runtime_context.policy import (
    MIN_PROMPT_CHARS,
    is_trivial_prompt,
    sample_random,
    truncate_block,
)

# ── is_trivial_prompt ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "   ",
        "\n\t ",
        "/commit",
        "/memory:search failover",
        "hi",
        "ok",
        "OK",
        "okay",
        "ok!",
        "ok.",
        "thanks",
        "thanks!",
        "continue",
        "continue.",
        "got it",
        "got it!",
        "done",
        "nice",
        "k",
        "y",
        "nope",
        "nope!",
        "ab",  # shorter than MIN_PROMPT_CHARS
    ],
)
def test_is_trivial_prompt_true(text: str | None) -> None:
    """Blank, slash commands, short strings and acknowledgements are trivial."""
    assert is_trivial_prompt(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "explain the failover design",
        "why did the cascade re-embed everything",
        "继续处理那个集群问题",
    ],
)
def test_is_trivial_prompt_false(text: str) -> None:
    """Real prompts — including a CJK one — are never trivial."""
    assert is_trivial_prompt(text) is False


def test_cjk_prompt_len_is_above_min_chars() -> None:
    """The CJK prompt is longer than MIN_PROMPT_CHARS yet still non-trivial."""
    text = "继续处理那个集群问题"
    assert len(text) > MIN_PROMPT_CHARS
    assert is_trivial_prompt(text) is False


# ── sample_random ────────────────────────────────────────────────────────


def test_sample_random_zero_or_empty_returns_empty() -> None:
    """n <= 0 and an empty pool both yield no items."""
    assert sample_random([1, 2, 3], 0) == []
    assert sample_random([1, 2, 3], -1) == []
    assert sample_random([], 3) == []


def test_sample_random_n_larger_than_pool_returns_pool() -> None:
    """Asking for more than exists returns the whole pool, order shuffled."""
    out = sample_random([1, 2, 3], 10)
    assert sorted(out) == [1, 2, 3]


def test_sample_random_has_no_duplicates() -> None:
    """A draw is without replacement, so every pool item appears at most once."""
    out = sample_random(list(range(5)), 5)
    assert sorted(out) == list(range(5))
    assert len(out) == len(set(out))


def test_sample_random_seeded_is_deterministic() -> None:
    """A seeded RNG pins the exact permutation returned."""
    out = sample_random(list(range(10)), 3, rng=random.Random(0))
    assert out == [7, 8, 1]


# ── truncate_block ───────────────────────────────────────────────────────


def test_truncate_block_shorter_text_unchanged() -> None:
    """Text within budget is returned verbatim."""
    assert truncate_block("short body", 100) == "short body"
    assert truncate_block("", 10) == ""


def test_truncate_block_non_positive_budget_is_empty() -> None:
    """A non-positive budget means "no room" and yields the empty string."""
    assert truncate_block("some body", 0) == ""
    assert truncate_block("some body", -5) == ""


def test_truncate_block_prefers_line_boundary() -> None:
    """The cut lands on the last newline inside the window."""
    text = "line one\nline two\nline three"
    out = truncate_block(text, 20)
    assert out == "line one\nline two …"
    assert out.endswith(" …")


def test_truncate_block_without_line_boundary_cuts_mid_line() -> None:
    """No newline in the window means a hard character cut plus the ellipsis."""
    out = truncate_block("a" * 100, 10)
    assert out == "a" * 8 + " …"
    assert out.endswith(" …")


def test_truncate_block_tiny_budget_overshoots_by_one() -> None:
    """At a one-char budget the ellipsis marker wins; length is budget + 1."""
    out = truncate_block("abcdef", 1)
    assert out == " …"
    assert len(out) == 2


@pytest.mark.parametrize("max_chars", [1, 2, 3, 5, 10, 50])
def test_truncate_block_never_exceeds_budget_plus_one(max_chars: int) -> None:
    """The result length never exceeds ``max_chars + 1`` (ellipsis marker)."""
    out = truncate_block("x" * 200, max_chars)
    assert len(out) <= max_chars + 1
