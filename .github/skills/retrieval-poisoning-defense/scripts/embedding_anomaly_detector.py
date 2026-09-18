"""Category-aware, calibrated anomaly signals for stored retrieval embeddings."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from math import isfinite
from numbers import Real

import numpy as np
from numpy.typing import ArrayLike, NDArray
from scipy.linalg import solve_triangular
from scipy.stats import chi2
from sklearn.covariance import LedoitWolf

from _validation import (
    as_float_matrix,
    as_float_vector,
    positive_int,
    probability,
    unit_rows,
)


@dataclass(frozen=True)
class EmbeddingScore:
    category: str
    flagged: bool
    mahalanobis_distance: float
    mahalanobis_threshold: float
    cosine_similarity: float
    cosine_threshold: float
    neighbor_concentration: float | None
    concentration_threshold: float
    reasons: tuple[str, ...]
    unavailable_checks: tuple[str, ...]


@dataclass(frozen=True)
class _CategoryReference:
    mean: NDArray[np.float64]
    cholesky: NDArray[np.float64]
    scale: float
    directions: NDArray[np.float64]
    mahalanobis_threshold: float
    cosine_threshold: float


def _cosine_threshold(value: float, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, Real)
        or not isfinite(value)
        or not -1 <= value <= 1
    ):
        raise ValueError(f"{name} must be a finite number in [-1, 1].")
    return float(value)


def _distances(
    matrix: NDArray[np.float64],
    mean: NDArray[np.float64],
    cholesky: NDArray[np.float64],
    scale: float,
) -> NDArray[np.float64]:
    with np.errstate(over="raise", invalid="raise"):
        whitened = solve_triangular(cholesky, (matrix / scale - mean).T, lower=True)
        distances = np.linalg.norm(whitened, axis=0)
    if not np.isfinite(distances).all():
        raise ValueError("Mahalanobis distances are not finite; check feature scaling.")
    return distances


def _neighbor_similarities(
    directions: NDArray[np.float64],
    reference: NDArray[np.float64],
    count: int,
) -> NDArray[np.float64]:
    similarities = np.clip(directions @ reference.T, -1, 1)
    count = min(count, reference.shape[0])
    return np.partition(similarities, -count, axis=1)[:, -count:].mean(axis=1)


class EmbeddingAnomalyDetector:
    """Fit only trusted reference data; a flag is a review signal, not proof.

    Mahalanobis distance uses raw embeddings and shrinkage covariance. Cosine
    checks use unit directions. Supply held-out calibration data for empirical
    per-category distance and similarity thresholds. Query banks, when supplied,
    must use the same embedding model, version, dimension, and preprocessing.
    """

    def __init__(
        self,
        *,
        mahalanobis_quantile: float = 0.99,
        min_cosine_similarity: float = 0.5,
        neighbor_similarity: float = 0.8,
        concentration_threshold: float = 0.2,
        n_neighbors: int = 5,
        min_reference_samples: int = 5,
    ) -> None:
        self.mahalanobis_quantile = probability(
            mahalanobis_quantile, "mahalanobis_quantile"
        )
        if self.mahalanobis_quantile == 1:
            raise ValueError("mahalanobis_quantile must be less than one.")
        self.min_cosine_similarity = _cosine_threshold(
            min_cosine_similarity, "min_cosine_similarity"
        )
        self.neighbor_similarity = _cosine_threshold(
            neighbor_similarity, "neighbor_similarity"
        )
        self.concentration_threshold = probability(
            concentration_threshold, "concentration_threshold", allow_zero=True
        )
        self.n_neighbors = positive_int(n_neighbors, "n_neighbors")
        self.min_reference_samples = positive_int(
            min_reference_samples, "min_reference_samples", minimum=3
        )
        self._references: dict[str, _CategoryReference] = {}
        self._query_banks: dict[str, NDArray[np.float64]] = {}
        self._dimension: int | None = None

    def fit_reference(
        self,
        clean_embeddings_by_category: Mapping[str, ArrayLike],
        *,
        calibration_embeddings_by_category: Mapping[str, ArrayLike] | None = None,
        query_embeddings_by_category: Mapping[str, ArrayLike] | None = None,
    ) -> EmbeddingAnomalyDetector:
        if not isinstance(clean_embeddings_by_category, Mapping) or not clean_embeddings_by_category:
            raise ValueError("At least one trusted reference category is required.")
        if any(
            not isinstance(category, str) or not category.strip()
            for category in clean_embeddings_by_category
        ):
            raise ValueError("Reference categories must be nonempty strings.")
        categories = set(clean_embeddings_by_category)
        if calibration_embeddings_by_category is not None:
            if set(calibration_embeddings_by_category) != categories:
                raise ValueError("Calibration categories must match reference categories.")
        references: dict[str, _CategoryReference] = {}
        dimension: int | None = None
        for category, values in clean_embeddings_by_category.items():
            matrix = as_float_matrix(
                values, "reference embeddings", min_rows=self.min_reference_samples
            )
            if dimension is None:
                dimension = matrix.shape[1]
            if matrix.shape[1] != dimension:
                raise ValueError("All reference categories must have the same dimension.")
            directions = unit_rows(matrix, "reference embeddings")
            with np.errstate(over="raise", invalid="raise"):
                scale = float(np.max(np.abs(matrix)))
                scaled = matrix / scale
                covariance = LedoitWolf(store_precision=False).fit(scaled).covariance_
                # Keep constant/low-rank dimensions observable rather than discarding
                # them with a pseudoinverse, which could hide off-subspace insertions.
                ridge = max(float(np.trace(covariance) / dimension) * 1e-6, 1e-12)
                cholesky = np.linalg.cholesky(covariance + np.eye(dimension) * ridge)
                mean = scaled.mean(axis=0)
            distance_threshold = float(
                np.sqrt(chi2.ppf(self.mahalanobis_quantile, df=dimension))
            )
            cosine_threshold = self.min_cosine_similarity
            if calibration_embeddings_by_category is not None:
                calibration = as_float_matrix(
                    calibration_embeddings_by_category[category],
                    "held-out calibration embeddings",
                    min_rows=self.min_reference_samples,
                )
                if calibration.shape[1] != dimension:
                    raise ValueError("Calibration and reference dimensions must match.")
                calibration_directions = unit_rows(calibration, "calibration embeddings")
                distance_threshold = max(
                    float(
                        np.quantile(
                            _distances(calibration, mean, cholesky, scale),
                            self.mahalanobis_quantile,
                        )
                    ),
                    1e-8,
                )
                cosine_threshold = float(
                    np.quantile(
                        _neighbor_similarities(
                            calibration_directions, directions, self.n_neighbors
                        ),
                        1 - self.mahalanobis_quantile,
                    )
                )
            references[category] = _CategoryReference(
                mean, cholesky, scale, directions, distance_threshold, cosine_threshold
            )
        query_banks = {
            category: reference.directions for category, reference in references.items()
        }
        if query_embeddings_by_category is not None:
            if not query_embeddings_by_category:
                raise ValueError("A provided query bank must not be empty.")
            query_banks = {}
            for category, values in query_embeddings_by_category.items():
                if not isinstance(category, str) or not category.strip():
                    raise ValueError("Query-bank categories must be nonempty strings.")
                bank = unit_rows(values, "query-bank embeddings")
                if bank.shape[1] != dimension:
                    raise ValueError("Query-bank and reference dimensions must match.")
                query_banks[category] = bank
        self._references = references
        self._query_banks = query_banks
        self._dimension = dimension
        return self

    def score(self, new_embedding: ArrayLike, *, category: str) -> EmbeddingScore:
        if not self._references:
            raise RuntimeError("Call fit_reference with trusted embeddings before scoring.")
        if category not in self._references:
            raise ValueError("The claimed category has no trusted reference distribution.")
        vector = as_float_vector(
            new_embedding, "new_embedding", dimension=self._dimension
        )
        direction = unit_rows(vector.reshape(1, -1), "new_embedding")
        reference = self._references[category]
        distance = float(
            _distances(
                vector.reshape(1, -1),
                reference.mean,
                reference.cholesky,
                reference.scale,
            )[0]
        )
        similarity = float(
            _neighbor_similarities(
                direction, reference.directions, self.n_neighbors
            )[0]
        )
        unrelated_fractions = [
            float(np.mean(np.clip(direction @ bank.T, -1, 1) >= self.neighbor_similarity))
            for bank_category, bank in self._query_banks.items()
            if bank_category != category
        ]
        concentration = (
            float(np.mean(unrelated_fractions)) if unrelated_fractions else None
        )
        reasons = []
        if distance > reference.mahalanobis_threshold:
            reasons.append("mahalanobis_outlier")
        if similarity < reference.cosine_threshold:
            reasons.append("category_cosine_outlier")
        if concentration is not None and concentration > self.concentration_threshold:
            reasons.append("unrelated_neighbor_concentration")
        return EmbeddingScore(
            category=category,
            flagged=bool(reasons),
            mahalanobis_distance=distance,
            mahalanobis_threshold=reference.mahalanobis_threshold,
            cosine_similarity=similarity,
            cosine_threshold=reference.cosine_threshold,
            neighbor_concentration=concentration,
            concentration_threshold=self.concentration_threshold,
            reasons=tuple(reasons),
            unavailable_checks=(
                ("unrelated_neighbor_concentration",) if concentration is None else ()
            ),
        )
