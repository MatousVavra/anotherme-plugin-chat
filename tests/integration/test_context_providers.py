"""Phase 6 Task 2: plugins register context providers; the kernel
ContextBuilder (wired as ctx.context_builder in src/main.py) assembles chat
context from plugin-provided sections — no hardcoded plugin-table queries."""
from unittest.mock import patch

import pytest


@pytest.fixture
def client(make_client):
    return make_client()


def _capture_system_prompt(client, user_text):
    """POST /plugins/chat/send with a mocked LLM; return the system prompt text.

    The background auto-title LLM call may fire before the response returns,
    so identify the main chat call by its `tools` kwarg.
    """
    calls = []

    async def fake_chat(*args, **kwargs):
        calls.append(kwargs)
        return "ok"

    with patch("src.llm.chat", side_effect=fake_chat):
        resp = client.post("/plugins/chat/send", json={
            "messages": [{"role": "user", "content": user_text}],
        })
    assert resp.status_code == 200
    messages = next(kw["messages"] for kw in calls if kw.get("tools") is not None)
    assert messages[0]["role"] == "system"
    return messages[0]["content"]


def _insert_fact(vault="test-main", key="hometown", value="Prague"):
    import src.database
    conn = src.database.get_db()
    conn.execute(
        "INSERT INTO memory_facts (vault_name, key, value, category, created_at) VALUES (?, ?, ?, ?, ?)",
        (vault, key, value, "Preferences", "2026-08-20T00:00:00Z"),
    )
    conn.commit()


def _insert_event(vault="test-main", event_id="evt-1", title="Dentist appointment",
                  date="2099-01-01", time="10:00"):
    import src.database
    conn = src.database.get_db()
    conn.execute(
        "INSERT INTO calendar_events (id, vault_name, title, event_date, event_time, note, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (event_id, vault, title, date, time, "", "2026-08-20T00:00:00Z"),
    )
    conn.commit()


def test_memory_facts_appear_in_chat_context(client):
    _insert_fact(key="hometown", value="Prague")
    system = _capture_system_prompt(client, "Tell me about my hometown")
    assert "Relevant facts" in system
    assert "hometown: Prague" in system


def test_memory_watch_fors_appear_in_chat_context(client, tmp_path):
    people_dir = tmp_path / "vaults" / "test-main" / "People"
    people_dir.mkdir(parents=True, exist_ok=True)
    (people_dir / "Alice.md").write_text(
        "# Alice\n\n## Watch For\n- her birthday in September\n", encoding="utf-8",
    )
    system = _capture_system_prompt(client, "hello")
    assert "Watch For" in system
    assert "her birthday in September" in system


def test_calendar_upcoming_events_appear_in_chat_context(client):
    _insert_event(title="Dentist appointment")
    system = _capture_system_prompt(client, "what's coming up?")
    assert "Upcoming events" in system
    assert "Dentist appointment" in system


def test_chat_mentioned_entities_appear_in_chat_context(client):
    system = _capture_system_prompt(client, "Alice and Bob went to Prague")
    assert "Mentioned in this conversation" in system
    assert "Prague" in system


def test_diary_unresolved_threads_appear_in_chat_context(client, tmp_path):
    diary_dir = tmp_path / "vaults" / "test-main" / "Diary"
    diary_dir.mkdir(parents=True, exist_ok=True)
    (diary_dir / "Diary 2026-08-20-1200.md").write_text(
        "Today was busy. I need to buy more oat milk this weekend.\n", encoding="utf-8",
    )
    system = _capture_system_prompt(client, "hello")
    assert "Unresolved diary threads" in system
    assert "buy more oat milk this weekend" in system


def test_disabling_plugin_removes_its_provider(client):
    """Disable calendar → upcoming-events section vanishes from context.
    Other providers survive the plugin reload, and re-enabling registers the
    provider exactly once (no duplicates after repeated reloads)."""
    _insert_fact(key="hometown", value="Prague")
    _insert_event(title="Dentist appointment")

    resp = client.post("/plugins/calendar/disable")
    assert resp.status_code == 200

    system = _capture_system_prompt(client, "Tell me about my hometown")
    assert "Upcoming events" not in system
    # memory provider still works after the reload
    assert "hometown: Prague" in system

    resp = client.post("/plugins/calendar/enable")
    assert resp.status_code == 200

    system = _capture_system_prompt(client, "what's coming up?")
    assert "Dentist appointment" in system
    assert system.count("Upcoming events") == 1
