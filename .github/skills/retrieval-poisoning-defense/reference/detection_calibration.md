# Detection calibration and interpretation

Defaults are starting points, not production policy. None of these detectors
certifies that data is clean or that a model is free of backdoors.

## Prepare the evaluation

Use three separate, provenance-verified sets: a clean fitting reference, a
held-out clean calibration set, and a final evaluation set with known clean data
and approved defensive fixtures. Split by source/batch/time where duplicates or
near-duplicates would leak across the split. Never calibrate on the batch under
investigation or reuse the final evaluation set to choose thresholds.

Keep embedding model/deployment/version, vector dimension, text preprocessing,
feature names/order/units, label meaning and transformation version identical.
Fit feature scaling on the trusted fitting set, then freeze it for later batches.
Validate a known vector after decoding a new transport format. Finite numbers
with the right dimension can still represent the wrong byte order or model.

Measure per-source and per-category false-positive rate, recall on labeled
fixtures, candidate-review volume, unassessed coverage, latency and cost. Record
sample sizes and uncertainty; a small clean set with zero flags does not establish
a zero false-positive rate. Thresholds belong to a versioned evaluation, not to a
service brand.

## EmbeddingAnomalyDetector

| Parameter | Default | Meaning |
| --- | --- | --- |
| `mahalanobis_quantile` | `0.99` | Held-out upper distance quantile and complementary lower cosine quantile |
| `min_cosine_similarity` | `0.5` | Fallback lower mean top-neighbor cosine similarity without held-out calibration |
| `neighbor_similarity` | `0.8` | Cosine cutoff for a high-similarity unrelated reference |
| `concentration_threshold` | `0.2` | Upper macro-average fraction of high-similarity unrelated-category references |
| `n_neighbors` | `5` | Maximum number of claimed-category neighbors averaged for cosine similarity |
| `min_reference_samples` | `5` | Minimum rows per reference/calibration category, not a claim of statistical adequacy |

Raw-space distance is
`sqrt((x - mu)^T (Sigma + ridge I)^-1 (x - mu))`. Ledoit-Wolf shrinkage plus a
small positive ridge handles low-rank references without silently ignoring
off-subspace directions. The ridge is numerical stabilization, not an attack
threshold. A fixed per-category amplitude scale keeps covariance calculations
stable for very small or large embedding units; it does not normalize away a
candidate's magnitude relative to its reference. High-dimensional covariance
remains costly and needs enough diverse
clean data; five rows in a 1,536-dimensional space is not reliable calibration.

Without a held-out calibration mapping, the distance threshold is
`sqrt(chi2.ppf(0.99, dimensions))`, a Gaussian approximation only. Supplying
`calibration_embeddings_by_category` replaces distance and lower cosine thresholds
with that category's empirical clean quantiles. It does not automatically
calibrate the unrelated-neighbor cutoff or concentration threshold.

For concentration, compute the fraction of unrelated reference vectors above the
cosine cutoff separately for each category, then average those fractions. This
prevents a large category alone from setting the score. Supply representative
`query_embeddings_by_category` to measure similarity to queries; otherwise the
fit-set document directions are explicitly only a proxy. A single category
without unrelated queries returns `neighbor_concentration=None` and a named
`unavailable_checks` entry, not a fabricated zero.

Select thresholds jointly against the review budget: combining several tests
with OR can raise the overall false-positive rate above each test's nominal rate.
Unknown categories, zero/nonfinite vectors and dimension mismatches are errors.
Do not auto-create an unknown category from the suspect vector.

## ContentSanitizer and SemanticInjectionClassifier

Regex findings are deterministic pattern/format signals, not probabilities.
Test legitimate code examples, AI-safety documentation, multilingual content,
HTML and meaningful Unicode (including joiners) from the real corpus. Some of
these must be reviewed as false positives rather than silently allowlisted.
Compare source and normalized text; normalization must not erase the fact that
a suspicious instruction was detected.

The semantic default is `similarity_threshold=0.80`, using maximum cosine
similarity to a versioned seed bank. Sweep that threshold on held-out approved
examples from each source and language. Embedding model changes invalidate a
previous threshold and its cached seed vectors.

Semantic proximity is not an attack probability. Content legitimately discussing
injection or model safety can be close to the seed bank. Attack text buried in
long benign passages can be diluted. If inputs exceed the supported limit, stop
and use a reviewed chunking policy with overlap and document-level aggregation;
never truncate and mark the rest evaluated. Arbitrary encodings and images are
outside a plain-text scanner's coverage.

Reuse a classifier instance to cache the seed bank. Initialization embeds the
bank once; scoring ordinarily makes one embeddings request per document.
Account for SDK retries, input size, throughput, endpoint policies and cost.
Do not skip the semantic layer based solely on a negative regex result.
SDK/mock tests check algorithm and integration behavior, not real-service recall.

## k-NN label-flip checks

`check_label_flips` uses `n_neighbors=5` and
`disagreement_threshold=0.8`. A row is flagged when at least that fraction of its
other nearest neighbors has a different label. The row itself is removed by
position, including when duplicate vectors tie for distance. At least `k + 1`
rows are required.

Sweep `k` and the disagreement threshold using class-balanced examples with
trusted, fixed feature scaling. Naturally overlapping classes, rare subtypes and
bad feature representations can produce disagreements without label corruption.
Feature columns must exclude the label itself.

A consistently mislabeled isolated cluster can have zero disagreement. Do not
increase trust just because a large cluster votes unanimously for its own label.
Run spectral and reference-based checks independently.

## Spectral signature checks

For each sufficiently large class, center the feature matrix and compute its SVD.
The reported `class_top_ratios` are **variance fractions**:
`sigma_1^2 / sum(sigma_i^2)`, not an unsquared singular-value ratio. Row scores are
the squared projections on that class's leading right singular vector.

| Parameter | Default | Effect |
| --- | --- | --- |
| `absolute_ratio_threshold` | `0.5` | Primary class flag when leading variance fraction exceeds it |
| `expected_poison_fraction` | `0.15` | Surface `ceil(class_rows * fraction)` candidates from each flagged class |
| `cross_class_z_threshold` | `1.5` | Secondary high-ratio population z-score signal, only for 4+ evaluable classes |
| `min_class_size` | `10` | Skip and explicitly report smaller classes |

`expected_poison_fraction` is a review budget, not an estimate of actual
contamination, not the number of known poisoned rows, and not a batch-flag
threshold. A flagged class stays flagged when this budget changes.

The primary threshold is deliberately absolute. With two classes, population
z-scores are at most `+1` and `-1`; a high relative-only threshold cannot trigger.
With three classes the maximum remains constrained (`sqrt(2)`). The secondary
comparison is only computed with four or more evaluable classes and is still a
relative heuristic, not a statistical guarantee.

Require at least two feature dimensions. In two dimensions the top variance
fraction is always at least `0.5`, and low-dimensional or naturally correlated
clean data will routinely exceed that default. Calibrate for actual intrinsic
dimension, class size, class imbalance and source diversity. The default is not
appropriate for every feature space. Identical constant rows have zero variance,
so the implementation reports ratio zero rather than dividing by zero.

An undersized class is included in `skipped_classes`, emits an
`AuditCoverageWarning`, and makes `complete=False`; if no class is evaluable the
method raises. Do not treat skipped coverage as a clean result.

The deterministic regression has 240 clean rows per class and 40 tightly grouped
rows consistently labeled as class 1 away from both clean clusters. k-NN catches
0 of those 40; the spectral candidate set must contain all 40. The default 15%
review budget for the 280-row class produces 42 candidates, so complete recall
does **not** imply zero false positives. These fixture-specific numbers are
asserted by the executable tests, not promised for real data.

## Batch drift

`check_batch_drift` runs a two-sample Kolmogorov-Smirnov test per feature.
`alpha=0.01` applies after Bonferroni correction
(`adjusted_p=min(raw_p * feature_count, 1)`). A feature is flagged only when that
adjusted p-value is at most alpha **and** its KS statistic is at least
`min_effect_size=0.1`. Both batches need at least two rows; realistic evaluation
usually needs many more.

Tune effect size and significance separately so huge batches do not flag
operationally negligible changes. The assumptions behind p-values can be violated
by repeated users, time autocorrelation or duplicate telemetry; use matched
sampling/windows and validate the observed clean flag rate.

A constant reference feature has `mean_shift_std=None` and
`reference_constant=True` rather than infinity; the KS signal still evaluates
distribution changes. Joint-only drift may pass these marginal tests.

For ADX, match weekday/time of day, load, deployment, source mix and retention
changes. Comparing an overnight baseline to a business-hours batch is not
evidence of an attack. For SQL and PostgreSQL, use the same calibration as the
detector for the table's role: there are no engine-specific new thresholds.

## Persist and review the evaluation

Record source/batch IDs, real ingestion time, feature/model/reference versions,
detector parameters, omissions and reviewer decisions alongside results.
`record_batch` distinguishes `recorded_at` from optional `ingested_at`; unknown
source ingestion time stays null. Supply timezone-aware times, not local naive
timestamps. Logs serialize result dataclasses and reject nonfinite numbers
before appending. An I/O error must prevent a claim that evidence was recorded.
Dataclass fields marked with `metadata={"audit": False}` are omitted recursively,
so bundled content results can retain normalized text in memory without writing
that text to lineage. This is not a general secret scrubber: never put source
documents, tokens or connection strings in arbitrary audit-result dictionaries.

Promote a baseline or threshold only after a reviewed evaluation. Reassess after
schema/model/feature changes, and never use this skill to automatically delete
candidate rows or manufacture replacement labels.
