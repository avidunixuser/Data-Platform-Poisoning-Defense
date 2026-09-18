# Data connectors

`scripts\data_connectors.py` moves bounded batches into the existing defenses.
**It adds no detectors, detection thresholds, automatic reference fitting, or
"clean" verdicts.** Choose the defense by the table's role, not its database brand.

| Data role | Loader and downstream use |
| --- | --- |
| Azure SQL/PostgreSQL feature or labeled training table | `load_dataframe_from_sql` -> existing `LineageAuditor` checks on explicitly selected feature/label columns |
| Azure SQL/PostgreSQL document or free-text columns | `load_dataframe_from_sql` -> both `ContentSanitizer` and `SemanticInjectionClassifier`; database origin does not make text trustworthy |
| Azure SQL/PostgreSQL embedding table | `load_embeddings_from_sql_table` -> existing `EmbeddingAnomalyDetector.fit_reference` / `.score` |
| ADX time-windowed telemetry | `load_dataframe_from_kusto` -> existing batch-drift/lineage checks against an independently trusted, comparable historical window |
| ADX labeled features or LLM-facing log text | Kusto loader -> label/spectral checks when labels exist; both content layers for free text used by an LLM |

## Setup and credentials

Use the skill's existing manifests and a local virtual environment. From the
repository root, after creating `.venv`:

```powershell
.\.venv\Scripts\python.exe -m pip install -r .github\skills\retrieval-poisoning-defense\requirements.txt
.\.venv\Scripts\python.exe -m pip install -r .github\skills\retrieval-poisoning-defense\requirements-azure.txt
$env:PYTHONPATH = (Resolve-Path .github\skills\retrieval-poisoning-defense\scripts).Path
```

The first manifest includes pandas, NumPy, SQLAlchemy 2, and detector dependencies.
The optional second manifest supplies `azure-kusto-data>=5,<7`, `azure-identity`,
`psycopg2-binary`, and `pyodbc`. SQL/offline tests do not import Azure packages.
For Azure SQL, install Microsoft's **ODBC Driver 18 for SQL Server separately at
the OS level**; installing `pyodbc` does not install that driver.

| Environment variable | Contract |
| --- | --- |
| `AZURE_SQL_CONNECTION_STRING` | Default for the DataFrame SQL loader; a SQLAlchemy URL, normally using `mssql+pyodbc` |
| `POSTGRES_CONNECTION_STRING` | Default for the embedding loader; a SQLAlchemy URL, normally using `postgresql+psycopg2` |
| Custom `env_var` argument | Names another SQLAlchemy-URL environment variable; useful for file-based SQLite tests or another approved database |
| `AZURE_KUSTO_CLUSTER` | Required HTTPS **public Azure** Kusto endpoint under `.kusto.windows.net`; no credentials, custom path, query, fragment, or non-443 port |
| `AZURE_KUSTO_DATABASE` | Required ADX database name |
| `AZURE_AUTH_MODE` | Absent or `managed_identity`: `ManagedIdentityCredential`; explicit `default`: local-development `DefaultAzureCredential` |
| `AZURE_CLIENT_ID` | Optional user-assigned managed identity client ID |

Inject SQL connection URLs through deployment configuration or a secret manager.
Do not put credentials in source, examples, prompts, chat, logs, or committed
environment files. Raw ODBC strings are not SQLAlchemy URLs. Configure TLS and
certificate verification in the selected driver/URL; do not disable them to make
a connection succeed. The SQL loaders support SQLite, PostgreSQL, and SQL Server
dialects, not arbitrary SQLAlchemy backends or async engines.

Grant the SQL principal **only SELECT on approved tables/views**, with no DDL,
write permissions, or access to side-effecting routines. ADX identities should
have only the required database/table read access, not ingestion/admin roles.
Managed identity is the ADX production default and never silently falls back to
a developer identity. For an intentional local run, sign in with approved local
tooling and explicitly set:

```powershell
$env:AZURE_AUTH_MODE = "default"
```

`DefaultAzureCredential` can select identities from its configured credential
chain; review which identity is selected. Interactive-browser and broker
credentials are excluded by this loader. Set nonsecret cluster/database values
in your environment before calling it. Sovereign-cloud, Fabric, custom/proxy, and
localhost ADX endpoints are intentionally unsupported.

## Shared safety and batch contract

- **SQL and KQL are application-owned, reviewed query definitions, never text
  taken from retrieved documents or model output.** Bind scalar values; do not
  concatenate them into a query. Retrieved text stays data, never executable code.
- Conservative statement guards reject obvious writes/control commands and
  unsafe query modes. A regex is **not a security boundary**, a complete parser,
  or proof that a function invoked by a query is harmless. Database authorization
  and trusted query ownership remain mandatory.
- `max_rows` defaults to **10,000** and must be a positive integer, not a boolean.
  Loaders request/fetch **`max_rows + 1`** rows and raise if that sentinel row
  exists. They do not silently truncate or return a partial batch.
- Empty batches, missing/duplicate column names, inconsistent row widths, and
  malformed embeddings raise `BatchValidationError` (a `ValueError`). Invalid
  options raise `ValueError`; backend/authentication/ADX partial-query failures
  raise `DataConnectorError` (a `RuntimeError`). Missing optional packages raise
  an actionable `ImportError`. **A failed load is not a clean audit.**
- Narrow SQLAlchemy/Azure/Kusto SDK exceptions are sanitized without including
  SQL text, parameter values, rows, credentials, or chained SDK messages.
  The module does not log batches. Do not enable verbose driver/SDK logging for
  sensitive workloads. Unrelated programming errors are not swallowed.
- General DataFrames preserve data rather than enforcing feature-specific
  validity: nullable text or telemetry fields remain nullable. Downstream
  defenses must validate their selected numeric/text inputs.
- SQL results/connections are context-managed, transactions are never committed,
  and engines are disposed on success and failure. PostgreSQL uses `SET
  TRANSACTION READ ONLY`; SQLite uses connection-local `PRAGMA query_only = ON`.
  SQL Server has no equivalent transaction flag here: its read-only identity is
  essential. SQL streaming is a driver-dependent hint, not a guarantee against
  driver-side buffering or large individual cells.
- On overflow, partition an approved query into explicit disjoint windows or
  key ranges and audit every partition. Do not drop rows or relabel the failure
  as safe. These helpers do not implement automatic pagination or retries.

## SQL DataFrames

```text
load_dataframe_from_sql(
    query, *,
    env_var="AZURE_SQL_CONNECTION_STRING",
    params=None,
    max_rows=10_000,
)  # -> pandas.DataFrame
```

```python
from data_connectors import load_dataframe_from_sql

df = load_dataframe_from_sql(
    "SELECT * FROM customer_features",
    env_var="AZURE_SQL_CONNECTION_STRING",
)
```

`params` is a mapping of simple named keys to DBAPI-supported scalar values.
For example, an application-approved query can use
`WHERE customer_id = :customer_id` with `params={"customer_id": customer_id}`.
The mapping is passed separately to SQLAlchemy 2's `Connection.execute`.
The same loader works with PostgreSQL by selecting
`env_var="POSTGRES_CONNECTION_STRING"`.

Only one SELECT or read-only CTE is accepted. A terminal semicolon and ordinary
comments are supported. Guards are deliberately conservative; some otherwise
valid dialect-specific syntax may be refused. The caller owns query identifiers
in raw SQL; parameters cannot substitute for table/column names.

## Embedding tables

```text
load_embeddings_from_sql_table(
    table, *,
    embedding_column="embedding",
    category_column="category",
    id_column="id",
    schema=None,
    env_var="POSTGRES_CONNECTION_STRING",
    embedding_format="json",
    expected_dimension=None,
    max_rows=10_000,
)  # -> (list[original ID], list[str], numpy.ndarray[float64])
```

```python
from data_connectors import load_embeddings_from_sql_table

ids, categories, embeddings = load_embeddings_from_sql_table(
    table="vector_store",
    embedding_column="embedding",
    category_column="category",
    schema="public",
    env_var="POSTGRES_CONNECTION_STRING",
)
```

Use `schema="dbo"` with an Azure SQL schema and
`env_var="AZURE_SQL_CONNECTION_STRING"`; use `schema="main"` for SQLite if needed.
Do not pass `public.vector_store` as `table`. Table, schema, and column identifiers
are separate, explicitly quoted SQLAlchemy Core objects, not interpolated SQL.
The three columns must be distinct. An expression such as `CAST(...)` is not a
column identifier.

The return matrix always has shape **`(n, d)`**, including a single row, and
`float64` dtype. Values are not normalized or rescaled. IDs are preserved as
nonblank strings, integers, or UUID objects; booleans, floating IDs, nulls, and
duplicate IDs (including across categories) are rejected. Categories must be
nonblank strings and are not trimmed or stringified. Row order and alignment are
preserved exactly as returned by the database; SQL table scans do **not** promise
a repeatable ordering between calls.

| `embedding_format` | Accepted transport, with no autodetection |
| --- | --- |
| `json` (default) | A JSON array **string**, including pgvector text such as `[0.1,0.2]`; a decoded list or byte string is not accepted |
| `array` | Driver-decoded list, tuple, or one-dimensional real numeric NumPy array; masked/object/string/boolean arrays are rejected |
| `float32_le` | Bytes, bytearray, or contiguous memoryview containing only little-endian IEEE float32 elements |
| `float64_le` | Bytes, bytearray, or contiguous memoryview containing only little-endian IEEE float64 elements |

Every vector must be nonempty, one-dimensional, finite, numeric, and nonzero.
Booleans (including mixed numeric/boolean lists), string elements, NaN/Inf,
ragged/nested arrays, trailing JSON garbage, and misaligned binary buffers fail.
All rows must have the same dimension; pass `expected_dimension` to pin the
embedding model's dimension rather than infer it from row one. Error messages
identify the **one-based row position**, not sensitive IDs or vector contents.

**Native SQL `VECTOR` / pgvector caveat:** representation depends on engine,
version, DBAPI driver, and registered adapters. pgvector can arrive as text or a
registered decoded array; native SQL vectors can use JSON-compatible text or
driver-specific binary/native transport. The binary formats above are **not**
decoders for pgvector's wire protocol or SQL Server's native vector payload.
Never strip unknown headers or guess an encoding. Use an approved query-side
cast to text or an existing DBA-managed read-only projection/view, or register
the appropriate driver adapter outside this loader. For example, pgvector can
be projected with `CAST(embedding AS text)`; supported SQL vector versions can
project `CAST(embedding AS varchar(max))`. The table helper can read the resulting
view. Match `embedding_format` to the **actual returned representation**, and
verify model/version/dimension/preprocessing agreement with the reference.
This module does not install adapters, create views, or change database schema.

### Existing-detector handoff

Use an independently approved reference table/window, **not the candidate batch**:

```python
import numpy as np
from embedding_anomaly_detector import EmbeddingAnomalyDetector
from data_connectors import load_embeddings_from_sql_table

_, reference_categories, reference_vectors = load_embeddings_from_sql_table(
    table="trusted_vector_reference", schema="public"
)
category_array = np.asarray(reference_categories, dtype=object)
reference_by_category = {
    category: reference_vectors[category_array == category]
    for category in dict.fromkeys(reference_categories)
}
detector = EmbeddingAnomalyDetector().fit_reference(reference_by_category)

ids, categories, embeddings = load_embeddings_from_sql_table(
    table="vector_store",
    schema="public",
    expected_dimension=reference_vectors.shape[1],
)
scores = [
    (record_id, detector.score(vector, category=category))
    for record_id, category, vector in zip(ids, categories, embeddings)
]
```

Reference categories must meet the detector's existing sample requirements.
Use its existing calibration APIs/policy, not database-specific thresholds.
Retain IDs with the scores for review and lineage; do not print raw records.

## ADX telemetry windows

```text
load_dataframe_from_kusto(
    query, *,
    params=None,
    max_rows=10_000,
    timeout_seconds=60,
    client=None,
    credential=None,
)  # -> pandas.DataFrame
```

```python
from data_connectors import load_dataframe_from_kusto

batch = load_dataframe_from_kusto("MyTable | where Timestamp > ago(1h)")
```

**The caller must provide an approved, time-bounded KQL query.** The helper
does not invent a timestamp column, add a hidden time filter, or prove a query's
temporal semantics. For drift auditing, use a trusted reference covering normal
periodic variation: comparable UTC hour/day-of-week, seasonality, category/service
mix, deployed version, aggregation granularity, and preprocessing. For example,
the preceding hour might be compared with the same hour one week earlier, but
one week is not automatically a sufficient or uncontaminated baseline. Freeze
absolute window endpoints when reproducibility matters. Ordinary workload
changes require investigation, not an automatic poisoning conclusion.

The implementation uses **`KustoClient.execute_query(database, query,
properties=...)` only**, never the dispatching `execute`, a management API, or
an ingestion client. Kusto `set` statements and management commands are refused.
`ClientRequestProperties` carries:

- `servertimeout` as a `timedelta` (`timeout_seconds` is an integer from 1-3,600);
  the SDK adds its own network grace period, so this is not a strict wall-clock
  bound on authentication plus the whole function.
- `request_readonly` / `request_readonly_hardline`; sandboxed code, callouts,
  external data, remote entities, and downstream impersonation disabled.
- `query_take_max_records` and `truncationmaxrecords`, both `max_rows + 1`.
  `notruncation`, `best_effort`, deferred partial errors, progressive results,
  and query-parameter logging are disabled.

The client also checks the returned row count and rejects SDK-reported partial
errors, including truncation/other completion errors. Exactly **one** primary
result table is required; fork/multi-table results are not silently flattened.
The SDK's normal size limits can still reject a wide batch below the row limit.
`params` uses `ClientRequestProperties.set_parameter`, separate from query text:
keys are simple names and values are strings in the SDK's parameter format for
the declared KQL scalar type.

The default loader owns and closes both its client and credential, including
failure paths. For tests or host-managed lifetimes, supply a trusted synchronous
`client` exposing `execute_query`, **or** a synchronous Azure token `credential`
exposing `get_token`, never both. Injected objects remain caller-owned and are
not closed. A borrowed-credential adapter prevents the SDK from closing a
caller-owned credential. Cluster/database/auth environment validation still
applies; an injected client must itself target the configured approved endpoint.

## Offline verification and limitations

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s .github\skills\retrieval-poisoning-defense\tests -p test_data_connectors.py -v
```

Tests use `tempfile` **files in the project directory**, closed file handles, and
file-based SQLite (not isolated `:memory:` engines). They remove their databases
after each test. Coverage includes bound values, quoted identifiers, real SQLite
read-only enforcement, strict formats/metadata, empty/overflow behavior, resource
cleanup, and a SQLite-loader -> `EmbeddingAnomalyDetector` integration. ADX SDK and
identity modules are mocked; tests make **no service calls** and require no cloud
credentials.

**Live Azure SQL, PostgreSQL, and ADX behavior has not been tested here.**
Validate your selected driver/SDK/engine versions, identity privileges, TLS,
native-vector serialization, result limits, and request-property support in your
own authorized environment before production use.

### Official API references

- [SQLAlchemy 2 connections and transactions](https://docs.sqlalchemy.org/en/20/core/connections.html)
- [ADX Python query library](https://learn.microsoft.com/en-us/azure/data-explorer/python-query-data)
- [Kusto request properties](https://learn.microsoft.com/en-us/kusto/api/rest/request-properties?view=azure-data-explorer)
- [Published Python SDK `execute_query` and client cleanup](https://github.com/Azure/azure-kusto-python/blob/v5.0.5/azure-kusto-data/azure/kusto/data/client.py)
- [Published Python SDK `with_azure_token_credential`](https://github.com/Azure/azure-kusto-python/blob/v5.0.5/azure-kusto-data/azure/kusto/data/kcsb.py)
- [ManagedIdentityCredential](https://learn.microsoft.com/en-us/python/api/azure-identity/azure.identity.managedidentitycredential)
- [DefaultAzureCredential](https://learn.microsoft.com/en-us/python/api/azure-identity/azure.identity.defaultazurecredential)
- [SQL native vector representation and driver compatibility](https://learn.microsoft.com/en-us/sql/t-sql/data-types/vector-data-type?view=sql-server-ver17)
- [pgvector Python driver adapters](https://github.com/pgvector/pgvector-python)
