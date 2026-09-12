from unittest.mock import patch

import pytest


@pytest.fixture
def client(make_client):
    return make_client()


def _create_conv(client, title="New conversation"):
    return client.post("/plugins/chat/conversations", json={"title": title}).json()


# --- 1. Migration v2: is_archived column ---

def test_is_archived_column_exists(client):
    import src.database
    conn = src.database.get_db()
    cols = [r[1] for r in conn.execute("PRAGMA table_info(chat_threads)").fetchall()]
    assert "is_archived" in cols


# --- 2. ConversationThread response includes is_archived ---

def test_conversation_thread_has_is_archived(client):
    conv = _create_conv(client, "Test chat")
    assert "is_archived" in conv
    assert conv["is_archived"] is False


# --- 3. Archive endpoint ---

def test_archive_conversation(client):
    conv = _create_conv(client, "To archive")
    cid = conv["id"]
    resp = client.post(f"/plugins/chat/conversations/{cid}/archive")
    assert resp.status_code == 200
    # Verify it's archived
    detail = client.get(f"/plugins/chat/conversations/{cid}")
    assert detail.json()["is_archived"] is True  # may need ConversationDetail update too


def test_archive_conversation_404(client):
    resp = client.post("/plugins/chat/conversations/99999/archive")
    assert resp.status_code == 404


# --- 4. Unarchive endpoint ---

def test_unarchive_conversation(client):
    conv = _create_conv(client, "Archived chat")
    cid = conv["id"]
    client.post(f"/plugins/chat/conversations/{cid}/archive")
    resp = client.post(f"/plugins/chat/conversations/{cid}/unarchive")
    assert resp.status_code == 200
    detail = client.get(f"/plugins/chat/conversations/{cid}")
    assert detail.json()["is_archived"] is False


def test_unarchive_conversation_404(client):
    resp = client.post("/plugins/chat/conversations/99999/unarchive")
    assert resp.status_code == 404


# --- 5. GET conversations filters by archive status ---

def test_conversations_default_excludes_archived(client):
    conv = _create_conv(client, "Active chat")
    _create_conv(client, "Archived chat")
    cid2 = client.post("/plugins/chat/conversations", json={"title": "Archived chat 2"}).json()["id"]
    client.post(f"/plugins/chat/conversations/{cid2}/archive")
    resp = client.get("/plugins/chat/conversations")
    titles = [c["title"] for c in resp.json()]
    assert "Archived chat 2" not in titles
    assert "Active chat" in titles


def test_conversations_archived_true_returns_only_archived(client):
    conv = _create_conv(client, "Will archive")
    cid = conv["id"]
    client.post(f"/plugins/chat/conversations/{cid}/archive")
    resp = client.get("/plugins/chat/conversations?archived=true")
    titles = [c["title"] for c in resp.json()]
    assert "Will archive" in titles


# --- 6. Auto-title fires after first exchange ---

def test_auto_title_after_first_exchange(client):
    title_calls = []

    async def fake_chat(*args, **kwargs):
        # If this is the title generation call, return a title
        msgs = kwargs.get("messages", args[0] if args else [])
        if msgs and len(msgs) == 1 and "Generate a concise" in (msgs[0].get("content", "") if isinstance(msgs[0], dict) else ""):
            return "Pizza Order Plans"
        return "Sure, I can help!"

    with patch("src.llm.chat", side_effect=fake_chat):
        resp = client.post("/plugins/chat/send", json={
            "messages": [{"role": "user", "content": "I want to order pizza"}]
        })

    assert resp.status_code == 200
    data = resp.json()
    assert data["title_updated"] is True

    # Verify the conversation was renamed
    import time
    time.sleep(0.5)  # Let the background task complete
    convs = client.get("/plugins/chat/conversations").json()
    assert any(c["title"] == "Pizza Order Plans" for c in convs)


def test_auto_title_not_fired_when_already_titled(client):
    conv = client.post("/plugins/chat/conversations", json={"title": "My custom title"}).json()
    cid = conv["id"]

    async def fake_chat(*args, **kwargs):
        msgs = kwargs.get("messages", args[0] if args else [])
        if msgs and len(msgs) == 1 and "Generate a concise" in (msgs[0].get("content", "") if isinstance(msgs[0], dict) else ""):
            return "Should Not Be Used"
        return "Reply"

    with patch("src.llm.chat", side_effect=fake_chat):
        resp = client.post("/plugins/chat/send", json={
            "messages": [{"role": "user", "content": "hello"}],
            "conversation_id": cid,
        })

    assert resp.status_code == 200
    assert resp.json()["title_updated"] is False


# --- 7. Auto-title SSE stream includes title_updated ---

def test_auto_title_in_stream(client):
    class FakeDelta:
        def __init__(self, content=None):
            self.content = content
            self.tool_calls = None

    class FakeChoice:
        def __init__(self, delta):
            self.delta = delta

    class FakeChunk:
        def __init__(self, content=None):
            self.choices = [FakeChoice(FakeDelta(content=content))]

    stream_chunks = [FakeChunk(content="Hello!")]

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
                return FakeAsyncStream(stream_chunks)
            return FakeAsyncStream([])

    class FakeChat:
        completions = FakeCompletions()

    class FakeAsyncClient:
        chat = FakeChat()

    fake_async_client = FakeAsyncClient()

    async def fake_chat_async(*args, **kwargs):
        msgs = kwargs.get("messages", args[0] if args else [])
        if msgs and len(msgs) == 1 and "Generate a concise" in (msgs[0].get("content", "") if isinstance(msgs[0], dict) else ""):
            return "Stream Title"
        return "fallback"

    with patch("src.llm.get_async_client", return_value=fake_async_client), \
         patch("src.llm.LLM_MODEL", "test-model"), \
         patch("src.llm.chat", side_effect=fake_chat_async):
        resp = client.post("/plugins/chat/stream", json={
            "messages": [{"role": "user", "content": "hi there"}],
        })

    body = resp.content.decode()
    assert "done" in body
    assert '"title_updated": true' in body


# --- 8. Search filters by title and message content ---

def test_search_by_title(client):
    _create_conv(client, "Pizza delivery plans")
    _create_conv(client, "Work meeting notes")
    resp = client.get("/plugins/chat/conversations?q=pizza")
    titles = [c["title"] for c in resp.json()]
    assert "Pizza delivery plans" in titles
    assert "Work meeting notes" not in titles


def test_search_by_message_content(client):
    conv = _create_conv(client, "Random topic")
    cid = conv["id"]

    async def fake_chat(*args, **kwargs):
        return "Solar panels are great"

    with patch("src.llm.chat", side_effect=fake_chat):
        client.post("/plugins/chat/send", json={
            "messages": [{"role": "user", "content": "Tell me about solar panels"}],
            "conversation_id": cid,
        })

    resp = client.get("/plugins/chat/conversations?q=solar")
    titles = [c["title"] for c in resp.json()]
    assert "Random topic" in titles


def test_search_respects_archived_filter(client):
    conv = _create_conv(client, "Archived search test")
    cid = conv["id"]
    client.post(f"/plugins/chat/conversations/{cid}/archive")
    # Search in active conversations — should not find archived
    resp = client.get("/plugins/chat/conversations?q=archived")
    titles = [c["title"] for c in resp.json()]
    assert "Archived search test" not in titles
    # Search in archived conversations — should find it
    resp = client.get("/plugins/chat/conversations?q=archived&archived=true")
    titles = [c["title"] for c in resp.json()]
    assert "Archived search test" in titles
