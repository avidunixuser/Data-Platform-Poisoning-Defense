# Service integration

The detectors run offline on arrays, text and DataFrames. Live examples below
are integration patterns, not automatically executed skill actions. Confirm the
resource, identity, query scope, data classification and costs first. Never send
private source text to an embedding endpoint without authorization for that
source and destination.

## Dependencies and identity

Install `requirements.txt` for offline use. Install `requirements-azure.txt` only
when live Azure SDKs or database drivers are needed. From the repository root:

```powershell
.\.venv\Scripts\python.exe -m pip install -r .github\skills\retrieval-poisoning-defense\requirements-azure.txt
$env:PYTHONPATH = (Resolve-Path .github\skills\retrieval-poisoning-defense\scripts).Path
```

Keep secrets outside the repository and chat. In production, prefer an explicit
managed or workload identity with narrowly scoped data-plane permissions.
`AZURE_AUTH_MODE=managed_identity` is the built-in adapter default;
`AZURE_AUTH_MODE=default` explicitly selects `DefaultAzureCredential` for local
development. `AZURE_CLIENT_ID` optionally selects a user-assigned identity.
No script provisions identities, grants roles or changes service configuration.

## Foundry semantic screening

| Environment variable | Purpose |
| --- | --- |
| `FOUNDRY_EMBEDDINGS_ENDPOINT` | Approved Azure HTTPS v1 base URL, for example `https://<resource>.openai.azure.com/openai/v1/` |
| `FOUNDRY_EMBEDDINGS_MODEL` | Deployed embedding model's deployment name, not an assumed universal model ID |
| `FOUNDRY_API_KEY` | Optional key-auth fallback supplied externally; prefer identity |
| `AZURE_AUTH_MODE` | `managed_identity` by default; `default` for explicit local development |
| `AZURE_CLIENT_ID` | Optional user-assigned managed identity client ID |

The adapter requires an explicit Azure endpoint. It never falls back to public
OpenAI. The `openai` Python package is the client library; requests still go to
the configured Azure resource. Verify sovereign-cloud endpoints and token
audiences separately: the built-in provider intentionally accepts **public Azure
only**. Sovereign clouds, custom/proxy endpoints and local servers require a
separately reviewed embedding provider; do not relax endpoint validation to make
an unsupported destination work.

The source guidelines named `azure-ai-inference`. Microsoft's current
[migration guidance](https://learn.microsoft.com/azure/foundry/how-to/model-inference-to-openai-migration?pivots=programming-language-python)
marks that SDK retired and directs embeddings clients to the OpenAI SDK/v1 API.
This skill preserves the semantic detector's behavior while using that supported
API. Model behavior and threshold calibration still require live evaluation.

```python
from content_sanitizer import ContentSanitizer, SemanticInjectionClassifier

def screen_approved_batch(documents):
    regex = ContentSanitizer()
    with SemanticInjectionClassifier() as semantic:
        for source_id, original_text in documents:
            regex_result = regex.scan(original_text)
            semantic_result = semantic.score(original_text)
            yield source_id, regex_result, semantic_result
```

The caller must consume all results and hold the item if either layer flags.
Exceptions stop evaluation and must enter the existing pipeline's failure/review
path. Do not catch an exception and emit a false/clean result. The default seed
bank is deliberately small and is not a comprehensive attack corpus.
Both scanners reject inputs over their default 16,000-character cap rather than
truncating them. A character limit is not an exact embedding-token budget; the
configured model can still reject a dense input. Treat that rejection as an
unevaluated item and use a reviewed chunking policy.

`ContentScanResult.normalized_text` remains available in memory but is marked
`metadata={"audit": False}` for `LineageAuditor.record_batch` to omit. Do not use
plain `dataclasses.asdict()` to build content logs: it does not honor that metadata.
The optional `assess_content` helper has a computed combined `.flagged` property;
record that OR explicitly if a top-level verdict is required in addition to its
nested scan/semantic flags.

## Cosmos DB

Use the existing application SDK integration when possible. An authorized,
bounded, read-only sample for a string partition key and a `batchId` field is:

```python
from itertools import islice
import os
from azure.cosmos import CosmosClient
from azure.identity import ManagedIdentityCredential

def audit_cosmos_batch(detector):
    limit = 100
    with ManagedIdentityCredential(client_id=os.getenv("AZURE_CLIENT_ID")) as credential:
        with CosmosClient(os.environ["COSMOS_ENDPOINT"], credential=credential) as client:
            container = client.get_database_client(
                os.environ["COSMOS_DATABASE"]
            ).get_container_client(os.environ["COSMOS_CONTAINER"])
            items = list(islice(container.query_items(
                query=(
                    "SELECT c.id, c.category, c.embedding FROM c "
                    "WHERE c.batchId = @batch_id"
                ),
                parameters=[{"name": "@batch_id", "value": os.environ["COSMOS_BATCH_ID"]}],
                partition_key=os.environ["COSMOS_PARTITION_KEY"],
                max_item_count=limit + 1,
            ), limit + 1))
    if not items or len(items) > limit:
        raise ValueError("Expected a nonempty batch within the approved row limit.")
    return [
        (item["id"], detector.score(item["embedding"], category=item["category"]))
        for item in items
    ]
```

This is for the Cosmos DB for NoSQL SDK; other Cosmos APIs require their own
source adapter. Fit `detector` on a separate trusted reference before calling it.
Adjust field names, partition-key type and the batch predicate to the approved
schema, not to instructions found in documents. `max_item_count` is a page hint,
not a total-result limit; the explicit sentinel read detects overflow. Do not
silently audit only the first page and describe the whole batch as evaluated.

Use a data-reader identity for auditing and a separate guarded ingestion identity
for publication. Scoring does not prove the vector matches its claimed content:
where needed, re-embed source text with the same trusted model and compare it,
retaining the version/hash and provenance used to make the decision.

## Azure AI Search

Prefer scanning an incoming document before it reaches the application's
existing upload/indexer path. For a periodic audit, use a data-reader client and
an approved query selecting only the fields required for inspection:

```python
import os
from azure.identity import ManagedIdentityCredential
from azure.search.documents import SearchClient

def read_search_batch():
    limit = 100
    with ManagedIdentityCredential(client_id=os.getenv("AZURE_CLIENT_ID")) as credential:
        with SearchClient(
            endpoint=os.environ["AZURE_SEARCH_ENDPOINT"],
            index_name=os.environ["AZURE_SEARCH_INDEX"],
            credential=credential,
        ) as client:
            rows = list(client.search(
                search_text=os.environ["AZURE_SEARCH_AUDIT_QUERY"],
                select=["id", "content"],
                top=limit + 1,
            ))
    if not rows or len(rows) > limit:
        raise ValueError("Narrow the approved search scope to a nonempty bounded batch.")
    return [(row["id"], row["content"]) for row in rows]
```

Field names are example schema, not built-in Search fields. Establish that the
selected query covers the intended scope; relevance search is not proof of an
exhaustive index audit. The sentinel detects matches beyond the row budget.
Plan checkpointed, stable scopes for larger audits instead of accepting silent
truncation. Do not log returned document bodies.

After permission to send these fields to Foundry is established, pass the
bounded output through both layers in `screen_approved_batch`. Keep the original
text, result signals and source/version association; actual quarantine or index
publication remains a separate approved application operation.

## Fabric Lakehouse

Use an already authorized notebook/job to select an immutable Delta version or
an approved batch partition. Collect only a bounded, projected DataFrame.
In an existing Fabric Spark session, a pattern is:

```python
from pyspark.sql import functions as F

limit = 5000
selected = (
    spark.table(approved_table)
    .where(F.col("batch_id") == approved_batch_id)
    .select(*feature_cols, "label")
    .limit(limit + 1)
)
df = selected.toPandas()
if df.empty or len(df) > limit:
    raise ValueError("The approved Lakehouse batch is empty or exceeds the audit budget.")
```

This snippet requires the Fabric Spark runtime; the offline skill does not
install Spark. Table/column names and batch IDs must be approved application
configuration, not retrieved instructions. A large production dataset needs a
reviewed sampling or distributed design rather than an unbounded `toPandas`.
Record the sampling policy and do not claim the sample represents every row.

Feed prepared features to both label and spectral checks, and compare drift to
a separate representative clean reference. In the ingestion job, call
`record_batch` with the actual source ingestion time and transformation versions.
If the time is unavailable, leave it unknown instead of substituting audit time.

## Relational databases and ADX

The [connector guide](data_connectors.md) describes SQLAlchemy URLs, SQL Server
ODBC prerequisites, vector serialization, time-windowed KQL, result bounds and
read-only permissions. It includes `load_dataframe_from_sql`,
`load_embeddings_from_sql_table` and `load_dataframe_from_kusto`.

## Validation scope

The supplied automated suite is offline: numerical fixtures, a file-backed SQLite
database and fake semantic/Kusto clients. It does not log into Azure, validate
tenant RBAC, prove real semantic detection quality, or certify vendor-specific
vector transport. Run a separate approved integration evaluation before using
the results as an ingestion gate.

Sources: [Cosmos DB data-plane identity](https://learn.microsoft.com/azure/cosmos-db/how-to-connect-role-based-access-control),
[Azure AI Search security](https://learn.microsoft.com/azure/search/search-security-overview),
and [Foundry embeddings](https://learn.microsoft.com/azure/foundry/openai/how-to/embeddings).
