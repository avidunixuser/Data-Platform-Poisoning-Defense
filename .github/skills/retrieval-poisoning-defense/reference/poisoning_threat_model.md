# Poisoning threat model

## Scope and distinction from evasion

This skill addresses changes to stored data, an ingestion feed, a search corpus,
or training features/labels. The adversary can write directly, compromise an
upstream producer, publish content that is crawled, or exploit an unvalidated
import. The poisoned data can persist and affect many subsequent queries or
training runs.

FGSM/PGD-style evasion instead perturbs an input presented to a model at inference
time; its canonical attacks use gradients. Not all evasion attacks require
gradient access, and not every data anomaly is poisoning. This skill neither
implements adversarial training nor establishes certified robustness.

An ingestion-time instruction can cause an inference-time indirect injection when
retrieved later. These are two stages of the same incident; sanitization at the
first stage does not remove the need for instruction/data isolation at the second.

## Services and trust boundaries

| Service | Stored material / writer boundary | Potential effect | Detection and investigation |
| --- | --- | --- | --- |
| Azure Cosmos DB vector store | Embeddings, content, category and provenance metadata; ingestion or container writers | Wrong content ranks for unrelated queries; broad-query hub behavior | Category-aware distance/cosine checks, unrelated query-bank concentration, trusted re-embedding, writer and source lineage |
| Azure AI Search | Crawled/pushed text, chunks, metadata and embeddings | Retrieved content instructs a downstream LLM or contaminates answers | Independent regex/Unicode and semantic screening; review source ACLs, crawlers, chunking and ingestion identities |
| Microsoft Fabric Lakehouse | Raw/curated features, training labels, transformations and partitions | Label corruption, learned backdoors or blind spots | Local-neighbor disagreements, class-level spectral signatures, matched-reference drift and transformation lineage |
| Azure SQL Database | Relational rows, native VECTOR data, features or LLM-facing text | Depends on the table's downstream role | SQL connector into the same vector, tabular or text detectors |
| Azure Database for PostgreSQL | pgvector/native driver vectors, features or LLM-facing text | Depends on the table's downstream role | SQL connector with verified serialization into the same detectors |
| Azure Data Explorer | Continuous telemetry/log events, optional labels and free text | Skewed metrics/features; indirect injection into log copilots | Time-windowed read-only KQL into drift, conditional label/spectral checks, and both text layers |

Transport compatibility is not a detection algorithm. Do not describe SQLite
coverage as evidence that every Azure SQL or pgvector driver transports vectors
correctly. Do not assume all telemetry has meaningful categorical training labels.

## What the signals establish

**Embedding anomalies.** Mahalanobis distance measures deviation from the trusted
category's raw embedding distribution. Cosine checks measure direction relative
to category neighbors and unrelated-category vectors. A high similarity to many
unrelated references is a review signal. When those references are documents
rather than representative queries, actual query hijacking has not been measured.
Attacks that stay inside the reference distribution can evade all three signals.
An attacker-controlled category can undermine the comparison; validate its origin.

**Content screening.** Regex detects recognizable instruction patterns and
suspicious representation. Embedding similarity tests proximity to known
strategies, not intent, authority, factual accuracy, or entailment. A security
article quoting an injection may be flagged. Novel strategies, multilingual
content, encoded/multimodal content and long-text dilution may evade screening.
Unicode removal is not safe HTML rendering or complete input sanitization.

**Label disagreements.** k-NN asks whether a row's label differs from nearby
feature-space labels, excluding the row itself. Scattered minority flips can
disagree with clean neighbors. A tight isolated cluster consistently given the
wrong label agrees with itself, so neighborhood consensus has no independent
ground truth.

**Spectral signatures.** Center features within each class and inspect the share
of variance in its leading singular direction. A separated coherent subcluster
can create a dominant direction even when local labels agree. Rows with large
squared projections are candidates for review. This is an inspired-by-the-paper
heuristic over supplied features, not a full reproduction or guarantee of the
original backdoor-defense procedure. Legitimate correlations, subpopulations,
low intrinsic dimensionality and small classes can look the same.

The 40-row regression is a **local-majority** scenario: all neighbors within the
isolated cluster share the corrupted label, but the cluster is a minority of its
whole class. Spectral methods can also fail, especially when contamination
dominates an entire class or does not create a distinguishable direction.

**Drift.** Per-feature distribution changes can indicate poisoning, schema changes,
seasonality, changing users, sampling bias, or legitimate product changes. These
univariate tests do not detect all joint/correlation-only shifts. A drift flag
is not a finding of malicious causation.

## Layered defensive controls

1. Restrict producers/writers to least privilege. Separate source ownership,
   ingestion, review, baseline publication, and model deployment identities.
2. Preserve immutable source/version identifiers, ingestion timestamps and
   transformations. Verify authorized provenance before trusting category labels
   or using a corpus as a reference.
3. Evaluate bounded batches before publication and periodically audit existing
   data. Prevent races: bind an approval to the actual version/hash that will be
   written, not just an ID that a writer can change afterward.
4. Hold flagged or incompletely evaluated items in an access-controlled queue.
   Retain originals for investigation. Human review, not score ranking alone,
   determines deletion, relabeling, rollback, or release.
5. Protect reference datasets and detector configuration from the ingestion
   identity. Version approved changes; never automatically learn a new "clean"
   baseline from suspicious incoming batches.
6. At retrieval, keep source text in an untrusted-data channel. Enforce system
   instruction precedence, tool allowlists and authorization in application code.
   Provenance/citations aid investigation but do not make instructions trustworthy.
7. Monitor reviewed false positives and confirmed misses by category/source/model.
   Recalibrate after an embedding model, feature transform, schema or source change.

## Operational and evidence boundaries

These scripts do not write to cloud datastores, remove documents, or train a
model. The only built-in persistent application write is an explicitly requested
local JSONL lineage record; the offline demo creates disposable synthetic SQLite
data. A service adapter's read-only checks are defense in depth, not a replacement
for server-side authorization.

Lineage records can themselves be sensitive. Keep source identifiers opaque and
exclude raw documents, free-text logs, connection strings and keys. The local log
does not prove integrity, provide a distributed append protocol, or prevent a
privileged local actor from editing it. Use an approved protected evidence sink
and retention policy for production. Missing source ingestion time is stored as
unknown, separately from the time the audit record was written.

## Sources

- Tran, Li and Madry (2018), [Spectral Signatures in Backdoor Attacks](https://arxiv.org/abs/1811.00636).
- OWASP, [LLM Prompt Injection Prevention Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/LLM_Prompt_Injection_Prevention_Cheat_Sheet.html).
- Microsoft, [Azure AI Search security overview](https://learn.microsoft.com/azure/search/search-security-overview).
