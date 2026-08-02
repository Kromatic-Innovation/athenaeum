"""Tests for athenaeum.vecmath -- shared pure-Python cosine similarity (athenaeum#542)."""

from __future__ import annotations

import math

import pytest

from athenaeum.vecmath import cosine


class TestCosine:
    def test_identical_vectors_are_one(self) -> None:
        v = [0.3, -0.2, 0.5, 1.1]
        assert cosine(v, v) == pytest.approx(1.0)

    def test_identical_unit_vector_is_one(self) -> None:
        v = [1.0, 0.0, 0.0]
        assert cosine(v, v) == pytest.approx(1.0)

    def test_orthogonal_vectors_are_zero(self) -> None:
        assert cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    def test_opposite_vectors_are_negative_one(self) -> None:
        assert cosine([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)

    def test_scaling_invariant(self) -> None:
        # Cosine similarity is magnitude-invariant: scaling one vector must
        # not change the result.
        a = [1.0, 2.0, 3.0]
        b = [2.0, 4.0, 6.0]
        assert cosine(a, b) == pytest.approx(1.0)

    def test_zero_vector_left_is_zero(self) -> None:
        assert cosine([0.0, 0.0], [1.0, 1.0]) == 0.0

    def test_zero_vector_right_is_zero(self) -> None:
        assert cosine([1.0, 1.0], [0.0, 0.0]) == 0.0

    def test_both_zero_vectors_is_zero(self) -> None:
        assert cosine([0.0, 0.0], [0.0, 0.0]) == 0.0

    def test_length_mismatch_is_zero(self) -> None:
        assert cosine([1.0, 0.0], [1.0, 0.0, 0.0]) == 0.0

    def test_negative_components(self) -> None:
        a = [-1.0, -2.0, -3.0]
        b = [1.0, 2.0, 3.0]
        assert cosine(a, b) == pytest.approx(-1.0)

    def test_known_value(self) -> None:
        # cos(theta) between (1,0) and (1,1) is 1/sqrt(2).
        assert cosine([1.0, 0.0], [1.0, 1.0]) == pytest.approx(1.0 / math.sqrt(2))

    def test_empty_vectors_is_zero(self) -> None:
        assert cosine([], []) == 0.0
