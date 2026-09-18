# Data Platform Poisoning Defense

A reusable **GitHub Copilot agent skill** for ingestion-time poisoning defense
across Azure Cosmos DB, Azure AI Search, Microsoft Fabric Lakehouse, Azure SQL
Database, Azure Database for PostgreSQL, and Azure Data Explorer.

The skill is in
[`.github/skills/retrieval-poisoning-defense`](.github/skills/retrieval-poisoning-defense/SKILL.md).
It implements the supplied retrieval-poisoning-defense guidelines as executable
Python detectors, read-only connectors, calibration guidance, and offline
regressions. It is separate from inference-time FGSM/PGD defenses.

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
