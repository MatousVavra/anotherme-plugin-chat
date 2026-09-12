import asyncio
import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def client(make_client):
    return make_client()


# ===========================================================================
# Chat plugin tests
# ===========================================================================

# --- 1. Plugin listed ---

def test_chat_plugin_listed(client):
    names = {p["name"] for p in client.get("/plugins").json()}
    assert "chat" in names


# --- 2. Tables created ---

def test_chat_tables_created(client):
    import src.database
    conn = src.database.get_db()
    tables = {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert "chat_threads" in tables
    assert "chat_messages" in tables


# --- 3. Chat send (mocked) ---

def test_chat_send(client):
    async def fake_chat(*a, **kw):
        return "Hello!"

    with patch("src.llm.chat", side_effect=fake_chat):
        resp = client.post(
            "/plugins/chat/send",
            json={"messages": [{"role": "user", "content": "Hi"}]},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["reply"] == "Hello!"
    assert data["conversation_id"] is not None


# --- 4. Chat send creates conversation ---

def test_chat_send_creates_conversation(client):
    async def fake_chat(*a, **kw):
        return "Reply"

    with patch("src.llm.chat", side_effect=fake_chat):
        resp = client.post(
            "/plugins/chat/send",
            json={"messages": [{"role": "user", "content": "Hi"}]},
        )
    assert resp.status_code == 200
    cid = resp.json()["conversation_id"]
    assert cid is not None

    # Conversation exists
    resp = client.get("/plugins/chat/conversations")
    assert resp.status_code == 200
    convos = resp.json()
    assert any(c["id"] == cid for c in convos)


# --- 5. Chat stream (mocked SSE) ---

def test_chat_stream(client):
    class FakeDelta:
        def __init__(self, content=None, tool_calls=None):
            self.content = content
            self.tool_calls = tool_calls

    class FakeChoice:
        def __init__(self, delta):
            self.delta = delta

    class FakeChunk:
        def __init__(self, **kwargs):
            self.choices = [FakeChoice(FakeDelta(**kwargs))]

    stream_chunks = [FakeChunk(content="Hello "), FakeChunk(content="world!")]

    class FakeAsyncStream:
        def __init__(self, chunks):
            self._chunks = list(chunks)
        def __aiter__(self):
            self._iter = iter(self._chunks)
            return self
        async def __anext__(self):
            try:
                return next(self._iter)
            except StopIteration:
                raise StopAsyncIteration

    class FakeCompletions:
        async def create(self, **kwargs):
            return FakeAsyncStream(stream_chunks)

    class FakeChat:
        completions = FakeCompletions()

    class FakeAsyncClient:
        chat = FakeChat()

    fake_async_client = FakeAsyncClient()

    async def noop(*a, **k):
        pass

    with patch("src.llm.get_async_client", return_value=fake_async_client), \
         patch("src.llm.LLM_MODEL", "test-model"), \
         patch("src.tools.execute_tool", side_effect=noop):
        resp = client.post(
            "/plugins/chat/stream",
            json={"messages": [{"role": "user", "content": "Hi"}]},
        )
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "text" in body
    assert "done" in body
    assert "Hello " in body
    assert "world!" in body


def test_chat_stream_with_tools(client):
    class FakeFunction:
        def __init__(self, name=None, arguments=None):
            self.name = name
            self.arguments = arguments

    class FakeToolCall:
        def __init__(self, index=0, id=None, function=None):
            self.index = index
            self.id = id
            self.function = function or FakeFunction()

    class FakeDelta:
        def __init__(self, content=None, tool_calls=None):
            self.content = content
            self.tool_calls = tool_calls

    class FakeChoice:
        def __init__(self, delta):
            self.delta = delta

    class FakeChunk:
        def __init__(self, **kwargs):
            self.choices = [FakeChoice(FakeDelta(**kwargs))]

    tool_call_chunk = FakeChunk(tool_calls=[
        FakeToolCall(index=0, id="call_1", function=FakeFunction(name="search_vault", arguments='{"query":"test"}'))
    ])
    follow_up_chunks = [FakeChunk(content="Found it"), FakeChunk(content="!")]

    class FakeAsyncStream:
        def __init__(self, chunks):
            self._chunks = list(chunks)
        def __aiter__(self):
            self._iter = iter(self._chunks)
            return self
        async def __anext__(self):
            try:
                return next(self._iter)
            except StopIteration:
                raise StopAsyncIteration

    class FakeCompletions:
        def __init__(self):
            self.call_count = 0
        async def create(self, **kwargs):
            self.call_count += 1
            if self.call_count == 1:
                return FakeAsyncStream([tool_call_chunk])
            return FakeAsyncStream(follow_up_chunks)

    class FakeChat:
        completions = FakeCompletions()

    class FakeAsyncClient:
        chat = FakeChat()

    fake_async_client = FakeAsyncClient()

    async def fake_execute(*a, **k):
        return "search result: found 2 items"

    with patch("src.llm.get_async_client", return_value=fake_async_client), \
         patch("src.llm.LLM_MODEL", "test-model"), \
         patch("src.tools.execute_tool", side_effect=fake_execute):
        resp = client.post(
            "/plugins/chat/stream",
            json={"messages": [{"role": "user", "content": "Search for test"}]},
        )
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "tool_call" in body
    assert "search_vault" in body
    assert "tool_result" in body
    assert "Found it" in body
    assert "done" in body


# --- 6. Chat summarize ---

def test_chat_summarize(client):
    # Seed messages via the send endpoint (mocked LLM)
    async def fake_chat(*a, **kw):
        return "Reply"

    with patch("src.llm.chat", side_effect=fake_chat):
        resp1 = client.post(
            "/plugins/chat/send",
            json={"messages": [{"role": "user", "content": "Hi"}]},
        )
    cid = resp1.json()["conversation_id"]

    # We need 4+ messages for summarize — send more
    with patch("src.llm.chat", side_effect=fake_chat):
        resp2 = client.post(
            "/plugins/chat/send",
            json={
                "conversation_id": cid,
                "messages": [
                    {"role": "user", "content": "Hi"},
                    {"role": "assistant", "content": "Reply"},
                    {"role": "user", "content": "Tell me about X"},
                ],
            },
        )

    async def fake_summary(*a, **kw):
        return "- Topic A\n- Topic B"

    with patch("src.llm.chat", side_effect=fake_summary):
        resp = client.post("/plugins/chat/summarize", params={"conversation_id": cid})
    assert resp.status_code == 200
    data = resp.json()
    assert "summary" in data
    assert "Topic A" in data["summary"]

    # Summary saved as note
    vault = Path(os.environ["VAULTS_DIR"]) / "test-main"
    notes = list((vault / "Notes").glob("Summary*.md"))
    assert len(notes) >= 1


# --- 7. Conversations CRUD ---

def test_chat_conversations_crud(client):
    # Create
    resp = client.post("/plugins/chat/conversations", json={"title": "My thread"})
    assert resp.status_code == 201
    cid = resp.json()["id"]

    # List
    resp = client.get("/plugins/chat/conversations")
    assert resp.status_code == 200
    convos = resp.json()
    assert any(c["id"] == cid and c["title"] == "My thread" for c in convos)

    # Get by id
    resp = client.get(f"/plugins/chat/conversations/{cid}")
    assert resp.status_code == 200
    detail = resp.json()
    assert detail["id"] == cid
    assert detail["title"] == "My thread"

    # Rename
    resp = client.put(f"/plugins/chat/conversations/{cid}", json={"title": "Renamed"})
    assert resp.status_code == 200

    resp = client.get(f"/plugins/chat/conversations/{cid}")
    assert resp.json()["title"] == "Renamed"

    # Delete
    resp = client.delete(f"/plugins/chat/conversations/{cid}")
    assert resp.status_code == 200
    assert client.get(f"/plugins/chat/conversations/{cid}").status_code == 404


# --- 8. Conversation 404s ---

def test_chat_conversation_404s(client):
    assert client.get("/plugins/chat/conversations/99999").status_code == 404
    assert client.put("/plugins/chat/conversations/99999", json={"title": "x"}).status_code == 404
    assert client.delete("/plugins/chat/conversations/99999").status_code == 404


# --- 9. Reprompt ---

def test_chat_reprompt(client):
    # Seed messages via the send endpoint (mocked LLM)
    async def fake_chat(*a, **kw):
        return "Original reply"

    with patch("src.llm.chat", side_effect=fake_chat):
        resp1 = client.post(
            "/plugins/chat/send",
            json={"messages": [{"role": "user", "content": "Original"}]},
        )
    cid = resp1.json()["conversation_id"]

    async def fake_chat2(*a, **kw):
        return "New reply"

    with patch("src.llm.chat", side_effect=fake_chat2):
        resp = client.put(
            f"/plugins/chat/conversations/{cid}/messages/0",
            json={"content": "Edited question"},
        )
    assert resp.status_code == 200
    assert resp.json()["reply"] == "New reply"

    # Check messages were updated
    resp = client.get(f"/plugins/chat/conversations/{cid}")
    msgs = resp.json()["messages"]
    assert msgs[0]["content"] == "Edited question"
    # The old assistant reply was deleted and new one appended
    assert msgs[-1]["content"] == "New reply"


# --- 10. Memory extraction fires ---

def test_chat_memory_extraction_fires(client):
    extract_called = []

    class FakeMemoryApi:
        def extract_facts(self, vault_name, messages):
            extract_called.append((vault_name, messages))
            return {}

    # Patch the plugin's memory API with our fake
    import src.main
    registry = src.main.plugin_manager.get_registry()
    original_memory_api = registry.get_api("memory")
    registry._apis["memory"] = FakeMemoryApi()

    async def fake_chat(*a, **kw):
        return "Reply"

    try:
        with patch("src.llm.chat", side_effect=fake_chat):
            resp = client.post(
                "/plugins/chat/send",
                json={"messages": [{"role": "user", "content": "I like pizza"}]},
            )
        assert resp.status_code == 200
        # Poll for the async extraction task to complete (up to 5s)
        import time
        for _ in range(50):
            if extract_called:
                break
            client.get("/health")
            time.sleep(0.1)
    finally:
        registry._apis["memory"] = original_memory_api

    assert len(extract_called) >= 1


# --- 11. Old chat endpoint removed ---

# (Old /chat endpoint removed in Phase 7)


# --- 12. Old conversations endpoint removed ---

# (Old /conversations endpoint removed in Phase 7)
