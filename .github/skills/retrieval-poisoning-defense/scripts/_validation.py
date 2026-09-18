"""Shared strict numeric validation for defensive detectors."""

from __future__ import annotations

import math
from numbers import Integral, Real

import numpy as np
from numpy.typing import ArrayLike, NDArray


def positive_int(value: int, name: str, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}.")
    return int(value)


def probability(value: float, name: str, *, allow_zero: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a finite number in (0, 1].")
    number = float(value)
    if not math.isfinite(number) or number > 1 or number < 0:
        raise ValueError(f"{name} must be a finite number in [0, 1].")
    if number == 0 and not allow_zero:
        raise ValueError(f"{name} must be greater than zero.")
    return number


def _numeric_array(values: ArrayLike, name: str) -> NDArray:
    if np.ma.isMaskedArray(values):
        raise ValueError(f"{name} must not contain masked or unevaluated values.")
    try:
        raw = np.asarray(values)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{name} must be a rectangular numeric matrix.") from exc
    if raw.dtype.kind not in "iuf":
        raise ValueError(f"{name} must contain real numbers, not strings or booleans.")
    if not isinstance(values, np.ndarray):
        original = np.asarray(values, dtype=object)
        if any(isinstance(item, (bool, np.bool_)) for item in original.flat):
            raise ValueError(f"{name} must not contain booleans mixed with numbers.")
    return raw


def as_float_matrix(
    values: ArrayLike, name: str, *, min_rows: int = 1
) -> NDArray[np.float64]:
    raw = _numeric_array(values, name)
    if raw.ndim != 2 or raw.shape[0] < min_rows or raw.shape[1] < 1:
        raise ValueError(
            f"{name} must have at least {min_rows} rows and one feature column."
        )
    matrix = raw.astype(np.float64, copy=True)
    if not np.isfinite(matrix).all():
        raise ValueError(f"{name} must contain only finite values.")
    return matrix


def as_float_vector(
    values: ArrayLike, name: str, *, dimension: int | None = None
) -> NDArray[np.float64]:
    raw = _numeric_array(values, name)
    if raw.ndim != 1 or raw.size < 1:
        raise ValueError(f"{name} must be a nonempty one-dimensional vector.")
    if dimension is not None and raw.size != dimension:
        raise ValueError(f"{name} must have {dimension} dimensions; got {raw.size}.")
    return as_float_matrix(raw.reshape(1, -1), name)[0]


def unit_rows(values: ArrayLike, name: str) -> NDArray[np.float64]:
    matrix = as_float_matrix(values, name)
    # Scale first so finite, large inputs do not overflow while computing norms.
    scale = np.max(np.abs(matrix), axis=1, keepdims=True)
    if (scale == 0).any():
        raise ValueError(f"{name} must not contain zero-length vectors.")
    scaled = matrix / scale
    return scaled / np.linalg.norm(scaled, axis=1, keepdims=True)
