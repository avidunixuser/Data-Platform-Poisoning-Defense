# Data Platform Poisoning Defense

A reusable **GitHub Copilot agent skill** for ingestion-time poisoning defense
across Azure Cosmos DB, Azure AI Search, Microsoft Fabric Lakehouse, Azure SQL
Database, Azure Database for PostgreSQL, and Azure Data Explorer.

The skill is in
[`.github/skills/retrieval-poisoning-defense`](.github/skills/retrieval-poisoning-defense/SKILL.md).
It implements the supplied retrieval-poisoning-defense guidelines as executable
Python detectors, read-only connectors, calibration guidance, and offline
regressions. It is separate from inference-time FGSM/PGD defenses.

## Microsoft Foundry coverage and gap assessment

**Assessment date: 2026-09-18.** Based on the public Microsoft documentation
linked below, the gap is a **turnkey, cross-datastore ingestion-time poisoning
defense**, not a general absence of prompt-injection protection in Foundry.
This is a documentation-based assessment, not confirmation from Microsoft's
product team or a statement about its private roadmap.

| Capability | Documented Microsoft coverage | Gap assessment |
| --- | --- | --- |
| Instructions hidden in retrieved documents | [Prompt Shields][foundry-prompt-shields] detects document attacks. [Foundry agent guardrails][foundry-guardrails] support tool-response screening, currently documented as preview. | Existing capability. This skill's regex and embedding-similarity checks overlap; they do not establish stronger protection. |
| Text screening before indexing or embedding | The standalone [Content Safety Prompt Shields API][content-safety-prompt-shields] accepts document inputs and can be called from an ingestion pipeline. | Integration work, not a missing Microsoft detector. |
| Anomalous embeddings, clusters, or retrieval concentration | Microsoft's [grounding-data-compromise guidance][grounding-data-compromise] recommends monitoring embedding clusters, retrieval distributions, and top-K changes. | No equivalent turnkey Foundry ingestion detector was identified in the reviewed documentation. Statistical checks still require workload-specific implementation and calibration. |
| Training-data label flips and suspicious spectral structure | Microsoft's [training-data-poisoning guidance][training-data-poisoning] recommends validation, anomaly detection, provenance, and secure MLOps. | No equivalent managed Foundry label-flip or spectral-signature detector was identified in the reviewed documentation. |
| Lineage, review, and quarantine across all six datastores | Microsoft Purview and other Azure services provide governance and monitoring components; the grounding and training guidance describes how to combine these controls. | An orchestration gap, not an absence of lineage or governance. No unified poisoning-detection and quarantine workflow matching this scope was identified. |

**Poisoned data need not contain malicious instructions.** Incorrect labels,
manipulated ranking metadata, and misleading but ordinary-looking content can
corrupt outcomes without triggering a prompt-injection detector. An answer being
grounded in a source also does not prove that the source is trustworthy.

Position this project as **an ingestion-time data-integrity companion to Foundry
guardrails**. Use native Prompt Shields and applicable runtime guardrails
alongside calibrated statistical checks, provenance, and review controls.
This repository supplies heuristic detectors and integration guidance; it does
not implement a managed, production-complete quarantine platform or demonstrate
that the broader gap is fully closed. Recheck the linked product documentation
before relying on this assessment as capabilities and preview status evolve.

[foundry-prompt-shields]: https://learn.microsoft.com/azure/foundry/openai/concepts/content-filter-prompt-shields
[foundry-guardrails]: https://learn.microsoft.com/azure/foundry/guardrails/guardrails-overview
[content-safety-prompt-shields]: https://learn.microsoft.com/azure/ai-services/content-safety/quickstart-jailbreak
[grounding-data-compromise]: https://learn.microsoft.com/security/zero-trust/catalog-ai-attack-techniques/grounding-data-compromise
[training-data-poisoning]: https://learn.microsoft.com/security/zero-trust/catalog-ai-attack-techniques/training-data-poisoning

## Does pinpointing an anomaly require agentic orchestration?

**Not for detecting and locating suspicious data.** Explaining its cause across
systems requires a broader investigation workflow, but that workflow does not
necessarily require an LLM or multiple agents.

| Level | What it establishes | What this repository provides |
| --- | --- | --- |
| Detect and localize | A supplied embedding is an outlier, particular rows have unusual spectral scores, or a feature's distribution has shifted. | Numerical detectors and calibration guidance. Callers must retain the mapping from row positions to stable source IDs and versions. |
| Diagnose and attribute | Whether a signal originated in source content, label changes, preprocessing, an embedding-model upgrade, or an unauthorized write. | Connectors and lineage-recording building blocks, not automatic cross-service root-cause tracing. Diagnosis needs trusted references and correlation across provenance, dataset versions, transformations, and audit logs. |
| Respond | Whether to hold a batch, rebuild an index, roll back a transformation, or release a false positive. | Review recommendations, not automatic remediation. Operational workflows, evidence preservation, and approvals must be supplied by the integrating application. |

Mahalanobis distance, k-NN, and singular value decomposition (SVD) are numerical
computations; they do not need an LLM to execute them. A conventional ingestion
or batch-audit pipeline can run the detectors, preserve the source IDs associated
with candidates, record evidence, and route findings to an existing review queue.
Semantic content screening uses the configured embedding service, but likewise
does not require multi-agent orchestration.

Agentic orchestration becomes useful when the investigation path is uncertain:
choosing which evidence to retrieve, following lineage across services, comparing
competing explanations, and producing a diagnosis supported by cited evidence.
An investigating agent should use the detectors as tools rather than substitute
its own judgment for numerical scores. It must treat suspect documents and tool
results as untrusted data, stay within authorized access, and keep consequential
write actions behind approval.

**A statistical anomaly is not proof of poisoning.** Legitimate drift or a
correlated subpopulation can produce the same signal. An agent cannot establish
malicious causation without corroborating evidence, and orchestration cannot
compensate for missing provenance or an untrusted baseline.

This skill supplies **detection and investigation components, not an autonomous
poisoning-investigation system**.

## Use the skill

Copilot discovers the repository's `SKILL.md` and loads it when relevant. For
example:

> Use retrieval-poisoning-defense to audit this RAG ingestion pipeline. Identify
> the data roles, run the applicable offline checks, and report flags and gaps
> without changing the source data.

For another repository, copy the **entire** `retrieval-poisoning-defense`
directory into its `.github/skills` directory. For personal Copilot use on
Windows, the corresponding location is
`$HOME\.copilot\skills\retrieval-poisoning-defense`. This repository does not
install anything globally or grant tools automatic approval.

### Is the skill itself called as a tool?

**The skill is an instruction-and-resource package, not a standalone audit API
or MCP tool.** A skill-aware host loads `SKILL.md` and makes its supporting
resources available to the agent. Some hosts expose that loading mechanism as a
tool or command; invoking the loader supplies guidance, not a completed audit.

The agent then **calls execution tools to do the work**: for example, an
authorized Python/shell tool running code that imports and calls the bundled
detectors and connectors. Another runtime can expose those Python APIs through
explicitly registered function tools. The skill explains when and how to use
them; the tools perform the reads and computations.

```text
User request + skill instructions + approved workload context
    -> authorized execution tool
    -> Python connectors and detectors
    -> structured evidence
    -> agent explanation and review recommendation
```

This repository does not deploy an MCP server or automatically register a
Foundry tool. For a custom or Foundry agent, make the instructions available and
provide an approved execution/tool layer, dependencies, configuration, and
scoped access. Cloning the repository or adding its text to a prompt does not
grant datastore access or start continuous monitoring. The Python APIs can also
run in a conventional pipeline without an agent.

### How an agent uses context to detect anomalies

1. **Load the playbook.** Match the request to the skill, read `SKILL.md`, and
   consult the relevant threat-model, calibration, and connector references.
   Reading these instructions alone does not detect poisoning.
2. **Establish the workload context.** Identify the approved source and batch or
   time window, schema, downstream data role, category/label definitions,
   embedding model/version/dimension, feature preprocessing, and source IDs.
   Obtain independently trusted fitting and held-out calibration data where
   applicable. Ask for missing information rather than treating the suspect
   batch as its own clean baseline. Conversation history and pretrained model
   knowledge are not substitutes for a trusted reference.
3. **Load bounded data and execute the detectors.** Use approved read-only
   queries and explicit vector decoding; verify schema, dimensions, and numeric
   validity. Run all applicable detector layers with calibrated thresholds.
   Compute distances and SVD in Python, not by asking the LLM to inspect arrays
   in its prompt. Obtain authorization before semantic screening sends text to
   the configured Azure embedding endpoint.
4. **Map evidence back to records.** Collect flags, measured scores, thresholds,
   reason codes, candidate row positions, affected classes/features, and
   unavailable checks. Preserve the caller's mapping to stable source IDs,
   versions, and batch lineage. Tabular candidate positions are zero-based
   positions for `df.iloc`, not business IDs or DataFrame index labels.
5. **Explain and record the outcome.** Report which signals triggered, what
   context supports them, and what remains unassessed. Record approved batch
   metadata and result summaries without raw document bodies or credentials.
   Errors, missing required checks, or partial results mean not evaluated/hold,
   never "clean". Actual quarantine or remediation belongs to a separately
   authorized application workflow.

The context is different for each detector:

| Data role | Context the detector uses |
| --- | --- |
| Embeddings | Trusted category distributions, held-out calibration vectors, matching model/preprocessing, and optionally representative query embeddings. Without a query bank, reference document vectors are only a proxy for query similarity. |
| Free-text documents or logs | Recognizable instruction/Unicode patterns and a small embedding-based bank of known injection examples. This is not a factual knowledge base or a test of document truthfulness. |
| Labeled training rows | Feature-space neighborhoods and within-class spectral structure, with trusted feature preprocessing and calibrated thresholds. These checks use relationships within the batch; they do not independently establish correct labels. |
| New telemetry or feature batches | A trusted reference window with comparable workload, seasonality, sampling, and preprocessing for distribution-drift comparison. |

### Example: audit a new product-document embedding

An example request with explicit scope is:

> Use retrieval-poisoning-defense to audit approved batch B-104 in our product
> document store against approved reference R-12. Keep source access read-only.
> Report document IDs, reference/model versions, scores, thresholds, reasons,
> and any checks that could not run.

After resolving the approved data and verifying its metadata, the agent calls
`EmbeddingAnomalyDetector.fit_reference(...)`, then
`detector.score(new_embedding, category="product_docs")` for each candidate.
If a calibrated distance or similarity check flags an item, the agent associates
that result with its original document ID and reports the supporting evidence
for review. It does not infer an attacker's intent from the score.

Distinguishing poisoning from a legitimate new topic, incorrect category
metadata, or an embedding-model change requires corroboration. With additional
authorized tools, an agent could inspect transformation history or writer audit
logs; this repository does not discover that history automatically. Suspect
documents and tool results remain untrusted data, never instructions to the
investigating agent.

## Included components

| Component | Purpose |
| --- | --- |
| [Embedding anomaly detector](.github/skills/retrieval-poisoning-defense/scripts/embedding_anomaly_detector.py) | Shrinkage Mahalanobis distance, category cosine checks and unrelated-neighbor concentration |
| [Content sanitizer](.github/skills/retrieval-poisoning-defense/scripts/content_sanitizer.py) | Regex/Unicode screening plus an independent Foundry embedding-similarity classifier |
| [Lineage auditor](.github/skills/retrieval-poisoning-defense/scripts/lineage_audit.py) | k-NN label disagreements, spectral signatures, batch drift and JSONL provenance |
| [Data connectors](.github/skills/retrieval-poisoning-defense/scripts/data_connectors.py) | Bounded SQL/PostgreSQL/Kusto reads with explicit vector decoding |
| [Offline smoke test](.github/skills/retrieval-poisoning-defense/scripts/smoke_test.py) | Synthetic cluster regression, SQLite-to-detector integration and persisted audit evidence |

## Run locally

Python 3.10+ is required. From the repository root in PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r .github\skills\retrieval-poisoning-defense\requirements.txt
.\.venv\Scripts\python.exe .github\skills\retrieval-poisoning-defense\scripts\smoke_test.py
.\.venv\Scripts\python.exe -m unittest discover -s .github\skills\retrieval-poisoning-defense\tests -v
```

On other platforms, use the equivalent virtual-environment Python executable.
The offline path needs no credentials and makes no Azure requests. The synthetic
regression checks the documented local-neighbor blind spot: k-NN catches 0/40
members of a consistently mislabeled isolated cluster, while the spectral review
candidate set contains all 40. This is fixture-specific, not a real-world recall
guarantee.

For approved live integrations, install the separate
[`requirements-azure.txt`](.github/skills/retrieval-poisoning-defense/requirements-azure.txt)
and follow the
[service integration guide](.github/skills/retrieval-poisoning-defense/reference/service_integration.md)
and [connector guide](.github/skills/retrieval-poisoning-defense/reference/data_connectors.md).
Azure SQL additionally requires a supported Microsoft ODBC driver.
Foundry embeddings use the current `openai` SDK against an explicitly configured
Azure endpoint, replacing the retired `azure-ai-inference` SDK named in the source
guidelines. No live Azure deployment or service validation is implied.

## Interpretation and safety

Read the [threat model](.github/skills/retrieval-poisoning-defense/reference/poisoning_threat_model.md)
and [calibration guide](.github/skills/retrieval-poisoning-defense/reference/detection_calibration.md)
before using any score as an ingestion gate. Flags mean **hold for review**, not
automatic deletion or relabeling. Errors and incomplete checks must not become
"clean" verdicts. Legitimate drift, correlated data and security documentation
can produce false positives; attacks can also evade these heuristics.

Retrieved content remains untrusted at query time. Keep credentials and raw data
out of chat, source control and audit logs, preserve source lineage, and require
authorization before sending text to an embedding endpoint. Local JSONL evidence
is single-writer and is not a tamper-evident production audit system.
