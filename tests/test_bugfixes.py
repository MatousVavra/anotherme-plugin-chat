"""Unit tests for verified bug fixes (migrations, message fetch, fallback context)."""
import sqlite3
from unittest.mock import MagicMock

from conftest import load_plugin_module
from fake_plugin_context import FakePluginContext


class _DbModule:
    def __init__(self, conn):
        self._conn = conn

    def get_db(self):
        return self._conn


class _RecordingCtx(FakePluginContext):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.migrations = []

    def register_migration(self, version, sql):
        super().register_migration(version, sql)
        self.migrations.append(version)


def _make_db(with_archived=False):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE chat_threads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vault_name TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT 'New conversation',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vault_name TEXT NOT NULL,
            thread_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    if with_archived:
        conn.execute("ALTER TABLE chat_threads ADD COLUMN is_archived INTEGER DEFAULT 0")
    return conn


def test_migration_v2_skipped_when_column_exists():
    conn = _make_db(with_archived=True)
    ctx = _RecordingCtx(db_module=_DbModule(conn))
    mod = load_plugin_module()
    mod.Plugin().on_load(ctx)
    assert 2 not in ctx.migrations


def test_migration_v2_registered_when_column_missing():
    conn = _make_db(with_archived=False)
    ctx = _RecordingCtx(db_module=_DbModule(conn))
    mod = load_plugin_module()
    mod.Plugin().on_load(ctx)
    assert 2 in ctx.migrations


def test_get_messages_most_recent_returns_latest_and_preserves_order():
    conn = _make_db()
    for i in range(40):
        conn.execute(
            "INSERT INTO chat_messages (vault_name, thread_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
            ("main", 1, "user", f"msg{i:02d}", f"2026-01-01T00:{i:02d}:00Z"),
        )
    mod = load_plugin_module()
    msgs = mod._get_messages(conn, "main", thread_id=1, limit=30, most_recent=True)
    assert len(msgs) == 30
    assert msgs[0]["content"] == "msg10"
    assert msgs[-1]["content"] == "msg39"


def test_get_messages_default_order_unchanged():
    conn = _make_db()
    for i in range(5):
        conn.execute(
            "INSERT INTO chat_messages (vault_name, thread_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
            ("main", 1, "user", f"msg{i}", f"2026-01-01T00:{i:02d}:00Z"),
        )
    mod = load_plugin_module()
    msgs = mod._get_messages(conn, "main", thread_id=1)
    assert [m["content"] for m in msgs] == [f"msg{i}" for i in range(5)]


def test_prepare_request_rejects_unknown_conversation_id():
    conn = _make_db()
    ctx = MagicMock()
    ctx.vault_name = "main"
    ctx.db_module = _DbModule(conn)
    mod = load_plugin_module()
    plugin = mod.Plugin()
    plugin._ctx = ctx
    plugin._db = ctx.db_module
    plugin._bg_tasks = set()
    import pytest
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        import asyncio
        asyncio.run(plugin._prepare_request(mod.ChatRequest(
            messages=[mod.ChatMessage(role="user", content="hi")], conversation_id=999,
        )))
    assert exc.value.status_code == 404
