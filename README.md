# anotherme-plugin-chat

Chat with LLM — conversations, streaming, tool calling, memory extraction —
for [AnotherMe](https://github.com/MatousVavra/AnotherMe).

Extracted from the AnotherMe host repository at commit 9a6ef26 — prior
history lives there.

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `LLM_MODEL` | `deepseek-v4-pro-thinking` | Chat model name (read by the host LLM client via `ctx.llm_client`) |

The model is also configurable per-install via the plugin settings UI.

## Development

Unit tests run standalone against `FakePluginContext`:

    pip install fastapi pydantic httpx pyyaml pytest pytest-asyncio openai
    pytest tests/ --ignore=tests/integration

Integration tests run inside the released app image (see
`.github/workflows/test.yml`). To develop against a live app, point
`COMMUNITY_PLUGINS_DIR` at this checkout's parent directory.

## Releases

Tag `vX.Y.Z` (must match `plugin/plugin.yaml` `version`), then bump the tag
in the [community index](https://github.com/MatousVavra/anotherme-plugins).
