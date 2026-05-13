"""Unit tests for api.db_helper context managers."""

import unittest
from unittest.mock import patch, MagicMock


class TestDbHelper(unittest.TestCase):
    """Test the db_helper context manager wrappers."""

    @patch("api.db_helper.mysql.connector.connect")
    def test_get_connection_closes_on_success(self, mock_connect):
        from api.db_helper import get_connection
        mock_conn = MagicMock()
        mock_connect.return_value = mock_conn

        with get_connection({"host": "localhost"}) as conn:
            self.assertIs(conn, mock_conn)

        mock_conn.close.assert_called_once()

    @patch("api.db_helper.mysql.connector.connect")
    def test_get_connection_closes_on_exception(self, mock_connect):
        from api.db_helper import get_connection
        mock_conn = MagicMock()
        mock_connect.return_value = mock_conn

        with self.assertRaises(ValueError):
            with get_connection({"host": "localhost"}) as conn:
                raise ValueError("test error")

        mock_conn.close.assert_called_once()

    @patch("api.db_helper.mysql.connector.connect")
    def test_get_cursor_closes_both(self, mock_connect):
        from api.db_helper import get_cursor
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_connect.return_value = mock_conn

        with get_cursor({"host": "localhost"}, dictionary=True) as (conn, cursor):
            self.assertIs(cursor, mock_cursor)

        mock_cursor.close.assert_called_once()
        mock_conn.close.assert_called_once()
        mock_conn.cursor.assert_called_once_with(dictionary=True, buffered=True)

    @patch("api.db_helper.mysql.connector.connect")
    def test_get_cursor_closes_on_exception(self, mock_connect):
        from api.db_helper import get_cursor
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_connect.return_value = mock_conn

        with self.assertRaises(RuntimeError):
            with get_cursor({"host": "localhost"}) as (conn, cursor):
                raise RuntimeError("db error")

        mock_cursor.close.assert_called_once()
        mock_conn.close.assert_called_once()

    def test_default_config(self):
        from api.db_helper import _default_config
        cfg = _default_config()
        self.assertIn("host", cfg)
        self.assertIn("port", cfg)
        self.assertIn("user", cfg)
        self.assertIn("database", cfg)
        self.assertTrue(cfg["use_pure"])


if __name__ == "__main__":
    unittest.main()
