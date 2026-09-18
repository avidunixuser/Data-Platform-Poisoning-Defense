"""Complementary tabular poisoning signals and explicit JSONL lineage records."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime, timezone
from math import ceil, isfinite
from numbers import Real
import os
from pathlib import Path
import re
from urllib.parse import urlsplit
from uuid import uuid4
import warnings
import json

import numpy as np
from numpy.typing import NDArray
import pandas as pd
from pandas.api.types import is_bool_dtype, is_complex_dtype, is_numeric_dtype
from scipy.stats import ks_2samp
from sklearn.neighbors import NearestNeighbors

from _validation import as_float_matrix, positive_int, probability


@dataclass(frozen=True)
class LabelFlipResult:
    flagged: bool
    flagged_positions: tuple[int, ...]
    neighbor_disagreement: tuple[float, ...]
    n_neighbors: int
    disagreement_threshold: float


@dataclass(frozen=True)
class SpectralSignatureResult:
    flagged: bool
    candidate_positions: tuple[int, ...]
    scores: tuple[float, ...]
    class_top_ratios: dict[str, float]
    flagged_classes: tuple[str, ...]
    cross_class_zscores: dict[str, float]
    skipped_classes: dict[str, str]
    complete: bool
    absolute_ratio_threshold: float
    cross_class_z_threshold: float
    expected_poison_fraction: float


@dataclass(frozen=True)
class FeatureDrift:
    flagged: bool
    ks_statistic: float
    p_value: float
    adjusted_p_value: float
    mean_shift_std: float | None
    reference_constant: bool


@dataclass(frozen=True)
class BatchDriftResult:
    flagged: bool
    features: dict[str, FeatureDrift]
    new_rows: int
    reference_rows: int
    alpha: float
    min_effect_size: float
    correction: str = "bonferroni"


class AuditCoverageWarning(UserWarning):
    """A requested audit could not evaluate every class."""


def _features(
    df: pd.DataFrame, feature_cols: Sequence[str], *, min_rows: int
) -> NDArray[np.float64]:
    if not isinstance(df, pd.DataFrame):
        raise TypeError("Expected a pandas DataFrame.")
    if (
        not isinstance(feature_cols, Sequence)
        or isinstance(feature_cols, (str, bytes))
        or not feature_cols
        or any(not isinstance(column, str) or not column for column in feature_cols)
        or len(set(feature_cols)) != len(feature_cols)
    ):
        raise ValueError("feature_cols must be a nonempty sequence of unique column names.")
    if not df.columns.is_unique:
        raise ValueError("DataFrame column names must be unique.")
    if any(column not in df for column in feature_cols):
        raise ValueError("A required feature column is missing.")
    if any(
        not is_numeric_dtype(df[column])
        or is_bool_dtype(df[column])
        or is_complex_dtype(df[column])
        for column in feature_cols
    ):
        raise ValueError("Feature columns must contain numeric, non-boolean values.")
    return as_float_matrix(
        df.loc[:, list(feature_cols)].to_numpy(dtype=float, na_value=np.nan),
        "feature data",
        min_rows=min_rows,
    )


def _labels(
    df: pd.DataFrame, label_col: str, feature_cols: Sequence[str]
) -> tuple[NDArray[np.intp], list[str]]:
    if not isinstance(label_col, str) or not label_col:
        raise ValueError("label_col must be a nonempty column name.")
    if label_col in feature_cols:
        raise ValueError("Do not include the label column in feature_cols.")
    if label_col not in df:
        raise ValueError("The label column is missing.")
    if df[label_col].isna().any():
        raise ValueError("Labels must not contain missing values.")
    try:
        codes, categories = pd.factorize(df[label_col], sort=False)
    except TypeError as exc:
        raise ValueError("Labels must be scalar, hashable categorical values.") from exc
    names = [str(category) for category in categories]
    if len(set(names)) != len(names):
        raise ValueError("Distinct labels must have distinct string representations.")
    return codes, names


def _json_value(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return _json_value(
            {
                field.name: getattr(value, field.name)
                for field in fields(value)
                if field.metadata.get("audit", True) is not False
            }
        )
    if isinstance(value, np.ndarray):
        return _json_value(value.tolist())
    if isinstance(value, np.generic):
        return _json_value(value.item())
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise ValueError("Audit results must not contain NaN or infinity.")
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("Audit result mapping keys must be strings.")
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    raise TypeError("Audit results must be dataclasses or JSON-compatible values.")


class LineageAuditor:
    """Audit prepared features without mutating data, labels, or a training set.

    Features should be scaled with a transform fitted on trusted reference data,
    not refitted on the suspect batch. Returned row positions are positional:
    use df.iloc, even when the DataFrame has duplicate or nonnumeric index labels.
    """

    def __init__(self, log_path: str | Path = "lineage_log.jsonl") -> None:
        self.log_path = Path(log_path)

    def check_label_flips(
        self,
        df: pd.DataFrame,
        *,
        feature_cols: Sequence[str],
        label_col: str,
        n_neighbors: int = 5,
        disagreement_threshold: float = 0.8,
    ) -> LabelFlipResult:
        count = positive_int(n_neighbors, "n_neighbors")
        threshold = probability(disagreement_threshold, "disagreement_threshold")
        matrix = _features(df, feature_cols, min_rows=count + 1)
        codes, _ = _labels(df, label_col, feature_cols)
        neighbors = NearestNeighbors(n_neighbors=count + 1).fit(matrix)
        indices = neighbors.kneighbors(matrix, return_distance=False)
        disagreement = []
        for position, row_neighbors in enumerate(indices):
            # With duplicate vectors, the row itself is not necessarily first
            # (or present) in the returned ties. Remove it by identity, not offset.
            other_positions = row_neighbors[row_neighbors != position][:count]
            disagreement.append(float(np.mean(codes[other_positions] != codes[position])))
        flagged_positions = tuple(
            position
            for position, fraction in enumerate(disagreement)
            if fraction >= threshold
        )
        return LabelFlipResult(
            bool(flagged_positions),
            flagged_positions,
            tuple(disagreement),
            count,
            threshold,
        )

    def check_spectral_signature(
        self,
        df: pd.DataFrame,
        *,
        feature_cols: Sequence[str],
        label_col: str,
        expected_poison_fraction: float = 0.15,
        absolute_ratio_threshold: float = 0.5,
        cross_class_z_threshold: float = 1.5,
        min_class_size: int = 10,
    ) -> SpectralSignatureResult:
        fraction = probability(expected_poison_fraction, "expected_poison_fraction")
        ratio_threshold = probability(absolute_ratio_threshold, "absolute_ratio_threshold")
        minimum = positive_int(min_class_size, "min_class_size", minimum=3)
        if (
            isinstance(cross_class_z_threshold, bool)
            or not isinstance(cross_class_z_threshold, Real)
            or not isfinite(cross_class_z_threshold)
            or cross_class_z_threshold <= 0
        ):
            raise ValueError("cross_class_z_threshold must be a positive finite number.")
        matrix = _features(df, feature_cols, min_rows=minimum)
        if matrix.shape[1] < 2:
            raise ValueError("Spectral analysis requires at least two feature dimensions.")
        codes, names = _labels(df, label_col, feature_cols)
        ratios: dict[str, float] = {}
        skipped: dict[str, str] = {}
        class_positions: dict[str, NDArray[np.intp]] = {}
        scores = np.zeros(len(df), dtype=float)
        for code, name in enumerate(names):
            positions = np.flatnonzero(codes == code)
            if len(positions) < minimum:
                skipped[name] = f"Fewer than {minimum} rows."
                continue
            class_matrix = matrix[positions]
            with np.errstate(over="raise", invalid="raise"):
                centered = class_matrix - class_matrix.mean(axis=0)
                _, singular_values, directions = np.linalg.svd(
                    centered, full_matrices=False
                )
                if np.all(class_matrix == class_matrix[0]) or singular_values[0] == 0:
                    ratio = 0.0
                else:
                    ratio = float(1 / np.square(singular_values / singular_values[0]).sum())
                    scores[positions] = np.square(centered @ directions[0])
            if not np.isfinite(scores[positions]).all():
                raise ValueError("Spectral scores are not finite; check feature scaling.")
            ratios[name] = ratio
            class_positions[name] = positions
        if not ratios:
            raise ValueError("No class has enough rows for spectral analysis.")
        zscores: dict[str, float] = {}
        if len(ratios) >= 4:
            values = np.asarray(list(ratios.values()))
            deviation = float(values.std())
            zscores = {
                name: (float((ratio - values.mean()) / deviation) if deviation > 1e-12 else 0.0)
                for name, ratio in ratios.items()
            }
        flagged_classes = tuple(
            name
            for name, ratio in ratios.items()
            if ratio > ratio_threshold
            or zscores.get(name, 0.0) > cross_class_z_threshold
        )
        candidates: list[int] = []
        for name in flagged_classes:
            positions = class_positions[name]
            candidate_count = ceil(len(positions) * fraction)
            ranking = np.argsort(-scores[positions], kind="stable")[:candidate_count]
            candidates.extend(int(position) for position in positions[ranking])
        if skipped:
            warnings.warn(
                "Spectral audit skipped undersized classes; inspect skipped_classes "
                "and do not interpret this result as complete.",
                AuditCoverageWarning,
                stacklevel=2,
            )
        return SpectralSignatureResult(
            flagged=bool(flagged_classes),
            candidate_positions=tuple(sorted(candidates)),
            scores=tuple(float(score) for score in scores),
            class_top_ratios=ratios,
            flagged_classes=flagged_classes,
            cross_class_zscores=zscores,
            skipped_classes=skipped,
            complete=not skipped,
            absolute_ratio_threshold=ratio_threshold,
            cross_class_z_threshold=float(cross_class_z_threshold),
            expected_poison_fraction=fraction,
        )

    def check_batch_drift(
        self,
        new_df: pd.DataFrame,
        reference_df: pd.DataFrame,
        *,
        feature_cols: Sequence[str],
        alpha: float = 0.01,
        min_effect_size: float = 0.1,
    ) -> BatchDriftResult:
        significance = probability(alpha, "alpha")
        effect_threshold = probability(min_effect_size, "min_effect_size")
        new = _features(new_df, feature_cols, min_rows=2)
        reference = _features(reference_df, feature_cols, min_rows=2)
        results: dict[str, FeatureDrift] = {}
        for index, name in enumerate(feature_cols):
            statistic, p_value = ks_2samp(
                new[:, index], reference[:, index], method="auto"
            )
            adjusted = min(float(p_value) * len(feature_cols), 1.0)
            with np.errstate(over="raise", invalid="raise"):
                deviation = float(reference[:, index].std(ddof=1))
                shift = float(new[:, index].mean() - reference[:, index].mean())
            mean_shift = shift / deviation if deviation > 0 else None
            if mean_shift is not None and not isfinite(mean_shift):
                raise ValueError("Standardized drift is not finite; check feature scaling.")
            results[name] = FeatureDrift(
                flagged=bool(adjusted <= significance and statistic >= effect_threshold),
                ks_statistic=float(statistic),
                p_value=float(p_value),
                adjusted_p_value=adjusted,
                mean_shift_std=mean_shift,
                reference_constant=deviation == 0,
            )
        return BatchDriftResult(
            flagged=any(result.flagged for result in results.values()),
            features=results,
            new_rows=len(new),
            reference_rows=len(reference),
            alpha=significance,
            min_effect_size=effect_threshold,
        )

    def record_batch(
        self,
        *,
        batch_id: str,
        source: str,
        row_count: int,
        transformations: Sequence[str],
        audit_results: Mapping[str, object],
        ingested_at: datetime | None = None,
    ) -> dict[str, object]:
        if not isinstance(batch_id, str) or not batch_id.strip():
            raise ValueError("batch_id must be a nonempty provenance identifier.")
        if not isinstance(source, str) or not source.strip():
            raise ValueError("source must be a nonempty provenance identifier.")
        parsed = urlsplit(source)
        if (
            parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or re.search(r"(?i)(password|pwd|accountkey|token|secret)\s*=", source)
        ):
            raise ValueError("Use a source identifier without credentials or URL parameters.")
        count = positive_int(row_count, "row_count", minimum=0)
        if not isinstance(transformations, Sequence) or isinstance(
            transformations, (str, bytes)
        ) or any(
            not isinstance(step, str) or not step.strip() for step in transformations
        ):
            raise ValueError("transformations must be a sequence of nonempty step names.")
        if not isinstance(audit_results, Mapping) or not audit_results:
            raise ValueError("audit_results must be a nonempty mapping.")
        if ingested_at is not None and (
            not isinstance(ingested_at, datetime)
            or ingested_at.tzinfo is None
            or ingested_at.utcoffset() is None
        ):
            raise ValueError("ingested_at must be a timezone-aware datetime when known.")
        record: dict[str, object] = {
            "schema_version": 1,
            "event_id": str(uuid4()),
            "batch_id": batch_id,
            "source": source,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "ingested_at": (
                ingested_at.astimezone(timezone.utc).isoformat()
                if ingested_at is not None
                else None
            ),
            "row_count": count,
            "transformations": list(transformations),
            "audit_results": _json_value(audit_results),
        }
        line = json.dumps(record, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(line + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        return record
