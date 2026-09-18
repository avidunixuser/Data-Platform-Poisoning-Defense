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
