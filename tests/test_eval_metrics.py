"""Metric tests: every expected value below is hand-computed, not produced by the code under test.

A metric bug is the cheapest way to publish a fake-looking number, so these cases
pin MRR/P/R/F1/AP at exact values, including the truncation and budget-normalisation
rules that the README states.
"""
from __future__ import annotations

import math

import pytest

from eval import metrics


def test_mrr_uses_first_hit_and_respects_the_cutoff():
    ranking = ["a", "b", "c", "d"]
    relevant = {"c"}
    assert metrics.mrr_at_k(ranking, relevant, 1) == 0.0
    assert metrics.mrr_at_k(ranking, relevant, 2) == 0.0
    assert metrics.mrr_at_k(ranking, relevant, 3) == pytest.approx(1 / 3)
    assert metrics.mrr_at_k(ranking, relevant, 8) == pytest.approx(1 / 3)


def test_mrr_is_one_when_the_top_hit_is_relevant():
    assert metrics.mrr_at_k(["a", "b"], {"a", "b"}, 1) == 1.0


def test_single_relevant_at_rank_three():
    ranking = ["a", "b", "c"]
    relevant = {"c"}
    assert metrics.precision_at_k(ranking, relevant, 3) == pytest.approx(1 / 3)
    # budget-normalised recall: min(|R|, k) == 1, so the one relevant slot was found
    assert metrics.recall_at_k(ranking, relevant, 3) == 1.0
    assert metrics.f1_at_k(ranking, relevant, 3) == pytest.approx(0.5)
    # AP: one hit at rank 3 contributes P@3 = 1/3, divided by min(|R|, 3) = 1
    assert metrics.ap_at_k(ranking, relevant, 3) == pytest.approx(1 / 3)
    assert metrics.max_f1_at_k(ranking, relevant, 3) == pytest.approx(0.5)


def test_multi_relevant_ranked_list():
    ranking = ["a", "b", "c", "d", "e"]
    relevant = {"a", "c", "e"}
    assert metrics.precision_at_k(ranking, relevant, 5) == pytest.approx(3 / 5)
    assert metrics.recall_at_k(ranking, relevant, 5) == pytest.approx(1.0)
    assert metrics.f1_at_k(ranking, relevant, 5) == pytest.approx(0.75)
    expected_ap = (1 / 1 + 2 / 3 + 3 / 5) / 3
    assert metrics.ap_at_k(ranking, relevant, 5) == pytest.approx(expected_ap)
    assert metrics.max_f1_at_k(ranking, relevant, 5) == pytest.approx(1.0)


def test_large_gold_set_is_budget_normalised_and_the_ceiling_is_reported():
    """8 slots, 50 relevant documents: top-8 all relevant is a perfect *budgeted* result."""
    ranking = [f"p{i}" for i in range(8)]
    relevant = {f"p{i}" for i in range(50)}
    assert metrics.precision_at_k(ranking, relevant, 8) == 1.0
    assert metrics.recall_at_k(ranking, relevant, 8) == 1.0  # min(50, 8) == 8
    assert metrics.f1_at_k(ranking, relevant, 8) == 1.0
    assert metrics.ap_at_k(ranking, relevant, 8) == 1.0
    assert metrics.recall_ceiling(relevant, 8) == pytest.approx(8 / 50)


def test_shallow_ranking_cannot_exceed_its_depth():
    ranking = ["a", "b", "c", "d", "e"]
    relevant = {f"p{i}" for i in range(10)} | {"a", "b"}  # |R| = 12
    assert metrics.ap_at_k(ranking, relevant, 20) == pytest.approx(
        (1 / 1 + 2 / 2) / 12
    )


def test_no_hits_scores_zero_but_does_not_crash():
    ranking = ["a", "b"]
    relevant = {"z"}
    assert metrics.mrr_at_k(ranking, relevant, 8) == 0.0
    assert metrics.precision_at_k(ranking, relevant, 2) == 0.0
    assert metrics.recall_at_k(ranking, relevant, 2) == 0.0
    assert metrics.f1_at_k(ranking, relevant, 2) == 0.0
    assert metrics.ap_at_k(ranking, relevant, 2) == 0.0


def test_empty_relevance_is_an_error_not_a_zero():
    with pytest.raises(ValueError):
        metrics.mrr_at_k(["a"], set(), 8)
    with pytest.raises(ValueError):
        metrics.ap_at_k(["a"], set(), 8)
    with pytest.raises(ValueError):
        metrics.recall_ceiling(set(), 8)


def test_bootstrap_ci_is_deterministic_and_brackets_the_mean():
    values = [0.0, 0.5, 1.0, 1.0, 0.25]
    first = metrics.bootstrap_ci(values, n=500)
    second = metrics.bootstrap_ci(values, n=500)
    assert first == second
    lo, hi = first
    mean = sum(values) / len(values)
    assert lo <= mean <= hi
    assert 0.0 <= lo <= hi <= 1.0


def test_bootstrap_ci_degenerate_cases():
    assert metrics.bootstrap_ci([]) == (0.0, 0.0)
    assert metrics.bootstrap_ci([0.4]) == (0.4, 0.4)


def test_per_query_rows_carry_depth_relevance_and_hits():
    rows = metrics.per_query([["a", "b"], ["c"]], [{"a"}, {"z"}], mrr_ks=(1,), f1_k=2, ap_ks=(2,))
    assert [row["depth"] for row in rows] == [2, 1]
    assert [row["relevant"] for row in rows] == [1, 1]
    assert [row["hits"] for row in rows] == [1, 0]
    assert rows[0]["mrr@1"] == 1.0
    assert rows[1]["mrr@1"] == 0.0


def test_summarize_means_and_shape():
    rankings = [["a", "b"], ["c", "d"], ["e", "f"]]
    relevants = [{"a"}, {"c"}, {"z"}]
    out = metrics.summarize(rankings, relevants, mrr_ks=(1, 3, 8), f1_k=2, ap_ks=(2,), ci_n=200)
    assert out["n_queries"] == 3
    assert set(out["metrics"]) == {"mrr@1", "mrr@3", "mrr@8", "p@2", "r@2", "f1@2", "maxf1@2", "ceiling@2", "ap@2"}
    assert out["metrics"]["mrr@1"]["mean"] == pytest.approx(2 / 3)
    for key, entry in out["metrics"].items():
        lo, hi = entry["ci95"]
        assert math.isfinite(entry["mean"])
        assert lo <= entry["mean"] <= hi, key
    assert out["mean_relevant"] == pytest.approx(1.0)
    assert len(out["per_query"]) == 3
