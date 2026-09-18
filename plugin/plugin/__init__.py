import asyncio
import json as jsonmod
import logging
import re
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from typing import Optional

from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    role: str = Field(pattern=r"^(user|assistant)$")
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage]
    system_prompt: Optional[str] = None
    conversation_id: Optional[int] = None


class ChatResponse(BaseModel):
    reply: str
    model: str
    conversation_id: Optional[int] = None
    title_updated: bool = False


class ConversationMessage(BaseModel):
    role: str
    content: str
    created_at: str


class ConversationThread(BaseModel):
    id: int
    title: str
    created_at: str
    updated_at: str
    is_archived: bool = False


class ConversationCreate(BaseModel):
    title: str | None = None


class ConversationDetail(BaseModel):
    id: int
    title: str
    messages: list[ConversationMessage]
    created_at: str
    updated_at: str
    is_archived: bool = False


class ConversationRename(BaseModel):
    title: str


class RepromptRequest(BaseModel):
    content: str = Field(min_length=1)


BASE_INSTRUCTIONS = (
    "Use tools to read files, search, append, and query diary ranges. "
    "Before creating a person record, search_vault for their name first. "
    "If identity is unclear, ask the user rather than guessing."
)


def _now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _extract_mentioned_entities(text: str) -> list[str]:
    # Look for capitalized words that are likely names or project titles
    matches = re.findall(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b", text)
    stop = {"I", "Im", "Im", "Today", "Yesterday", "Tomorrow", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday", "January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"}
    return list(dict.fromkeys(m for m in matches if m not in stop))[:10]


# ---------------------------------------------------------------------------
# DB helpers (plugin-owned tables via ctx.db_module.get_db())
# ---------------------------------------------------------------------------

def _create_conversation(conn, vault_name, title=None):
    cur = conn.execute(
        "INSERT INTO chat_threads (vault_name, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (vault_name, title or "New conversation", _now_utc(), _now_utc()),
    )
    conn.commit()
    return cur.lastrowid


def _get_conversations(conn, vault_name, archived=False, query=None):
    if query:
        sql = (
            "SELECT DISTINCT t.id, t.title, t.created_at, t.updated_at, t.is_archived "
            "FROM chat_threads t LEFT JOIN chat_messages m ON t.id = m.thread_id "
            "WHERE t.vault_name = ? AND COALESCE(t.is_archived, 0) = ? AND (t.title LIKE ? OR m.content LIKE ?) "
            "ORDER BY t.updated_at DESC"
        )
        pattern = f"%{query}%"
        rows = conn.execute(sql, (vault_name, 1 if archived else 0, pattern, pattern)).fetchall()
    else:
        sql = (
            "SELECT id, title, created_at, updated_at, is_archived FROM chat_threads "
            "WHERE vault_name = ? AND COALESCE(is_archived, 0) = ? ORDER BY updated_at DESC"
        )
        rows = conn.execute(sql, (vault_name, 1 if archived else 0)).fetchall()
    return [
        {"id": r["id"], "title": r["title"], "created_at": r["created_at"],
         "updated_at": r["updated_at"], "is_archived": bool(r["is_archived"])}
        for r in rows
    ]


def _rename_conversation(conn, vault_name, cid, title):
    cur = conn.execute(
        "UPDATE chat_threads SET title = ?, updated_at = ? WHERE vault_name = ? AND id = ?",
        (title, _now_utc(), vault_name, cid),
    )
    conn.commit()
    return cur.rowcount > 0


def _delete_conversation(conn, vault_name, cid):
    conn.execute(
        "DELETE FROM chat_messages WHERE vault_name = ? AND thread_id = ?",
        (vault_name, cid),
    )
    cur = conn.execute(
        "DELETE FROM chat_threads WHERE vault_name = ? AND id = ?",
        (vault_name, cid),
    )
    conn.commit()
    return cur.rowcount > 0


def _set_archived(conn, vault_name, cid, archived):
    cur = conn.execute(
        "UPDATE chat_threads SET is_archived = ?, updated_at = ? WHERE vault_name = ? AND id = ?",
        (1 if archived else 0, _now_utc(), vault_name, cid),
    )
    conn.commit()
    return cur.rowcount > 0


def _save_message(conn, vault_name, role, content, thread_id):
    cur = conn.execute(
        "INSERT INTO chat_messages (vault_name, thread_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
        (vault_name, thread_id, role, content, _now_utc()),
    )
    conn.execute(
        "UPDATE chat_threads SET updated_at = ? WHERE vault_name = ? AND id = ?",
        (_now_utc(), vault_name, thread_id),
    )
    conn.commit()
    return cur.lastrowid


def _get_messages(conn, vault_name, thread_id=None, limit=100, most_recent=False):
    order = "DESC" if most_recent else "ASC"
    if thread_id is not None:
        rows = conn.execute(
            f"SELECT role, content, created_at FROM chat_messages WHERE vault_name = ? AND thread_id = ? ORDER BY created_at {order} LIMIT ?",
            (vault_name, thread_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT role, content, created_at FROM chat_messages WHERE vault_name = ? ORDER BY created_at {order} LIMIT ?",
            (vault_name, limit),
        ).fetchall()
    messages = [
        {"role": r["role"], "content": r["content"], "created_at": r["created_at"]} for r in rows
    ]
    if most_recent:
        messages.reverse()
    return messages


def _clear_conversations(conn, vault_name):
    conn.execute("DELETE FROM chat_messages WHERE vault_name = ?", (vault_name,))
    conn.execute("DELETE FROM chat_threads WHERE vault_name = ?", (vault_name,))
    conn.commit()


def _update_message(conn, vault_name, thread_id, index, content):
    rows = conn.execute(
        "SELECT id FROM chat_messages WHERE vault_name = ? AND thread_id = ? ORDER BY created_at ASC LIMIT 1 OFFSET ?",
        (vault_name, thread_id, index),
    ).fetchall()
    if not rows:
        raise ValueError("Message not found")
    conn.execute(
        "UPDATE chat_messages SET content = ? WHERE id = ?",
        (content, rows[0]["id"]),
    )
    conn.execute(
        "DELETE FROM chat_messages WHERE vault_name = ? AND thread_id = ? AND id > ?",
        (vault_name, thread_id, rows[0]["id"]),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# System prompt builder (local copy using ctx.registry)
# ---------------------------------------------------------------------------

def _build_system_prompt(today, context_text, questions_ctx, base_instructions, registry):
    plugin_prompts = registry.get_prompt_fragments("chat")
    parts = [
        f"Today is {today}.",
        context_text,
        f"Pending questions / watch items:\n{questions_ctx or '(none)'}",
    ]
    if plugin_prompts:
        parts.append("\n".join(plugin_prompts))
    parts.append(base_instructions)
    return {"role": "system", "content": "\n\n".join(parts)}


# ---------------------------------------------------------------------------
# Plugin
# ---------------------------------------------------------------------------

class Plugin:
    def _spawn(self, coro):
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task

    async def _auto_title(self, vault_name, cid, user_msg, assistant_reply):
        """Generate and save a 2-5 word title via LLM. Never throws."""
        try:
            title = await self._llm.chat(
                messages=[{"role": "user", "content": (
                    f"Generate a concise 2-5 word title for this conversation. "
                    f"User: {user_msg}. Assistant: {assistant_reply}. "
                    f"Return ONLY the title, no quotes, no punctuation at the end."
                )}],
                caller="chat",
            )
            title = title.strip().strip('"').strip("'")[:100]
            if title:
                conn = self._db.get_db()
                _rename_conversation(conn, vault_name, cid, title)
        except Exception:
            import logging
            logging.getLogger(__name__).debug("Auto-title failed", exc_info=True)

    async def _prepare_request(self, body: ChatRequest) -> tuple:
        vn = self._ctx.vault_name
        conn = self._db.get_db()
        cid = body.conversation_id
        if cid is None:
            cid = _create_conversation(conn, vn)
        else:
            row = conn.execute(
                "SELECT 1 FROM chat_threads WHERE vault_name = ? AND id = ?",
                (vn, cid),
            ).fetchone()
            if not row:
                raise HTTPException(404, "Conversation not found")
        user_msg = None
        if body.messages and body.messages[-1].role == "user":
            user_msg = body.messages[-1].content
            _save_message(conn, vn, "user", user_msg, cid)
            stories_api = self._ctx.get_plugin_api("stories")
            if stories_api and hasattr(stories_api, 'consume_seeds'):
                await asyncio.to_thread(stories_api.consume_seeds, vn, user_msg)

        # Watch-fors and unresolved diary threads are provided by the memory
        # and diary plugins' registered context providers (Phase 6) and arrive
        # inside build_chat_context below — no separate questions block needed.
        questions_ctx = "(none)"
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d %A")
        context_text = self._ctx_builder.build_chat_context(
            user_msg or (body.messages[-1].content if body.messages else ""), cid, vn,
        )
        context_msg = _build_system_prompt(today, context_text, questions_ctx, BASE_INSTRUCTIONS, self._registry)
        full_msgs = [context_msg] + [m.model_dump() for m in body.messages]
        return conn, vn, cid, context_msg, full_msgs

    def _post_reply(self, ctx, conn, vn, cid, user_msg, reply) -> bool:
        _save_message(conn, vn, "assistant", reply, cid)
        memory_api = ctx.get_plugin_api("memory")
        if memory_api:
            self._spawn(
                asyncio.to_thread(
                    memory_api.extract_facts, vn, [{"role": "user", "content": user_msg or ""}, {"role": "assistant", "content": reply}]
                )
            )
        title_updated = False
        if user_msg:
            threads = _get_conversations(conn, vn, archived=False) + _get_conversations(conn, vn, archived=True)
            current = next((t for t in threads if t["id"] == cid), None)
            if current and current["title"] in ("New conversation", "New chat"):
                self._spawn(self._auto_title(vn, cid, user_msg, reply))
                title_updated = True
        return title_updated

    def on_load(self, ctx):
        self._ctx = ctx
        self._db = ctx.db_module
        self._vault = ctx.vault_manager
        self._ctx_builder = ctx.context_builder
        self._llm = ctx.llm_client
        self._tools = ctx.tool_executor
        self._registry = ctx.registry
        self._bg_tasks = set()

        # --- Migration v1: chat_threads + chat_messages ---
        ctx.register_migration(1, """
            CREATE TABLE IF NOT EXISTS chat_threads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vault_name TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT 'New conversation',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_chat_threads_vault ON chat_threads(vault_name, updated_at);
            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vault_name TEXT NOT NULL,
                thread_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (thread_id) REFERENCES chat_threads(id)
            );
            CREATE INDEX IF NOT EXISTS idx_chat_messages_thread ON chat_messages(vault_name, thread_id, created_at);
        """)

        # --- Migration v2: add is_archived column ---
        try:
            cols = {r[1] for r in ctx.db_module.get_db().execute("PRAGMA table_info(chat_threads)")}
            register_v2 = "is_archived" not in cols
        except Exception:
            register_v2 = True
        if register_v2:
            ctx.register_migration(2, "ALTER TABLE chat_threads ADD COLUMN is_archived INTEGER DEFAULT 0;")

        router = APIRouter()

        # --- POST /send ---
        @router.post("/send", response_model=ChatResponse)
        async def chat_send(body: ChatRequest):
            conn, vn, cid, context_msg, full_msgs = await self._prepare_request(body)
            try:
                reply = await self._llm.chat(
                    messages=full_msgs,
                    tools=self._tools.get_all_tools(),
                    system_prompt=body.system_prompt,
                    vault_manager=self._vault,
                    db_module=self._db,
                    caller="chat",
                )
            except Exception:
                logging.getLogger(__name__).warning("Chat with tools failed, retrying without", exc_info=True)
                reply = await self._llm.chat(
                    messages=full_msgs,
                    system_prompt=body.system_prompt,
                    caller="chat",
                )
            user_msg = body.messages[-1].content if body.messages and body.messages[-1].role == "user" else None
            title_updated = self._post_reply(ctx, conn, vn, cid, user_msg, reply)
            return ChatResponse(reply=reply, model=self._llm.LLM_MODEL, conversation_id=cid, title_updated=title_updated)

        # --- POST /stream ---
        @router.post("/stream")
        async def chat_stream(body: ChatRequest):
            conn, vn, cid, context_msg, full_msgs = await self._prepare_request(body)

            async def generate():
                client = self._llm.get_async_client()
                try:
                    stream = await client.chat.completions.create(
                        model=self._llm.LLM_MODEL,
                        messages=full_msgs,
                        tools=self._tools.get_all_tools(),
                        tool_choice="auto",
                        stream=True,
                    )
                except Exception:
                    logging.getLogger(__name__).warning("Stream with tools failed, retrying without", exc_info=True)
                    stream = await client.chat.completions.create(
                        model=self._llm.LLM_MODEL,
                        messages=full_msgs,
                        stream=True,
                    )

                tool_calls_acc = {}
                content_acc = ""
                async for chunk in stream:
                    delta = chunk.choices[0].delta
                    if delta.tool_calls:
                        for tc in delta.tool_calls:
                            idx = tc.index
                            if idx not in tool_calls_acc:
                                tool_calls_acc[idx] = {"id": tc.id or "", "name": tc.function.name or "", "args": ""}
                            if tc.function and tc.function.arguments:
                                tool_calls_acc[idx]["args"] += tc.function.arguments
                            if tc.id:
                                tool_calls_acc[idx]["id"] = tc.id
                            if tc.function and tc.function.name:
                                tool_calls_acc[idx]["name"] = tc.function.name

                        if any(tc.get("name") for tc in tool_calls_acc.values()):
                            names = [tc["name"] for tc in tool_calls_acc.values() if tc.get("name")]
                            yield f"data: {jsonmod.dumps({'type': 'tool_call', 'tools': names})}\n\n"

                    if delta.content:
                        content_acc += delta.content
                        yield f"data: {jsonmod.dumps({'type': 'text', 'content': delta.content})}\n\n"

                # After stream, process tool calls if any
                if tool_calls_acc:
                    assistant_tool_calls = []
                    tool_results = []
                    for idx, tc in sorted(tool_calls_acc.items()):
                        if tc["name"] and tc["args"]:
                            try:
                                result = await self._tools.execute_tool(tc["name"], tc["args"], self._vault, self._db)
                            except Exception as e:
                                result = f"Tool error: {e}"
                            tool_results.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
                            assistant_tool_calls.append({
                                "id": tc["id"],
                                "type": "function",
                                "function": {"name": tc["name"], "arguments": tc["args"]},
                            })
                            yield f"data: {jsonmod.dumps({'type': 'tool_result', 'tool': tc['name'], 'result': result})}\n\n"
                    if assistant_tool_calls:
                        full_msgs.append({"role": "assistant", "content": None, "tool_calls": assistant_tool_calls})
                        full_msgs.extend(tool_results)
                        resp2 = await client.chat.completions.create(
                            model=self._llm.LLM_MODEL,
                            messages=full_msgs,
                            stream=True,
                        )
                        async for chunk in resp2:
                            delta = chunk.choices[0].delta
                            if delta.content:
                                content_acc += delta.content
                                yield f"data: {jsonmod.dumps({'type': 'text', 'content': delta.content})}\n\n"

                user_msg = body.messages[-1].content if body.messages and body.messages[-1].role == "user" else None
                title_updated = self._post_reply(ctx, conn, vn, cid, user_msg, content_acc)
                yield f"data: {jsonmod.dumps({'type': 'done', 'conversation_id': cid, 'title_updated': title_updated})}\n\n"

            return StreamingResponse(generate(), media_type="text/event-stream")

        # --- POST /summarize ---
        @router.post("/summarize")
        async def summarize_conversation(conversation_id: int | None = None):
            vn = self._ctx.vault_name
            conn = self._db.get_db()
            if conversation_id is None:
                threads = _get_conversations(conn, vn)
                if not threads:
                    raise HTTPException(400, "No conversation to summarize")
                conversation_id = threads[0]["id"]
            messages = _get_messages(conn, vn, thread_id=conversation_id, limit=30, most_recent=True)
            if len(messages) < 4:
                raise HTTPException(400, "Not enough conversation to summarize")
            convo_text = "\n".join(f"{m['role']}: {m['content']}" for m in messages)
            summary = await self._llm.chat(
                messages=[{"role": "user", "content": f"Summarize this conversation in 2-3 bullet points. Be specific about key topics, decisions, and follow-ups mentioned.\n\n{convo_text}"}],
                system_prompt="Summarize concisely. Only return the bullet points.",
                caller="chat",
            )
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            self._vault.create_note(vn, f"Summary {now}", f"# Conversation Summary {now}\n\n{summary}", [], "note", subfolder="Notes")
            return {"detail": "Summary saved", "summary": summary}

        # --- Conversations CRUD ---

        @router.get("/conversations", response_model=list[ConversationThread])
        async def get_conversations(q: str | None = None, archived: bool = False):
            conn = self._db.get_db()
            return [ConversationThread(**m) for m in _get_conversations(conn, self._ctx.vault_name, archived=archived, query=q)]

        @router.post("/conversations", status_code=201, response_model=ConversationThread)
        async def create_conversation(body: ConversationCreate):
            vn = self._ctx.vault_name
            conn = self._db.get_db()
            cid = _create_conversation(conn, vn, body.title)
            threads = _get_conversations(conn, vn)
            for t in threads:
                if t["id"] == cid:
                    return ConversationThread(**t)
            raise HTTPException(500, "Failed to create conversation")

        @router.delete("/conversations")
        async def clear_conversations():
            conn = self._db.get_db()
            _clear_conversations(conn, self._ctx.vault_name)
            return {"detail": "Conversations cleared"}

        @router.get("/conversations/{conversation_id}", response_model=ConversationDetail)
        async def get_conversation(conversation_id: int):
            vn = self._ctx.vault_name
            conn = self._db.get_db()
            threads = _get_conversations(conn, vn, archived=False)
            archived_threads = _get_conversations(conn, vn, archived=True)
            all_threads = threads + archived_threads
            match = None
            for t in all_threads:
                if t["id"] == conversation_id:
                    match = t
                    break
            if not match:
                raise HTTPException(404, "Conversation not found")
            messages = [ConversationMessage(**m) for m in _get_messages(conn, vn, conversation_id)]
            return ConversationDetail(
                id=conversation_id,
                title=match["title"],
                messages=messages,
                created_at=match["created_at"],
                updated_at=match["updated_at"],
                is_archived=match["is_archived"],
            )

        @router.put("/conversations/{conversation_id}")
        async def rename_conversation(conversation_id: int, body: ConversationRename):
            vn = self._ctx.vault_name
            conn = self._db.get_db()
            if not _rename_conversation(conn, vn, conversation_id, body.title):
                raise HTTPException(404, "Conversation not found")
            return {"detail": "Conversation updated"}

        @router.delete("/conversations/{conversation_id}")
        async def delete_conversation(conversation_id: int):
            vn = self._ctx.vault_name
            conn = self._db.get_db()
            if not _delete_conversation(conn, vn, conversation_id):
                raise HTTPException(404, "Conversation not found")
            return {"detail": "Conversation deleted"}

        @router.post("/conversations/{conversation_id}/archive")
        async def archive_conversation(conversation_id: int):
            conn = self._db.get_db()
            if not _set_archived(conn, self._ctx.vault_name, conversation_id, True):
                raise HTTPException(404, "Conversation not found")
            return {"detail": "Archived"}

        @router.post("/conversations/{conversation_id}/unarchive")
        async def unarchive_conversation(conversation_id: int):
            conn = self._db.get_db()
            if not _set_archived(conn, self._ctx.vault_name, conversation_id, False):
                raise HTTPException(404, "Conversation not found")
            return {"detail": "Unarchived"}

        # --- Reprompt ---
        @router.put("/conversations/{conversation_id}/messages/{index}")
        async def reprompt_message(conversation_id: int, index: int, body: RepromptRequest):
            vn = self._ctx.vault_name
            conn = self._db.get_db()
            threads = _get_conversations(conn, vn, archived=False) + _get_conversations(conn, vn, archived=True)
            if not any(t["id"] == conversation_id for t in threads):
                raise HTTPException(404, "Conversation not found")

            _update_message(conn, vn, conversation_id, index, body.content)
            messages = [ConversationMessage(**m) for m in _get_messages(conn, vn, conversation_id)]

            today = datetime.now(timezone.utc).strftime("%Y-%m-%d %A")
            context_text = self._ctx_builder.build_chat_context(body.content, conversation_id, vn)
            context_msg = _build_system_prompt(today, context_text, "(none)", BASE_INSTRUCTIONS, self._registry)
            full_msgs = [context_msg] + [m.model_dump() for m in messages]

            reply = await self._llm.chat(
                messages=full_msgs,
                tools=self._tools.get_all_tools(),
                vault_manager=self._vault,
                db_module=self._db,
                caller="chat",
            )
            _save_message(conn, vn, "assistant", reply, conversation_id)
            return {"reply": reply}

        ctx.register_router(router)

        # --- Context providers (Phase 6) ---
        ctx.register_context_provider("chat", self._mentioned_entities_provider)

    def _mentioned_entities_provider(self, user_text, thread_id, vault_name):
        """Return mentioned entities from the conversation for chat context."""
        mentioned = set()
        if thread_id is not None:
            rows = self._db.get_db().execute(
                "SELECT role, content, created_at FROM chat_messages WHERE vault_name = ? AND thread_id = ? ORDER BY created_at ASC LIMIT 20",
                (vault_name, thread_id),
            ).fetchall()
            for m in rows:
                mentioned.update(_extract_mentioned_entities(m["content"]))
        mentioned.update(_extract_mentioned_entities(user_text or ""))
        if mentioned:
            return "## Mentioned in this conversation\n\n" + ", ".join(sorted(mentioned))
        return None
