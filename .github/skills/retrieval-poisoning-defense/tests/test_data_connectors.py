import _test_support

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import traceback
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch
from uuid import UUID

import numpy as np
import pandas as pd
import sqlalchemy as sa
from sqlalchemy.exc import OperationalError

import data_connectors as connectors
from data_connectors import (
    BatchValidationError,
    DataConnectorError,
    load_dataframe_from_kusto,
    load_dataframe_from_sql,
    load_embeddings_from_sql_table,
)


class SQLConnectorTests(unittest.TestCase):
    def setUp(self):
        descriptor, filename = tempfile.mkstemp(
            prefix=".connector-test-", suffix=".sqlite", dir=Path.cwd()
        )
        os.close(descriptor)
        self.database_file = Path(filename)
        self.addCleanup(self.database_file.unlink, missing_ok=True)
        url = sa.URL.create("sqlite", database=str(self.database_file))
        self.engine = sa.create_engine(url)
        self.addCleanup(self.engine.dispose)
        env = patch.dict(
            os.environ,
            {
                "CONNECTOR_TEST_SQL": url.render_as_string(hide_password=False),
                "AZURE_SQL_CONNECTION_STRING": url.render_as_string(hide_password=False),
                "POSTGRES_CONNECTION_STRING": url.render_as_string(hide_password=False),
            },
        )
        env.start()
        self.addCleanup(env.stop)
        metadata = sa.MetaData()
        self.features = sa.Table(
            "customer_features",
            metadata,
            sa.Column("id", sa.Integer),
            sa.Column("segment", sa.String),
            sa.Column("notes", sa.String),
        )
        self.vectors = sa.Table(
            "vector_store",
            metadata,
            sa.Column("id", sa.String),
            sa.Column("category", sa.String),
            sa.Column("embedding", sa.String),
        )
        metadata.create_all(self.engine)
        with self.engine.begin() as connection:
            connection.execute(
                self.features.insert(),
                [
                    {"id": 1, "segment": "north", "notes": "source text"},
                    {"id": 2, "segment": "south", "notes": None},
                    {"id": 3, "segment": "north", "notes": "more source text"},
                ],
            )
        self.replace_vectors(
            [
                {"id": "002", "category": "support", "embedding": "[2, 0.5]"},
                {"id": "001", "category": "billing", "embedding": "[0.5, 3]"},
            ]
        )

    def replace_vectors(self, rows):
        with self.engine.begin() as connection:
            connection.execute(self.vectors.delete())
            if rows:
                connection.execute(self.vectors.insert(), rows)

    def test_dataframe_default_and_bound_parameters(self):
        frame = load_dataframe_from_sql(
            "SELECT id, notes FROM customer_features WHERE segment = :segment ORDER BY id",
            params={"segment": "north"},
            max_rows=2,
        )
        self.assertIsInstance(frame, pd.DataFrame)
        self.assertEqual(frame.columns.tolist(), ["id", "notes"])
        self.assertEqual(frame["id"].tolist(), [1, 3])
        self.assertEqual(frame["notes"].tolist(), ["source text", "more source text"])

    def test_sql_parameters_cannot_add_a_statement(self):
        value = "'; DROP TABLE customer_features; --"
        with self.engine.begin() as connection:
            connection.execute(
                self.features.insert(), {"id": 4, "segment": value, "notes": "literal"}
            )
        frame = load_dataframe_from_sql(
            "SELECT id FROM customer_features WHERE segment = :segment",
            params={"segment": value},
            env_var="CONNECTOR_TEST_SQL",
        )
        self.assertEqual(frame["id"].tolist(), [4])
        self.assertEqual(len(load_dataframe_from_sql("SELECT * FROM customer_features")), 4)

    def test_cte_comments_literal_keywords_and_terminal_semicolon(self):
        frame = load_dataframe_from_sql(
            "/* approved */ WITH chosen AS (SELECT id FROM customer_features) "
            "SELECT id, 'DROP; UPDATE' AS \"select\" FROM chosen ORDER BY id; -- tail"
        )
        self.assertEqual(frame["select"].tolist(), ["DROP; UPDATE"] * 3)

    def test_sql_writes_control_commands_and_multiple_statements_are_refused(self):
        queries = [
            "DELETE FROM customer_features",
            "/* read first */ UPDATE customer_features SET id = 0",
            "SELECT * FROM customer_features; DROP TABLE customer_features",
            "WITH changed AS (DELETE FROM customer_features RETURNING *) SELECT * FROM changed",
            "SELECT * INTO stolen FROM customer_features",
            "EXEC some_procedure",
            "PRAGMA query_only = OFF",
            "SET TRANSACTION READ WRITE",
            "BEGIN; SELECT 1",
            "SELECT 1; COMMIT",
            "",
            "-- only a comment",
            None,
        ]
        with patch.object(connectors.sa, "create_engine") as create:
            for query in queries:
                with self.subTest(query=query), self.assertRaises(ValueError):
                    load_dataframe_from_sql(query)
            create.assert_not_called()
        self.assertEqual(len(load_dataframe_from_sql("SELECT * FROM customer_features")), 3)

    def test_empty_and_oversized_dataframe_fail(self):
        with self.assertRaisesRegex(BatchValidationError, "empty batch"):
            load_dataframe_from_sql("SELECT * FROM customer_features WHERE id = 99")
        with self.assertRaisesRegex(BatchValidationError, "exceeds max_rows"):
            load_dataframe_from_sql("SELECT * FROM customer_features", max_rows=2)
        self.assertEqual(
            len(load_dataframe_from_sql("SELECT * FROM customer_features", max_rows=3)), 3
        )

    def test_duplicate_dataframe_columns_fail(self):
        with self.assertRaisesRegex(BatchValidationError, "unique column"):
            load_dataframe_from_sql("SELECT id, id FROM customer_features")

    def test_invalid_limits_and_parameter_containers_fail_before_connecting(self):
        with patch.object(connectors.sa, "create_engine") as create:
            for value in [0, -1, True, 2.5, "2"]:
                with self.subTest(max_rows=value), self.assertRaises(ValueError):
                    load_dataframe_from_sql("SELECT 1", max_rows=value)
            for params in [[], [("id", 1)], {"id; DROP": 1}, {1: 1}]:
                with self.subTest(params=params), self.assertRaises(ValueError):
                    load_dataframe_from_sql("SELECT 1", params=params)
            create.assert_not_called()

    def test_missing_or_invalid_connection_configuration_is_sanitized(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "not set"):
                load_dataframe_from_sql("SELECT 1")
        for value in [
            "sensitive-invalid-connection",
            "postgresql://user:private-password@host:invalid-port/database",
            "mysql://user:private-password@host/database",
        ]:
            with self.subTest(value=value), patch.dict(
                os.environ, {"CONNECTOR_TEST_SQL": value}
            ):
                with self.assertRaises(ValueError) as caught:
                    load_dataframe_from_sql("SELECT 1", env_var="CONNECTOR_TEST_SQL")
                self.assertNotIn(value, str(caught.exception))
                self.assertNotIn("private-password", str(caught.exception))
        with self.assertRaises(ValueError):
            load_dataframe_from_sql("SELECT 1", env_var="password=secret")

    def test_sql_errors_do_not_echo_query_parameters_or_driver_details(self):
        sensitive = "private_value_not_for_errors"
        try:
            load_dataframe_from_sql(
                f"SELECT missing_{sensitive} FROM customer_features WHERE segment = :value",
                params={"value": sensitive},
            )
        except DataConnectorError:
            self.assertNotIn(sensitive, traceback.format_exc())
        else:
            self.fail("A missing column must fail, not return a clean batch.")

    def test_sql_works_without_azure_imports(self):
        with patch.dict(sys.modules, {"azure": None, "azure.kusto.data": None}):
            frame = load_dataframe_from_sql("SELECT * FROM customer_features")
        self.assertEqual(frame.shape, (3, 3))

    def test_sqlite_read_only_connection_blocks_a_writing_function(self):
        original_create_engine = sa.create_engine

        def create_engine(*args, **kwargs):
            engine = original_create_engine(*args, **kwargs)

            @sa.event.listens_for(engine, "connect")
            def register_writer(dbapi_connection, _):
                def try_write():
                    dbapi_connection.execute("DELETE FROM customer_features")
                    return 1

                dbapi_connection.create_function("try_write", 0, try_write)

            return engine

        with patch.object(connectors.sa, "create_engine", side_effect=create_engine):
            with self.assertRaises(DataConnectorError):
                load_dataframe_from_sql("SELECT try_write() AS value")
        self.assertEqual(len(load_dataframe_from_sql("SELECT * FROM customer_features")), 3)

    def test_default_embedding_loader_preserves_alignment_and_values(self):
        ids, categories, embeddings = load_embeddings_from_sql_table(
            "vector_store", expected_dimension=2
        )
        self.assertEqual(ids, ["002", "001"])
        self.assertEqual(categories, ["support", "billing"])
        self.assertEqual(embeddings.shape, (2, 2))
        self.assertEqual(embeddings.dtype, np.dtype(np.float64))
        np.testing.assert_array_equal(embeddings, [[2, 0.5], [0.5, 3]])

    def test_sqlite_embeddings_feed_existing_detector_without_connector_thresholds(self):
        from embedding_anomaly_detector import EmbeddingAnomalyDetector

        directions = [[1, 0.02], [0.95, 0.04], [1.05, -0.01], [0.98, -0.03], [1.02, 0]]
        trusted = []
        for group, values in [
            ("support", directions),
            ("billing", [value[::-1] for value in directions]),
        ]:
            trusted.extend(
                {
                    "id": f"{group}-{index}",
                    "category": group,
                    "embedding": json.dumps(value),
                }
                for index, value in enumerate(values)
            )
        self.replace_vectors(trusted)
        _, categories, reference = load_embeddings_from_sql_table(
            "vector_store", expected_dimension=2
        )
        category_array = np.asarray(categories, dtype=object)
        detector = EmbeddingAnomalyDetector().fit_reference(
            {
                category: reference[category_array == category]
                for category in dict.fromkeys(categories)
            }
        )
        self.replace_vectors(
            [
                {"id": "normal", "category": "support", "embedding": "[1, 0]"},
                {"id": "review", "category": "support", "embedding": "[-20, -20]"},
            ]
        )
        ids, categories, embeddings = load_embeddings_from_sql_table("vector_store")
        scores = [
            detector.score(vector, category=category)
            for vector, category in zip(embeddings, categories)
        ]
        self.assertEqual(ids, ["normal", "review"])
        self.assertFalse(scores[0].flagged)
        self.assertTrue(scores[1].flagged)
        self.assertEqual([score.category for score in scores], ["support", "support"])

    def test_schema_and_quoted_identifiers_are_not_interpolated(self):
        metadata = sa.MetaData()
        table = sa.Table(
            'vectors"; DROP TABLE customer_features; --',
            metadata,
            sa.Column("select", sa.String),
            sa.Column('category"; --', sa.String),
            sa.Column("embedding value", sa.String),
        )
        metadata.create_all(self.engine)
        with self.engine.begin() as connection:
            connection.execute(
                table.insert(),
                {"select": "  original-id  ", 'category"; --': "  group  ", "embedding value": "[1, 2]"},
            )
        ids, categories, matrix = load_embeddings_from_sql_table(
            table.name,
            schema="main",
            id_column="select",
            category_column='category"; --',
            embedding_column="embedding value",
            env_var="CONNECTOR_TEST_SQL",
        )
        self.assertEqual(ids, ["  original-id  "])
        self.assertEqual(categories, ["  group  "])
        self.assertEqual(matrix.shape, (1, 2))
        self.assertEqual(len(load_dataframe_from_sql("SELECT * FROM customer_features")), 3)

    def test_untrusted_missing_identifier_cannot_write(self):
        with self.assertRaises(DataConnectorError):
            load_embeddings_from_sql_table('missing"; DROP TABLE vector_store; --')
        self.assertEqual(len(load_embeddings_from_sql_table("vector_store")[0]), 2)
        with self.assertRaises(DataConnectorError):
            load_embeddings_from_sql_table(
                "vector_store", schema='main"; DROP TABLE vector_store; --'
            )

    def test_embedding_configuration_is_strict(self):
        configurations = [
            {"embedding_format": "auto"},
            {"embedding_format": []},
            {"expected_dimension": True},
            {"expected_dimension": 0},
            {"expected_dimension": 1.5},
            {"id_column": "category"},
            {"id_column": ""},
            {"embedding_column": None},
            {"schema": ""},
            {"schema": "main\0"},
            {"max_rows": 0},
        ]
        with patch.object(connectors.sa, "create_engine") as create:
            for kwargs in configurations:
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    load_embeddings_from_sql_table("vector_store", **kwargs)
            with self.assertRaisesRegex(ValueError, "schema separately"):
                load_embeddings_from_sql_table("public.vector_store")
            create.assert_not_called()

    def test_embedding_batches_do_not_truncate_or_accept_empty_results(self):
        with self.assertRaisesRegex(BatchValidationError, "exceeds max_rows"):
            load_embeddings_from_sql_table("vector_store", max_rows=1)
        self.assertEqual(load_embeddings_from_sql_table("vector_store", max_rows=2)[2].shape, (2, 2))
        self.replace_vectors([])
        with self.assertRaisesRegex(BatchValidationError, "empty batch"):
            load_embeddings_from_sql_table("vector_store")

    def test_json_embeddings_reject_malformed_and_non_numeric_values(self):
        invalid = [
            "[1, 2] trailing-sensitive-data",
            "[1, 2,]",
            "1,2",
            "__import__('os').system('not-executed')",
            "null",
            "1",
            '"[1,2]"',
            '{"vector": [1, 2]}',
            "[[1, 2]]",
            "[[1], [1, 2]]",
            "[]",
            "[0, 0]",
            "[1, null]",
            "[true, 2]",
            "[1, false]",
            '["1", 2]',
            "[NaN, 1]",
            "[Infinity, 1]",
            "[-Infinity, 1]",
            "[1e999, 1]",
            "[" * 1100 + "1" + "]" * 1100,
            None,
            b"[1, 2]",
        ]
        for value in invalid:
            with self.subTest(value=value):
                self.replace_vectors(
                    [
                        {"id": "first", "category": "support", "embedding": "[1, 2]"},
                        {"id": "second", "category": "support", "embedding": value},
                    ]
                )
                with self.assertRaisesRegex(BatchValidationError, "Row 2:") as caught:
                    load_embeddings_from_sql_table("vector_store")
                self.assertNotIn("trailing-sensitive-data", str(caught.exception))

    def test_wrong_and_inconsistent_dimensions_fail(self):
        with self.assertRaisesRegex(BatchValidationError, "Row 1:"):
            load_embeddings_from_sql_table("vector_store", expected_dimension=3)
        self.replace_vectors(
            [
                {"id": "1", "category": "support", "embedding": "[1, 2]"},
                {"id": "2", "category": "support", "embedding": "[1]"},
            ]
        )
        with self.assertRaisesRegex(BatchValidationError, "Row 2:"):
            load_embeddings_from_sql_table("vector_store")

    def test_explicit_binary_formats_use_little_endian_float_values(self):
        for format_name, dtype in [("float32_le", "<f4"), ("float64_le", "<f8")]:
            with self.subTest(format_name=format_name):
                data = np.array([1.5, -2.25], dtype=dtype).tobytes()
                self.replace_vectors([{"id": "binary", "category": "group", "embedding": data}])
                _, _, matrix = load_embeddings_from_sql_table(
                    "vector_store", embedding_format=format_name, expected_dimension=2
                )
                np.testing.assert_array_equal(matrix, [[1.5, -2.25]])
                self.assertEqual(matrix.dtype, np.dtype(np.float64))

    def test_binary_formats_reject_empty_misaligned_nonfinite_or_wrong_transports(self):
        for value in [
            b"",
            b"\x01\x00\x00",
            np.array([0, 0], dtype="<f4").tobytes(),
            np.array([1, np.nan], dtype="<f4").tobytes(),
            np.array([1, np.inf], dtype="<f4").tobytes(),
            "[1,2]",
        ]:
            with self.subTest(value=value):
                self.replace_vectors([{"id": "binary", "category": "group", "embedding": value}])
                with self.assertRaisesRegex(BatchValidationError, "Row 1:"):
                    load_embeddings_from_sql_table("vector_store", embedding_format="float32_le")

    def test_decoded_array_driver_values_are_explicit_and_strict(self):
        valid = [[1, 2.5], (1, 2.5), np.array([1, 2.5], dtype=np.float32)]
        invalid = [
            "[1,2]",
            b"[1,2]",
            [1, True],
            [False, 2],
            [1, "2"],
            [1, None],
            [1, complex(2, 1)],
            [],
            [0, 0],
            [1, np.inf],
            [1, np.nan],
            [[1, 2], [3]],
            np.array([True, False]),
            np.array([1, 2], dtype=object),
            np.array(["1", "2"]),
            np.ma.array([1, 2], mask=[False, True]),
        ]
        for value in valid + invalid:
            rows = (["id", "category", "embedding"], [("id", "group", value)])
            with self.subTest(value=value), patch.object(connectors, "_fetch_sql", return_value=rows):
                if any(value is candidate for candidate in valid):
                    matrix = load_embeddings_from_sql_table("vectors", embedding_format="array")[2]
                    np.testing.assert_array_equal(matrix, [[1, 2.5]])
                else:
                    with self.assertRaisesRegex(BatchValidationError, "Row 1:"):
                        load_embeddings_from_sql_table("vectors", embedding_format="array")

    def test_binary_buffers_and_extreme_finite_vectors(self):
        for value in [
            bytearray(np.array([1, 2], dtype="<f8").tobytes()),
            memoryview(np.array([1, 2], dtype="<f8").tobytes()),
        ]:
            with patch.object(
                connectors, "_fetch_sql",
                return_value=(["id", "category", "embedding"], [(1, "group", value)]),
            ):
                matrix = load_embeddings_from_sql_table("vectors", embedding_format="float64_le")[2]
                np.testing.assert_array_equal(matrix, [[1, 2]])
        for value in ["[1e308, 1e308]", "[5e-324, 0]"]:
            self.replace_vectors([{"id": "huge", "category": "group", "embedding": value}])
            matrix = load_embeddings_from_sql_table("vector_store")[2]
            self.assertTrue(np.isfinite(matrix).all())

    def test_metadata_nulls_blanks_and_duplicate_ids_are_rejected(self):
        invalid = [
            {"id": None, "category": "group", "embedding": "[1, 2]"},
            {"id": "", "category": "group", "embedding": "[1, 2]"},
            {"id": " \t", "category": "group", "embedding": "[1, 2]"},
            {"id": "second", "category": None, "embedding": "[1, 2]"},
            {"id": "second", "category": "", "embedding": "[1, 2]"},
            {"id": "second", "category": " \n", "embedding": "[1, 2]"},
            {"id": "first", "category": "another", "embedding": "[1, 2]"},
        ]
        for row in invalid:
            with self.subTest(row=row):
                self.replace_vectors(
                    [{"id": "first", "category": "group", "embedding": "[1, 2]"}, row]
                )
                with self.assertRaisesRegex(BatchValidationError, "Row 2:"):
                    load_embeddings_from_sql_table("vector_store")

    def test_driver_metadata_types_are_not_stringified(self):
        invalid_ids = [True, np.bool_(True), 1.5, float("nan"), b"id", [], {}]
        invalid_categories = [1, False, float("nan"), b"group", [], {}]
        for row in (
            [(value, "group", "[1,2]") for value in invalid_ids]
            + [("id", value, "[1,2]") for value in invalid_categories]
        ):
            with self.subTest(row=row), patch.object(
                connectors, "_fetch_sql",
                return_value=(["id", "category", "embedding"], [row]),
            ):
                with self.assertRaisesRegex(BatchValidationError, "Row 1:"):
                    load_embeddings_from_sql_table("vectors")
        original_ids = [123, UUID("52b632b3-98ce-44ec-a499-870116be4c03"), "00123"]
        with patch.object(
            connectors, "_fetch_sql",
            return_value=(
                ["id", "category", "embedding"],
                [(value, "group", "[1,2]") for value in original_ids],
            ),
        ):
            ids, _, _ = load_embeddings_from_sql_table("vectors")
        self.assertEqual(ids, original_ids)
        for actual, original in zip(ids, original_ids):
            self.assertIs(actual, original)


class SQLResourceTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"CONNECTOR_TEST_SQL": "sqlite:///unused.sqlite"})
        env.start()
        self.addCleanup(env.stop)
        self.engine = MagicMock()
        self.engine.dialect.name = "postgresql"
        self.connection = self.engine.connect.return_value.__enter__.return_value
        self.connection.execution_options.return_value = self.connection
        self.result = self.connection.execute.return_value.__enter__.return_value
        self.result.returns_rows = True
        self.result.keys.return_value = ["value"]
        self.result.fetchmany.return_value = [(1,)]
        create = patch.object(connectors.sa, "create_engine", return_value=self.engine)
        self.create_engine = create.start()
        self.addCleanup(create.stop)

    def test_resources_close_and_postgres_transaction_is_read_only(self):
        load_dataframe_from_sql("SELECT 1 AS value", env_var="CONNECTOR_TEST_SQL", max_rows=3)
        self.connection.exec_driver_sql.assert_called_once_with("SET TRANSACTION READ ONLY")
        self.connection.execution_options.assert_called_once_with(
            stream_results=True, max_row_buffer=4
        )
        self.result.fetchmany.assert_called_once_with(4)
        self.connection.execute.return_value.__exit__.assert_called_once()
        self.engine.connect.return_value.__exit__.assert_called_once()
        self.engine.dispose.assert_called_once()
        self.connection.commit.assert_not_called()
        self.assertEqual(self.create_engine.call_args.kwargs, {"echo": False, "hide_parameters": True})

    def test_sqlite_guard_and_sql_server_no_unsupported_read_only_command(self):
        for dialect, guard in [("sqlite", "PRAGMA query_only = ON"), ("mssql", None)]:
            with self.subTest(dialect=dialect):
                self.connection.exec_driver_sql.reset_mock()
                self.engine.dialect.name = dialect
                load_dataframe_from_sql("SELECT 1 AS value", env_var="CONNECTOR_TEST_SQL")
                if guard:
                    self.connection.exec_driver_sql.assert_called_once_with(guard)
                else:
                    self.connection.exec_driver_sql.assert_not_called()

    def test_resources_close_on_overflow_or_malformed_results(self):
        for columns, rows, returns_rows in [
            (["value"], [(1,), (2,)], True),
            (["value"], [], True),
            (["value"], [(1, 2)], True),
            (["", "second"], [(1, 2)], True),
            (["value"], [(1,)], False),
        ]:
            with self.subTest(columns=columns, rows=rows, returns_rows=returns_rows):
                self.engine.dispose.reset_mock()
                self.engine.connect.return_value.__exit__.reset_mock()
                self.result.keys.return_value = columns
                self.result.fetchmany.return_value = rows
                self.result.returns_rows = returns_rows
                with self.assertRaises(BatchValidationError):
                    load_dataframe_from_sql(
                        "SELECT 1 AS value", env_var="CONNECTOR_TEST_SQL", max_rows=1
                    )
                self.engine.connect.return_value.__exit__.assert_called_once()
                self.engine.dispose.assert_called_once()

    def test_engine_is_disposed_if_connecting_fails(self):
        self.engine.connect.side_effect = OperationalError(
            "sensitive-query", {"private": "parameter"}, Exception("private-credential")
        )
        try:
            load_dataframe_from_sql("SELECT 1", env_var="CONNECTOR_TEST_SQL")
        except DataConnectorError:
            rendered = traceback.format_exc()
            self.assertNotIn("private-credential", rendered)
            self.assertNotIn("sensitive-query", rendered)
        else:
            self.fail("Connection failure was not reported.")
        self.engine.dispose.assert_called_once()

    def test_result_and_connection_close_when_fetching_fails(self):
        self.result.fetchmany.side_effect = OperationalError(
            "sensitive-query", {}, Exception("sensitive-driver-message")
        )
        with self.assertRaises(DataConnectorError):
            load_dataframe_from_sql("SELECT 1", env_var="CONNECTOR_TEST_SQL")
        self.connection.execute.return_value.__exit__.assert_called_once()
        self.engine.connect.return_value.__exit__.assert_called_once()
        self.engine.dispose.assert_called_once()

    def test_connections_are_closed_before_embedding_validation(self):
        self.result.keys.return_value = ["id", "category", "embedding"]
        self.result.fetchmany.return_value = [(1, "group", "[true, 1]")]
        with self.assertRaisesRegex(BatchValidationError, "Row 1:"):
            load_embeddings_from_sql_table("vectors", env_var="CONNECTOR_TEST_SQL")
        self.connection.execute.return_value.__exit__.assert_called_once()
        self.engine.connect.return_value.__exit__.assert_called_once()
        self.engine.dispose.assert_called_once()

    def test_missing_driver_error_does_not_echo_driver_message(self):
        self.create_engine.side_effect = ModuleNotFoundError("sensitive-driver-config")
        with self.assertRaisesRegex(ImportError, "Install the DBAPI driver") as caught:
            load_dataframe_from_sql("SELECT 1", env_var="CONNECTOR_TEST_SQL")
        self.assertNotIn("sensitive-driver-config", str(caught.exception))

    def test_non_sdk_programming_errors_are_not_swallowed(self):
        self.connection.execute.side_effect = RuntimeError("programming failure")
        with self.assertRaisesRegex(RuntimeError, "programming failure"):
            load_dataframe_from_sql("SELECT 1", env_var="CONNECTOR_TEST_SQL")
        self.engine.dispose.assert_called_once()


class FakeRequestProperties:
    def __init__(self):
        self.options = {}
        self.parameters = {}

    def set_option(self, name, value):
        self.options[name] = value

    def set_parameter(self, name, value):
        self.parameters[name] = value


class FakeKustoError(Exception):
    pass


class FakeAzureError(Exception):
    pass


class FakeKustoTable:
    def __init__(self, columns, rows):
        self.columns = [SimpleNamespace(column_name=name) for name in columns]
        self.rows = rows
        self.yielded = 0

    def __iter__(self):
        for row in self.rows:
            self.yielded += 1
            yield row


def fake_response(rows=None, columns=None, errors_count=0):
    table = FakeKustoTable(
        columns if columns is not None else ["Timestamp", "Count", "Message"],
        rows if rows is not None else [(datetime(2026, 9, 1, tzinfo=timezone.utc), 3, "source")],
    )
    return SimpleNamespace(errors_count=errors_count, primary_results=[table])


class KustoConnectorTests(unittest.TestCase):
    query = "Telemetry | where Timestamp > ago(1h) | project Timestamp, Count, Message"

    def setUp(self):
        env = patch.dict(
            os.environ,
            {
                "AZURE_KUSTO_CLUSTER": "https://example.westus.kusto.windows.net",
                "AZURE_KUSTO_DATABASE": "TelemetryDatabase",
            },
            clear=True,
        )
        env.start()
        self.addCleanup(env.stop)
        names = [
            "azure", "azure.core", "azure.core.exceptions", "azure.identity",
            "azure.kusto", "azure.kusto.data", "azure.kusto.data.exceptions",
        ]
        modules = {name: ModuleType(name) for name in names}
        for module in modules.values():
            module.__path__ = []
        modules["azure.core.exceptions"].AzureError = FakeAzureError
        modules["azure.kusto.data.exceptions"].KustoError = FakeKustoError
        self.sdk = modules["azure.kusto.data"]
        self.sdk.ClientRequestProperties = FakeRequestProperties
        self.sdk.KustoConnectionStringBuilder = Mock()
        self.sdk.KustoConnectionStringBuilder.with_azure_token_credential.side_effect = (
            lambda cluster, credential: SimpleNamespace(cluster=cluster, credential=credential)
        )
        self.client = Mock(spec=["execute_query", "close"])
        self.client.execute_query.return_value = fake_response()

        def new_client(builder):
            self.client.close.side_effect = builder.credential.close
            return self.client

        self.sdk.KustoClient = Mock(side_effect=new_client)
        self.identity = modules["azure.identity"]
        self.managed_credential = Mock(spec=["get_token", "close"])
        self.default_credential = Mock(spec=["get_token", "close"])
        self.identity.ManagedIdentityCredential = Mock(return_value=self.managed_credential)
        self.identity.DefaultAzureCredential = Mock(return_value=self.default_credential)
        modules_patch = patch.dict(sys.modules, modules)
        modules_patch.start()
        self.addCleanup(modules_patch.stop)

    def test_injected_client_uses_query_endpoint_and_retains_schema(self):
        frame = load_dataframe_from_kusto(
            self.query, max_rows=5, timeout_seconds=42, client=self.client
        )
        self.assertIsInstance(frame, pd.DataFrame)
        self.assertEqual(frame.columns.tolist(), ["Timestamp", "Count", "Message"])
        self.assertEqual(frame["Count"].tolist(), [3])
        self.assertEqual(frame["Timestamp"].iloc[0], pd.Timestamp("2026-09-01T00:00:00Z"))
        args, kwargs = self.client.execute_query.call_args
        self.assertEqual(args, ("TelemetryDatabase", self.query))
        options = kwargs["properties"].options
        self.assertEqual(options["servertimeout"], timedelta(seconds=42))
        self.assertEqual(options["query_take_max_records"], 6)
        self.assertEqual(options["truncationmaxrecords"], 6)
        for name in [
            "request_readonly", "request_readonly_hardline",
            "request_sandboxed_execution_disabled", "request_callout_disabled",
            "request_external_data_disabled", "request_remote_entities_disabled",
            "request_impersonation_disabled",
        ]:
            self.assertIs(options[name], True)
        for name in [
            "deferpartialqueryfailures", "query_log_query_parameters", "notruncation",
            "best_effort", "norequesttimeout", "results_progressive_enabled",
        ]:
            self.assertIs(options[name], False)
        self.client.close.assert_not_called()
        self.sdk.KustoClient.assert_not_called()
        self.identity.ManagedIdentityCredential.assert_not_called()
        self.identity.DefaultAzureCredential.assert_not_called()

    def test_kusto_parameters_are_separate_from_query(self):
        query = "declare query_parameters(start:datetime); Telemetry | where Timestamp > start"
        params = {"start": "datetime(2026-09-01T00:00:00Z)"}
        load_dataframe_from_kusto(query, params=params, client=self.client)
        args, kwargs = self.client.execute_query.call_args
        self.assertEqual(args[1], query)
        self.assertEqual(kwargs["properties"].parameters, params)

    def test_managed_identity_is_default_and_owned_resources_close(self):
        load_dataframe_from_kusto(self.query)
        self.identity.ManagedIdentityCredential.assert_called_once_with(client_id=None)
        self.identity.DefaultAzureCredential.assert_not_called()
        builder = self.sdk.KustoClient.call_args.args[0]
        self.assertEqual(builder.cluster, "https://example.westus.kusto.windows.net")
        self.client.close.assert_called_once()
        self.managed_credential.close.assert_called_once()

    def test_managed_identity_client_id_is_environment_driven(self):
        with patch.dict(os.environ, {"AZURE_CLIENT_ID": "a-user-assigned-client-id"}):
            load_dataframe_from_kusto(self.query)
        self.identity.ManagedIdentityCredential.assert_called_once_with(
            client_id="a-user-assigned-client-id"
        )

    def test_default_azure_credential_requires_explicit_local_opt_in(self):
        with patch.dict(os.environ, {"AZURE_AUTH_MODE": "default", "AZURE_CLIENT_ID": "identity-id"}):
            load_dataframe_from_kusto(self.query)
        self.identity.DefaultAzureCredential.assert_called_once_with(
            managed_identity_client_id="identity-id",
            exclude_interactive_browser_credential=True,
            exclude_broker_credential=True,
        )
        self.identity.ManagedIdentityCredential.assert_not_called()
        self.default_credential.close.assert_called_once()
        self.client.close.assert_called_once()

    def test_injected_credential_is_borrowed_even_when_sdk_closes_it(self):
        credential = Mock(spec=["get_token", "close"])
        token = object()
        credential.get_token.return_value = token
        load_dataframe_from_kusto(self.query, credential=credential)
        borrowed = self.sdk.KustoClient.call_args.args[0].credential
        self.assertIs(borrowed.get_token("https://kusto.kusto.windows.net/.default"), token)
        credential.get_token.assert_called_once()
        credential.close.assert_not_called()
        self.client.close.assert_called_once()
        self.identity.ManagedIdentityCredential.assert_not_called()

    def test_owned_credential_closes_if_client_construction_fails(self):
        self.sdk.KustoClient.side_effect = FakeKustoError("sensitive-construction-detail")
        with self.assertRaises(DataConnectorError):
            load_dataframe_from_kusto(self.query)
        self.managed_credential.close.assert_called_once()
        self.client.close.assert_not_called()

    def test_query_failure_is_sanitized_and_owned_resources_close(self):
        for error in [FakeKustoError("secret-query-and-rows"), FakeAzureError("secret-token")]:
            with self.subTest(error_type=type(error).__name__):
                self.client.reset_mock()
                self.managed_credential.reset_mock()
                self.client.execute_query.side_effect = error
                try:
                    load_dataframe_from_kusto(self.query)
                except DataConnectorError:
                    self.assertNotIn(str(error), traceback.format_exc())
                else:
                    self.fail("SDK failure was not reported.")
                self.client.close.assert_called_once()
                self.managed_credential.close.assert_called_once()

    def test_partial_query_failure_never_returns_partial_rows(self):
        response = fake_response(errors_count=1)
        self.client.execute_query.return_value = response
        with self.assertRaisesRegex(DataConnectorError, "partial query errors"):
            load_dataframe_from_kusto(self.query)
        self.assertEqual(response.primary_results[0].yielded, 0)
        self.client.close.assert_called_once()
        self.managed_credential.close.assert_called_once()

    def test_empty_and_overflow_batches_fail_and_resources_close(self):
        for rows, message in [([], "empty batch"), ([(1,), (2,), (3,)], "exceeds max_rows")]:
            with self.subTest(rows=rows):
                self.client.reset_mock()
                self.managed_credential.reset_mock()
                self.client.execute_query.return_value = fake_response(rows, ["value"])
                with self.assertRaisesRegex(BatchValidationError, message):
                    load_dataframe_from_kusto(self.query, max_rows=2)
                self.client.close.assert_called_once()
                self.managed_credential.close.assert_called_once()
        self.client.execute_query.return_value = fake_response([(1,), (2,)], ["value"])
        self.assertEqual(len(load_dataframe_from_kusto(self.query, max_rows=2)), 2)

    def test_only_max_rows_plus_one_rows_are_consumed_from_an_injected_response(self):
        response = fake_response(((index,) for index in range(100)), ["value"])
        self.client.execute_query.return_value = response
        with self.assertRaisesRegex(BatchValidationError, "exceeds max_rows"):
            load_dataframe_from_kusto(self.query, max_rows=2, client=self.client)
        self.assertEqual(response.primary_results[0].yielded, 3)
        self.client.close.assert_not_called()

    def test_missing_multiple_or_malformed_result_tables_fail(self):
        cases = [
            SimpleNamespace(primary_results=[FakeKustoTable(["value"], [(1,)])]),
            SimpleNamespace(errors_count="0", primary_results=[]),
            SimpleNamespace(errors_count=0, primary_results=[]),
            SimpleNamespace(errors_count=0, primary_results=[object(), object()]),
            SimpleNamespace(errors_count=0, primary_results=[object()]),
            fake_response([(1, 2)], ["same", "same"]),
            fake_response([(1,)], [""]),
            fake_response([(1,)], ["first", "second"]),
            fake_response([{"first": 1}], ["first"]),
            fake_response(["sensitive-row"], ["first"]),
            fake_response([None], ["first"]),
        ]
        for response in cases:
            with self.subTest(response_type=type(response).__name__):
                self.client.execute_query.return_value = response
                with self.assertRaises(BatchValidationError) as caught:
                    load_dataframe_from_kusto(self.query, client=self.client)
                self.assertNotIn("sensitive-row", str(caught.exception))
        self.client.close.assert_not_called()

    def test_untrusted_endpoints_fail_before_authentication_or_query(self):
        invalid = [
            "",
            "http://example.kusto.windows.net",
            "https://localhost",
            "https://127.0.0.1",
            "https://example.com",
            "https://kusto.windows.net",
            "https://notkusto.windows.net",
            "https://example.kusto.windows.net.attacker.example",
            "https://example.kusto.windows.net@attacker.example",
            "https://user:private-password@example.kusto.windows.net",
            "https://example.kusto.windows.net:444",
            "https://example.kusto.windows.net:invalid",
            "https://example.kusto.windows.net/v2/rest/query",
            "https://example.kusto.windows.net?secret=value",
            "https://example.kusto.windows.net?",
            "https://example.kusto.windows.net#fragment",
            "https://example.kusto.windows.net#",
            "https://exam\nple.kusto.windows.net",
        ]
        for endpoint in invalid:
            with self.subTest(endpoint=endpoint), patch.dict(
                os.environ, {"AZURE_KUSTO_CLUSTER": endpoint}
            ):
                with self.assertRaises(ValueError) as caught:
                    load_dataframe_from_kusto(self.query)
                self.assertNotIn("private-password", str(caught.exception))
        self.sdk.KustoClient.assert_not_called()
        self.identity.ManagedIdentityCredential.assert_not_called()
        self.client.execute_query.assert_not_called()

    def test_https_default_port_and_trailing_slash_are_allowed(self):
        with patch.dict(
            os.environ, {"AZURE_KUSTO_CLUSTER": "https://example.kusto.windows.net:443/"}
        ):
            load_dataframe_from_kusto(self.query)
        self.assertEqual(
            self.sdk.KustoClient.call_args.args[0].cluster,
            "https://example.kusto.windows.net:443",
        )

    def test_missing_database_invalid_auth_and_injection_options_fail(self):
        for env in [
            {"AZURE_KUSTO_DATABASE": ""},
            {"AZURE_KUSTO_DATABASE": " \n"},
            {"AZURE_AUTH_MODE": ""},
            {"AZURE_AUTH_MODE": "interactive"},
            {"AZURE_AUTH_MODE": "DEFAULT"},
        ]:
            with self.subTest(env=env), patch.dict(os.environ, env), self.assertRaises(ValueError):
                load_dataframe_from_kusto(self.query)
        for kwargs in [
            {"client": self.client, "credential": object()},
            {"client": object()},
            {"credential": object()},
        ]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                load_dataframe_from_kusto(self.query, **kwargs)
        self.identity.ManagedIdentityCredential.assert_not_called()
        self.client.execute_query.assert_not_called()

    def test_control_commands_plugins_and_limit_overrides_are_refused(self):
        invalid = [
            ".show tables",
            "// comment\n.drop table Telemetry",
            "/* comment */ .clear table Telemetry data",
            "let window = 1h; .set-or-append Telemetry <| Telemetry",
            "set notruncation; Telemetry | where Timestamp > ago(1h)",
            "let window = 1h; set truncationmaxrecords = 1; Telemetry",
            "Telemetry | evaluate python()",
            "externaldata (x:string) [ 'https://not-approved.example' ]",
            "",
            "// only comment",
        ]
        for query in invalid:
            with self.subTest(query=query), self.assertRaises(ValueError):
                load_dataframe_from_kusto(query, client=self.client)
        self.client.execute_query.assert_not_called()

    def test_async_resources_are_not_accepted_by_the_synchronous_loader(self):
        async def asynchronous_call(*args, **kwargs):
            return None

        for kwargs in [
            {"client": SimpleNamespace(execute_query=asynchronous_call)},
            {"credential": SimpleNamespace(get_token=asynchronous_call)},
        ]:
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(ValueError, "synchronous"):
                load_dataframe_from_kusto(self.query, **kwargs)
        self.sdk.KustoClient.assert_not_called()

    def test_kql_literal_strings_are_not_executed_or_mistaken_for_commands(self):
        query = self.query + """ | where Message == '.drop table Example; evaluate python()'"""
        load_dataframe_from_kusto(query, client=self.client)
        self.assertEqual(self.client.execute_query.call_args.args[1], query)

    def test_invalid_row_limits_timeouts_and_parameters_fail(self):
        invalid = (
            [{"max_rows": value} for value in [0, -1, True, 1.5]]
            + [{"timeout_seconds": value} for value in [0, -1, True, 1.5, 3601]]
            + [{"params": value} for value in [[], {"count": 3}, {"invalid-name": "1"}]]
        )
        for kwargs in invalid:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                load_dataframe_from_kusto(self.query, client=self.client, **kwargs)
        self.client.execute_query.assert_not_called()

    def test_azure_import_failure_is_actionable_and_sql_import_remains_optional(self):
        with patch.dict(sys.modules, {"azure.kusto.data": None}):
            with self.assertRaisesRegex(ImportError, "requires azure-kusto-data"):
                load_dataframe_from_kusto(self.query, client=self.client)
        self.client.execute_query.assert_not_called()

    def test_non_sdk_programming_errors_are_not_swallowed(self):
        self.client.execute_query.side_effect = RuntimeError("programming failure")
        with self.assertRaisesRegex(RuntimeError, "programming failure"):
            load_dataframe_from_kusto(self.query)
        self.client.close.assert_called_once()
        self.managed_credential.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
