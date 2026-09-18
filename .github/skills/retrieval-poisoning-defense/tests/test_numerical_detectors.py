from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import _test_support  # noqa: F401
import numpy as np
import pandas as pd

from embedding_anomaly_detector import EmbeddingAnomalyDetector
from lineage_audit import AuditCoverageWarning, LineageAuditor
from smoke_test import majority_cluster_fixture


class EmbeddingTests(unittest.TestCase):
    def setUp(self) -> None:
        rng = np.random.default_rng(21)
        self.reference = {
            "product_docs": rng.normal([3, 0, 0, 0], 0.12, (120, 4)),
            "other_docs": rng.normal([0, 3, 0, 0], 0.12, (120, 4)),
        }
        self.detector = EmbeddingAnomalyDetector().fit_reference(self.reference)

    def test_trusted_center_is_not_flagged(self) -> None:
        result = self.detector.score([3, 0, 0, 0], category="product_docs")
        self.assertFalse(result.flagged)
        self.assertEqual(result.reasons, ())
        self.assertEqual(result.unavailable_checks, ())

    def test_magnitude_outlier_cannot_hide_behind_cosine(self) -> None:
        result = self.detector.score([90, 0, 0, 0], category="product_docs")
        self.assertIn("mahalanobis_outlier", result.reasons)
        self.assertGreater(result.cosine_similarity, 0.9)

    def test_category_mismatch_and_unrelated_concentration(self) -> None:
        result = self.detector.score([0, 3, 0, 0], category="product_docs")
        self.assertIn("category_cosine_outlier", result.reasons)
        self.assertIn("unrelated_neighbor_concentration", result.reasons)
        self.assertGreater(result.neighbor_concentration, 0.95)

    def test_explicit_query_bank_is_used(self) -> None:
        self.detector.fit_reference(
            self.reference,
            query_embeddings_by_category={"unrelated_queries": np.tile([3, 0, 0, 0], (8, 1))},
        )
        result = self.detector.score([3, 0, 0, 0], category="product_docs")
        self.assertEqual(result.neighbor_concentration, 1.0)
        self.assertIn("unrelated_neighbor_concentration", result.reasons)

    def test_missing_unrelated_reference_is_explicit(self) -> None:
        detector = EmbeddingAnomalyDetector().fit_reference(
            {"product_docs": self.reference["product_docs"]}
        )
        result = detector.score([3, 0, 0, 0], category="product_docs")
        self.assertIsNone(result.neighbor_concentration)
        self.assertIn("unrelated_neighbor_concentration", result.unavailable_checks)

    def test_held_out_calibration_changes_thresholds(self) -> None:
        rng = np.random.default_rng(84)
        calibration = {
            "product_docs": rng.normal([3, 0, 0, 0], 0.2, (120, 4)),
            "other_docs": rng.normal([0, 3, 0, 0], 0.2, (120, 4)),
        }
        before = self.detector.score([3, 0, 0, 0], category="product_docs")
        self.detector.fit_reference(
            self.reference, calibration_embeddings_by_category=calibration
        )
        after = self.detector.score([3, 0, 0, 0], category="product_docs")
        self.assertNotEqual(before.mahalanobis_threshold, after.mahalanobis_threshold)
        self.assertGreater(after.cosine_threshold, before.cosine_threshold)
        self.assertFalse(after.flagged)

    def test_constant_reference_still_detects_off_subspace_vectors(self) -> None:
        detector = EmbeddingAnomalyDetector().fit_reference(
            {"constant": np.tile([1, 0, 0], (8, 1))}
        )
        self.assertFalse(detector.score([1, 0, 0], category="constant").flagged)
        self.assertTrue(detector.score([1, 1, 0], category="constant").flagged)

    def test_distance_is_stable_across_embedding_units(self) -> None:
        original = self.detector.score([90, 0, 0, 0], category="product_docs")
        for scale in (1e-150, 1e150):
            detector = EmbeddingAnomalyDetector().fit_reference(
                {category: values * scale for category, values in self.reference.items()}
            )
            result = detector.score(
                np.array([90, 0, 0, 0]) * scale, category="product_docs"
            )
            self.assertTrue(result.flagged)
            self.assertAlmostEqual(
                result.mahalanobis_distance, original.mahalanobis_distance, places=8
            )

    def test_not_fitted_and_unknown_category_fail(self) -> None:
        with self.assertRaises(RuntimeError):
            EmbeddingAnomalyDetector().score([1], category="anything")
        with self.assertRaises(ValueError):
            self.detector.score([1, 0, 0, 0], category="unknown")

    def test_invalid_vectors_fail_not_clean(self) -> None:
        for vector in (
            [1, 2], [0, 0, 0, 0], [1, 0, np.nan, 0], [np.inf] * 4,
            ["1"] * 4, [1, 0, True, 0],
            np.ma.array([1, 0, 0, 0], mask=[False, True, False, False]),
        ):
            with self.subTest(vector=vector), self.assertRaises(ValueError):
                self.detector.score(vector, category="product_docs")

    def test_reference_shape_and_calibration_are_strict(self) -> None:
        invalid = [
            {},
            {"small": np.ones((2, 4))},
            {"zero": np.zeros((8, 4))},
            {"a": np.ones((8, 4)), "b": np.ones((8, 3))},
            {"": np.ones((8, 4))},
        ]
        for reference in invalid:
            with self.subTest(reference=list(reference)), self.assertRaises(ValueError):
                self.detector.fit_reference(reference)
        with self.assertRaises(ValueError):
            self.detector.fit_reference(
                self.reference, calibration_embeddings_by_category={}
            )
        self.assertFalse(self.detector.score([3, 0, 0, 0], category="product_docs").flagged)

    def test_invalid_parameters_fail(self) -> None:
        for parameters in (
            {"mahalanobis_quantile": 1},
            {"mahalanobis_quantile": np.nan},
            {"min_cosine_similarity": 2},
            {"concentration_threshold": -1},
            {"n_neighbors": True},
            {"min_reference_samples": 2},
        ):
            with self.subTest(parameters=parameters), self.assertRaises(ValueError):
                EmbeddingAnomalyDetector(**parameters)


class LineageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.auditor = LineageAuditor()
        self.frame, self.poison_positions = majority_cluster_fixture()
        self.features = [f"x{index}" for index in range(6)]

    def test_scattered_minority_flip_is_detected(self) -> None:
        frame = self.frame.iloc[:480].copy()
        frame.loc[0, "label"] = 1
        result = self.auditor.check_label_flips(
            frame, feature_cols=self.features, label_col="label"
        )
        self.assertTrue(result.flagged)
        self.assertIn(0, result.flagged_positions)

    def test_majority_cluster_exact_recall_regression(self) -> None:
        local = self.auditor.check_label_flips(
            self.frame, feature_cols=self.features, label_col="label"
        )
        spectral = self.auditor.check_spectral_signature(
            self.frame, feature_cols=self.features, label_col="label"
        )
        self.assertEqual(len(set(local.flagged_positions) & self.poison_positions), 0)
        self.assertEqual(len(set(spectral.candidate_positions) & self.poison_positions), 40)
        self.assertFalse(local.flagged)
        self.assertTrue(spectral.flagged)
        self.assertGreater(spectral.class_top_ratios["1"], 0.5)
        self.assertLess(spectral.class_top_ratios["0"], 0.5)
        self.assertEqual(spectral.cross_class_zscores, {})

    def test_review_budget_does_not_decide_batch_flag(self) -> None:
        results = [
            self.auditor.check_spectral_signature(
                self.frame,
                feature_cols=self.features,
                label_col="label",
                expected_poison_fraction=fraction,
            )
            for fraction in (0.02, 0.15, 0.4)
        ]
        self.assertEqual([result.flagged for result in results], [True] * 3)
        self.assertLess(len(results[0].candidate_positions), len(results[1].candidate_positions))
        self.assertEqual(results[0].class_top_ratios, results[2].class_top_ratios)

    def test_one_or_three_classes_use_absolute_signal(self) -> None:
        for frame in (
            self.frame[self.frame.label == 1],
            pd.concat(
                [self.frame, self.frame.iloc[:40].assign(label=2)], ignore_index=True
            ),
        ):
            with self.subTest(classes=frame.label.nunique()):
                result = self.auditor.check_spectral_signature(
                    frame, feature_cols=self.features, label_col="label"
                )
                self.assertTrue(result.flagged)
                self.assertEqual(result.cross_class_zscores, {})

    def test_four_class_comparison_is_secondary_not_required(self) -> None:
        rng = np.random.default_rng(33)
        frame = pd.concat(
            [
                self.frame,
                pd.DataFrame(rng.normal(size=(80, 6)), columns=self.features).assign(label=2),
                pd.DataFrame(rng.normal(size=(80, 6)), columns=self.features).assign(label=3),
            ],
            ignore_index=True,
        )
        result = self.auditor.check_spectral_signature(
            frame, feature_cols=self.features, label_col="label"
        )
        self.assertEqual(set(result.cross_class_zscores), {"0", "1", "2", "3"})
        self.assertIn("1", result.flagged_classes)
        secondary_only = self.auditor.check_spectral_signature(
            frame,
            feature_cols=self.features,
            label_col="label",
            absolute_ratio_threshold=1.0,
        )
        self.assertIn("1", secondary_only.flagged_classes)

    def test_spectral_ratio_is_explained_variance_not_unsquared_singular_value(self) -> None:
        matrix = np.tile([[2, 0], [-2, 0], [0, 1], [0, -1]], (5, 1))
        frame = pd.DataFrame(matrix, columns=["x", "y"]).assign(label=0)
        result = self.auditor.check_spectral_signature(
            frame, feature_cols=["x", "y"], label_col="label"
        )
        self.assertAlmostEqual(result.class_top_ratios["0"], 0.8)

    def test_constant_class_is_not_spectral_anomaly(self) -> None:
        frame = pd.DataFrame({"x": [0.1] * 20, "y": [0.1] * 20, "label": [1] * 20})
        result = self.auditor.check_spectral_signature(
            frame, feature_cols=["x", "y"], label_col="label"
        )
        self.assertFalse(result.flagged)
        self.assertEqual(result.class_top_ratios, {"1": 0.0})

    def test_undersized_class_is_visible_not_silently_clean(self) -> None:
        frame = pd.concat(
            [self.frame, self.frame.iloc[:2].assign(label=2)], ignore_index=True
        )
        with self.assertWarns(AuditCoverageWarning):
            result = self.auditor.check_spectral_signature(
                frame, feature_cols=self.features, label_col="label"
            )
        self.assertFalse(result.complete)
        self.assertIn("2", result.skipped_classes)

    def test_self_neighbor_removed_by_position_with_duplicate_vectors(self) -> None:
        frame = pd.DataFrame({"x": [1] * 6, "y": [1] * 6, "label": [1, 0, 0, 0, 0, 0]})
        result = self.auditor.check_label_flips(
            frame, feature_cols=["x", "y"], label_col="label", n_neighbors=5
        )
        self.assertEqual(result.neighbor_disagreement[0], 1.0)
        self.assertEqual(result.flagged_positions, (0,))

    def test_positions_are_not_dataframe_index_labels(self) -> None:
        frame = self.frame.iloc[:480].copy()
        frame.loc[0, "label"] = 1
        frame.index = ["same"] * len(frame)
        result = self.auditor.check_label_flips(
            frame, feature_cols=self.features, label_col="label"
        )
        self.assertIn(0, result.flagged_positions)

    def test_shifted_batch_and_identical_reference(self) -> None:
        reference = self.frame.iloc[:200].copy()
        stable = self.auditor.check_batch_drift(
            reference.copy(), reference, feature_cols=self.features
        )
        shifted = reference.copy()
        shifted["x0"] += 5
        drift = self.auditor.check_batch_drift(
            shifted, reference, feature_cols=self.features
        )
        self.assertFalse(stable.flagged)
        self.assertTrue(drift.flagged)
        self.assertTrue(drift.features["x0"].flagged)
        self.assertFalse(drift.features["x1"].flagged)
        self.assertEqual(drift.correction, "bonferroni")
        self.assertLessEqual(drift.features["x0"].adjusted_p_value, drift.alpha)

    def test_constant_reference_drift_remains_json_safe(self) -> None:
        result = self.auditor.check_batch_drift(
            pd.DataFrame({"value": [1] * 50}),
            pd.DataFrame({"value": [0] * 50}),
            feature_cols=["value"],
        )
        self.assertTrue(result.flagged)
        self.assertTrue(result.features["value"].reference_constant)
        self.assertIsNone(result.features["value"].mean_shift_std)

    def test_bad_features_and_missing_labels_are_errors(self) -> None:
        frames = [
            self.frame.assign(x0=np.nan),
            self.frame.assign(x0="1"),
            self.frame.assign(x0=True),
            self.frame.assign(x0=1 + 2j),
            self.frame.assign(label=None),
        ]
        for frame in frames:
            with self.subTest(dtype=str(frame.x0.dtype)), self.assertRaises(ValueError):
                self.auditor.check_label_flips(
                    frame, feature_cols=self.features, label_col="label"
                )
        with self.assertRaises(ValueError):
            self.auditor.check_label_flips(
                self.frame, feature_cols=["x0", "label"], label_col="label"
            )
        with self.assertRaises(ValueError):
            self.auditor.check_spectral_signature(
                self.frame, feature_cols=["x0"], label_col="label"
            )
        with self.assertRaises(ValueError):
            self.auditor.check_label_flips(
                self.frame.iloc[:3], feature_cols=self.features, label_col="label"
            )

    def test_audits_do_not_mutate_input(self) -> None:
        original = self.frame.copy(deep=True)
        self.auditor.check_label_flips(
            self.frame, feature_cols=self.features, label_col="label"
        )
        self.auditor.check_spectral_signature(
            self.frame, feature_cols=self.features, label_col="label"
        )
        pd.testing.assert_frame_equal(self.frame, original)

    def test_record_batch_persists_dataclasses_and_utc_ingestion_time(self) -> None:
        result = self.auditor.check_label_flips(
            self.frame, feature_cols=self.features, label_col="label"
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "audit" / "lineage.jsonl"
            auditor = LineageAuditor(path)
            for number in range(2):
                record = auditor.record_batch(
                    batch_id=f"batch-{number}",
                    source="fabric://lakehouse/raw/events",
                    row_count=len(self.frame),
                    transformations=["deduplicate", "trusted-scale-v1"],
                    audit_results={"label_flip": result},
                    ingested_at=datetime(2026, 9, 17, tzinfo=timezone.utc),
                )
                self.assertEqual(record["ingested_at"], "2026-09-17T00:00:00+00:00")
            lines = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(len(lines), 2)
            self.assertNotEqual(lines[0]["event_id"], lines[1]["event_id"])
            self.assertFalse(lines[0]["audit_results"]["label_flip"]["flagged"])
            self.assertIn("recorded_at", lines[0])

    def test_failed_serialization_never_appends_partial_line(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "lineage.jsonl"
            auditor = LineageAuditor(path)
            for audit in ({"score": np.nan}, {"score": np.inf}, {"data": object()}):
                with self.subTest(audit=audit):
                    with self.assertRaises((ValueError, TypeError)):
                        auditor.record_batch(
                            batch_id="batch", source="source", row_count=1,
                            transformations=[], audit_results=audit,
                        )
            self.assertFalse(path.exists())

    def test_dataclass_fields_marked_private_are_not_logged(self) -> None:
        @dataclass(frozen=True)
        class ContentExample:
            flagged: bool
            normalized_text: str = field(metadata={"audit": False})

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "lineage.jsonl"
            LineageAuditor(path).record_batch(
                batch_id="batch",
                source="source",
                row_count=1,
                transformations=[],
                audit_results={"content": ContentExample(True, "private document")},
            )
            saved = path.read_text(encoding="utf-8")
            self.assertNotIn("private document", saved)
            self.assertNotIn("normalized_text", saved)
            self.assertTrue(json.loads(saved)["audit_results"]["content"]["flagged"])

    def test_record_rejects_secret_urls_and_unaware_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            auditor = LineageAuditor(Path(temporary) / "lineage.jsonl")
            for source in (
                "https://user:secret@example.test/data",
                "https://example.test/data?token=secret",
                "Server=example;Password=secret",
            ):
                with self.subTest(source=source), self.assertRaises(ValueError):
                    auditor.record_batch(
                        batch_id="batch", source=source, row_count=1,
                        transformations=[], audit_results={"status": "review"},
                    )
            with self.assertRaises(ValueError):
                auditor.record_batch(
                    batch_id="batch", source="source", row_count=1,
                    transformations=[], audit_results={"status": "review"},
                    ingested_at=datetime(2026, 9, 17),
                )

    def test_log_io_failure_is_not_reported_as_success(self) -> None:
        with patch.object(Path, "open", side_effect=PermissionError("not writable")):
            with self.assertRaises(PermissionError):
                self.auditor.record_batch(
                    batch_id="batch", source="source", row_count=1,
                    transformations=[], audit_results={"status": "review"},
                )


if __name__ == "__main__":
    unittest.main()
