"""Bounded data ingestion, not detection or a sandbox for untrusted queries."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import ExitStack
from datetime import timedelta
import inspect
import json
from numbers import Integral, Real
import os
import re
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import numpy as np
from numpy.typing import NDArray
import pandas as pd
import sqlalchemy as sa
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError
from sqlalchemy.sql import Executable, quoted_name

from _validation import as_float_vector, positive_int


class DataConnectorError(RuntimeError):
    """A backend failed or returned a partial result; no batch is usable."""


class BatchValidationError(ValueError):
    """A batch is empty, malformed, or exceeds its caller-selected row bound."""


_SQL_MASK = re.compile(
    r"--[^\r\n]*|/\*[\s\S]*?\*/|'(?:''|[^'])*'|"
    r'"(?:""|[^"])*"|\[(?:\]\]|[^\]])*\]|`(?:``|[^`])*`'
)
_KQL_MASK = re.compile(
    r"//[^\r\n]*|/\*[\s\S]*?\*/|'(?:\\.|''|[^'\\])*'|"
    r'"(?:\\.|""|[^"\\])*"'
)
_SQL_NON_READ = re.compile(
    r"\b(?:INSERT|UPDATE|DELETE|MERGE|DROP|CREATE|ALTER|TRUNCATE|"
    r"GRANT|REVOKE|EXEC|EXECUTE|CALL|COPY|VACUUM|ATTACH|DETACH|"
    r"PRAGMA|REINDEX|ANALYZE|BEGIN|COMMIT|ROLLBACK|SAVEPOINT|"
    r"RELEASE|INTO|SET|RESET|USE|DECLARE|GO|LOAD|UNLOAD|REPLACE)\b",
    re.IGNORECASE,
)
_PARAMETER_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_KUSTO_HOST = re.compile(
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+kusto\.windows\.net\Z"
)
_EMBEDDING_FORMATS = frozenset({"json", "array", "float32_le", "float64_le"})


def _parameters(params: Mapping[str, Any] | None) -> dict[str, Any]:
    if params is None:
        return {}
    if not isinstance(params, Mapping) or any(
        not isinstance(key, str) or not _PARAMETER_NAME.fullmatch(key)
        for key in params
    ):
        raise ValueError("params must be a mapping with simple named parameter keys.")
    return dict(params)


def _select_query(query: str) -> str:
    if not isinstance(query, str) or not query.strip() or "\0" in query:
        raise ValueError("Provide a nonempty, approved SELECT query.")
    masked = _SQL_MASK.sub(" ", query).strip()
    body = masked[:-1].rstrip() if masked.endswith(";") else masked
    # This is an accident guard, not a parser or an authorization boundary.
    if (
        not re.match(r"^(?:SELECT|WITH)\b", body, re.IGNORECASE)
        or ";" in body
        or _SQL_NON_READ.search(body)
    ):
        raise ValueError("Only one approved read-only SELECT/CTE query is permitted.")
    return query


def _kql_query(query: str) -> str:
    if not isinstance(query, str) or not query.strip() or "\0" in query:
        raise ValueError("Provide a nonempty, approved, time-bounded KQL query.")
    masked = _KQL_MASK.sub(" ", query)
    statements = [part.strip() for part in masked.split(";") if part.strip()]
    if not statements or any(
        part.startswith(".")
        or re.match(
            r"^(?:set|insert|update|delete|drop|create|alter|execute|exec)\b",
            part,
            re.IGNORECASE,
        )
        for part in statements
    ):
        raise ValueError("Kusto management commands and set statements are not permitted.")
    if re.search(r"\b(?:evaluate|externaldata)\b", masked, re.IGNORECASE):
        raise ValueError("Kusto plugins and external data are not permitted.")
    return query


def _connection_url(env_var: str) -> sa.engine.URL:
    if not isinstance(env_var, str) or not _PARAMETER_NAME.fullmatch(env_var):
        raise ValueError("env_var must name a connection-string environment variable.")
    value = os.environ.get(env_var)
    if not value or not value.strip():
        raise ValueError("The SQL connection-string environment variable is not set.")
    try:
        url = make_url(value)
    except (ArgumentError, ValueError):
        raise ValueError("The SQL environment variable must contain a SQLAlchemy URL.") from None
    if url.get_backend_name() not in {"sqlite", "postgresql", "mssql"}:
        raise ValueError("Supported SQL backends are SQLite, PostgreSQL, and SQL Server.")
    return url


def _validate_batch(
    columns: list[str], rows: list[tuple[Any, ...]], max_rows: int
) -> None:
    if (
        not columns
        or any(not isinstance(name, str) or not name.strip() for name in columns)
        or len(set(columns)) != len(columns)
    ):
        raise BatchValidationError("The batch must have nonempty, unique column names.")
    if len(rows) > max_rows:
        raise BatchValidationError("The batch exceeds max_rows; no truncated batch is returned.")
    if not rows:
        raise BatchValidationError("The query returned an empty batch.")
    for position, row in enumerate(rows, 1):
        if len(row) != len(columns):
            raise BatchValidationError(f"Row {position}: row width does not match the schema.")


def _fetch_sql(
    statement: Executable,
    *,
    env_var: str,
    params: Mapping[str, Any] | None,
    max_rows: int,
) -> tuple[list[str], list[tuple[Any, ...]]]:
    url = _connection_url(env_var)
    try:
        engine = sa.create_engine(url, echo=False, hide_parameters=True)
    except ImportError:
        raise ImportError("Install the DBAPI driver for the selected SQLAlchemy dialect.") from None
    except (SQLAlchemyError, ValueError):
        raise DataConnectorError("The SQL engine could not be initialized.") from None
    try:
        with ExitStack() as resources:
            resources.callback(engine.dispose)
            connection = resources.enter_context(engine.connect())
            if engine.dialect.name == "postgresql":
                with connection.exec_driver_sql("SET TRANSACTION READ ONLY"):
                    pass
            elif engine.dialect.name == "sqlite":
                with connection.exec_driver_sql("PRAGMA query_only = ON"):
                    pass
            connection = connection.execution_options(
                stream_results=True, max_row_buffer=max_rows + 1
            )
            # Connection exit rolls back; these loaders never commit a transaction.
            with connection.execute(statement, params or {}) as result:
                if not result.returns_rows:
                    raise BatchValidationError("The query did not return a tabular batch.")
                columns = list(result.keys())
                rows = [tuple(row) for row in result.fetchmany(max_rows + 1)]
            _validate_batch(columns, rows, max_rows)
            return columns, rows
    except SQLAlchemyError:
        raise DataConnectorError("SQL read failed; no batch was returned.") from None


def load_dataframe_from_sql(
    query: str,
    *,
    env_var: str = "AZURE_SQL_CONNECTION_STRING",
    params: Mapping[str, Any] | None = None,
    max_rows: int = 10_000,
) -> pd.DataFrame:
    """Run one trusted SELECT/CTE with bound values and reject empty/oversized batches."""
    limit = positive_int(max_rows, "max_rows")
    statement = sa.text(_select_query(query))
    columns, rows = _fetch_sql(
        statement, env_var=env_var, params=_parameters(params), max_rows=limit
    )
    return pd.DataFrame.from_records(rows, columns=columns)


def _identifier(value: str, name: str) -> quoted_name:
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise ValueError(f"{name} must be a nonempty SQL identifier.")
    return quoted_name(value, quote=True)


def _reject_json_constant(_: str) -> None:
    raise ValueError("Nonfinite JSON number.")


def _embedding(
    value: Any, embedding_format: str, dimension: int | None, position: int
) -> NDArray[np.float64]:
    try:
        if embedding_format == "json":
            if not isinstance(value, str):
                raise ValueError("Expected a JSON array string.")
            decoded = json.loads(value, parse_constant=_reject_json_constant)
            if not isinstance(decoded, list):
                raise ValueError("Expected an array.")
        elif embedding_format == "array":
            if not isinstance(value, (list, tuple, np.ndarray)) or np.ma.isMaskedArray(value):
                raise ValueError("Expected a decoded array.")
            decoded = value
        else:
            if not isinstance(value, (bytes, bytearray, memoryview)):
                raise ValueError("Expected an explicit binary representation.")
            dtype = "<f4" if embedding_format == "float32_le" else "<f8"
            decoded = np.frombuffer(value, dtype=dtype)
        # Check before NumPy can coerce mixed booleans/numbers to an integer array.
        if isinstance(decoded, (list, tuple)) and any(
            isinstance(item, (bool, np.bool_)) or not isinstance(item, Real)
            for item in decoded
        ):
            raise ValueError("Expected real numeric elements.")
        vector = as_float_vector(decoded, "embedding", dimension=dimension)
        if not np.any(vector):
            raise ValueError("Zero-length vector.")
        return vector
    except (ValueError, TypeError, OverflowError, BufferError, RecursionError):
        raise BatchValidationError(
            f"Row {position}: embedding must match embedding_format and dimension, "
            "with finite real numbers and nonzero norm."
        ) from None


def load_embeddings_from_sql_table(
    table: str,
    *,
    embedding_column: str = "embedding",
    category_column: str = "category",
    id_column: str = "id",
    schema: str | None = None,
    env_var: str = "POSTGRES_CONNECTION_STRING",
    embedding_format: str = "json",
    expected_dimension: int | None = None,
    max_rows: int = 10_000,
) -> tuple[list[Any], list[str], NDArray[np.float64]]:
    """Return aligned original IDs, category strings, and an (n, d) float64 matrix."""
    limit = positive_int(max_rows, "max_rows")
    if not isinstance(embedding_format, str) or embedding_format not in _EMBEDDING_FORMATS:
        raise ValueError("embedding_format must be json, array, float32_le, or float64_le.")
    dimension = (
        positive_int(expected_dimension, "expected_dimension")
        if expected_dimension is not None
        else None
    )
    table_name = _identifier(table, "table")
    if "." in table_name:
        raise ValueError("Pass schema separately, not as a dotted table name.")
    column_names = [
        _identifier(id_column, "id_column"),
        _identifier(category_column, "category_column"),
        _identifier(embedding_column, "embedding_column"),
    ]
    if len(set(column_names)) != 3:
        raise ValueError("ID, category, and embedding columns must be distinct.")
    source = sa.table(
        table_name,
        *(sa.column(name) for name in column_names),
        schema=_identifier(schema, "schema") if schema is not None else None,
    )
    statement = sa.select(*source.c).limit(limit + 1)
    _, rows = _fetch_sql(statement, env_var=env_var, params=None, max_rows=limit)
    ids: list[Any] = []
    categories: list[str] = []
    vectors: list[NDArray[np.float64]] = []
    seen: set[Any] = set()
    for position, (row_id, category, value) in enumerate(rows, 1):
        valid_id = (
            isinstance(row_id, str) and bool(row_id.strip())
            or isinstance(row_id, Integral) and not isinstance(row_id, (bool, np.bool_))
            or isinstance(row_id, UUID)
        )
        if not valid_id:
            raise BatchValidationError(
                f"Row {position}: ID must be a nonblank string, integer, or UUID."
            )
        if row_id in seen:
            raise BatchValidationError(f"Row {position}: duplicate ID.")
        if not isinstance(category, str) or not category.strip():
            raise BatchValidationError(f"Row {position}: category must be a nonblank string.")
        vector = _embedding(value, embedding_format, dimension, position)
        if dimension is None:
            dimension = vector.size
        ids.append(row_id)
        categories.append(category)
        vectors.append(vector)
        seen.add(row_id)
    return ids, categories, np.stack(vectors)


def _kusto_configuration() -> tuple[str, str, str, str | None]:
    cluster = os.environ.get("AZURE_KUSTO_CLUSTER", "")
    database = os.environ.get("AZURE_KUSTO_DATABASE", "")
    auth_mode = os.environ.get("AZURE_AUTH_MODE", "managed_identity")
    try:
        parsed = urlsplit(cluster)
        valid_cluster = (
            parsed.scheme == "https"
            and parsed.hostname is not None
            and len(parsed.hostname) <= 253
            and _KUSTO_HOST.fullmatch(parsed.hostname) is not None
            and parsed.port in (None, 443)
            and parsed.username is None
            and parsed.password is None
            and parsed.path in ("", "/")
            and not parsed.query
            and not parsed.fragment
            and "?" not in cluster
            and "#" not in cluster
            and not any(char.isspace() for char in cluster)
        )
    except ValueError:
        valid_cluster = False
    if not valid_cluster:
        raise ValueError(
            "AZURE_KUSTO_CLUSTER must be an HTTPS public Azure Kusto endpoint "
            "under .kusto.windows.net without credentials, query, or custom path."
        )
    if not database.strip() or any(ord(char) < 32 for char in database):
        raise ValueError("AZURE_KUSTO_DATABASE must specify a database.")
    if auth_mode not in {"managed_identity", "default"}:
        raise ValueError("AZURE_AUTH_MODE must be managed_identity or default.")
    client_id = os.environ.get("AZURE_CLIENT_ID") or None
    return cluster.rstrip("/"), database, auth_mode, client_id


class _BorrowedCredential:
    """Keep the Kusto SDK from closing a credential whose lifetime we manage."""

    def __init__(self, credential: Any) -> None:
        self._credential = credential

    def get_token(self, *scopes: str, **kwargs: Any) -> Any:
        return self._credential.get_token(*scopes, **kwargs)

    def close(self) -> None:
        pass


def _kusto_rows(
    response: Any, max_rows: int
) -> tuple[list[str], list[tuple[Any, ...]]]:
    errors = getattr(response, "errors_count", None)
    if not isinstance(errors, Integral) or isinstance(errors, bool) or errors < 0:
        raise BatchValidationError("Kusto response is missing valid completion status.")
    if errors:
        raise DataConnectorError("Kusto returned partial query errors; the batch was rejected.")
    tables = getattr(response, "primary_results", None)
    if not isinstance(tables, (list, tuple)) or len(tables) != 1:
        raise BatchValidationError("Kusto must return exactly one primary result table.")
    table = tables[0]
    schema = getattr(table, "columns", None)
    if not isinstance(schema, (list, tuple)):
        raise BatchValidationError("Kusto returned an invalid result schema.")
    columns = [getattr(column, "column_name", None) for column in schema]
    rows = []
    try:
        iterator = iter(table)
    except TypeError:
        raise BatchValidationError("Kusto returned an invalid result table.") from None
    for position in range(1, max_rows + 2):
        try:
            row = next(iterator)
            if isinstance(row, (str, bytes, Mapping)):
                raise TypeError("Invalid row type.")
            rows.append(tuple(row))
        except StopIteration:
            break
        except (ValueError, TypeError, KeyError, IndexError, OverflowError):
            raise BatchValidationError(f"Row {position}: malformed Kusto result row.") from None
    _validate_batch(columns, rows, max_rows)
    return columns, rows


def load_dataframe_from_kusto(
    query: str,
    *,
    params: Mapping[str, str] | None = None,
    max_rows: int = 10_000,
    timeout_seconds: int = 60,
    client: Any = None,
    credential: Any = None,
) -> pd.DataFrame:
    """Load one approved, time-bounded ADX query; injected resources remain caller-owned."""
    query = _kql_query(query)
    limit = positive_int(max_rows, "max_rows")
    timeout = positive_int(timeout_seconds, "timeout_seconds")
    if timeout > 3600:
        raise ValueError("timeout_seconds must not exceed the Kusto one-hour limit.")
    parameters = _parameters(params)
    if any(not isinstance(value, str) for value in parameters.values()):
        raise ValueError("Kusto parameter values must be KQL scalar literal strings.")
    cluster, database, auth_mode, client_id = _kusto_configuration()
    if client is not None and credential is not None:
        raise ValueError("Inject either a client or a credential, not both.")
    if client is not None and (
        not callable(getattr(client, "execute_query", None))
        or inspect.iscoroutinefunction(client.execute_query)
    ):
        raise ValueError("An injected client must provide synchronous execute_query.")
    if credential is not None and (
        not callable(getattr(credential, "get_token", None))
        or inspect.iscoroutinefunction(credential.get_token)
    ):
        raise ValueError("An injected credential must provide synchronous get_token.")
    try:
        from azure.core.exceptions import AzureError
        from azure.kusto.data import (
            ClientRequestProperties,
            KustoClient,
            KustoConnectionStringBuilder,
        )
        from azure.kusto.data.exceptions import KustoError
    except ImportError:
        raise ImportError("ADX loading requires azure-kusto-data and azure-identity.") from None
    properties = ClientRequestProperties()
    for name, value in {
        "servertimeout": timedelta(seconds=timeout),
        "norequesttimeout": False,
        "request_readonly": True,
        "request_readonly_hardline": True,
        "request_sandboxed_execution_disabled": True,
        "request_callout_disabled": True,
        "request_external_data_disabled": True,
        "request_remote_entities_disabled": True,
        "request_impersonation_disabled": True,
        "deferpartialqueryfailures": False,
        "best_effort": False,
        "notruncation": False,
        "query_take_max_records": limit + 1,
        "truncationmaxrecords": limit + 1,
        "query_log_query_parameters": False,
        "results_progressive_enabled": False,
    }.items():
        properties.set_option(name, value)
    for name, value in parameters.items():
        properties.set_parameter(name, value)
    try:
        with ExitStack() as resources:
            if client is None:
                if credential is None:
                    try:
                        from azure.identity import DefaultAzureCredential, ManagedIdentityCredential
                    except ImportError:
                        raise ImportError("ADX authentication requires azure-identity.") from None
                    if auth_mode == "managed_identity":
                        credential = ManagedIdentityCredential(client_id=client_id)
                    else:
                        credential = DefaultAzureCredential(
                            managed_identity_client_id=client_id,
                            exclude_interactive_browser_credential=True,
                            exclude_broker_credential=True,
                        )
                    resources.callback(credential.close)
                builder = KustoConnectionStringBuilder.with_azure_token_credential(
                    cluster, credential=_BorrowedCredential(credential)
                )
                client = KustoClient(builder)
                resources.callback(client.close)
            response = client.execute_query(database, query, properties=properties)
            columns, rows = _kusto_rows(response, limit)
            return pd.DataFrame.from_records(rows, columns=columns)
    except (KustoError, AzureError):
        raise DataConnectorError("Kusto read or authentication failed; no batch was returned.") from None
