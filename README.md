# OpenCode Lens

A mobile-friendly web wrapper for the OpenCode HTTP server.

## What it does

- Proxies the full OpenCode HTTP API under `/api/opencode/*`
- Streams the OpenCode event bus into the browser
- Shows sessions, messages, files, commands, agents, MCP, LSP, and tools
- Adds a local Ollama model picker that generates an OpenCode config snippet

## Defaults

- Wrapper UI: `http://<host-ip>:8088`
- OpenCode server: `http://127.0.0.1:4096`
- Ollama server: `http://127.0.0.1:11434`

Override with:

- `OPENCODE_SERVER_URL`
- `OPENCODE_SERVER_USERNAME`
- `OPENCODE_SERVER_PASSWORD`
- `OLLAMA_BASE_URL`
- `OPENCODE_MODEL`
- `OPENCODE_PROXY_TIMEOUT`
- `HOST`
- `PORT`

## Run

```bash
./run.sh
```

Then open the UI from your TailScale-reachable host address, for example:

```text
http://10.x.x.x:8088
```

## OpenCode docs used for the wrapper

OpenCode’s server exposes these endpoints, among others:

- `/app`
- `/config`
- `/config/providers`
- `/session`
- `/session/:id`
- `/session/:id/message`
- `/session/:id/shell`
- `/session/:id/abort`
- `/session/:id/share`
- `/session/:id/summarize`
- `/file/status`
- `/find`
- `/find/file`
- `/find/symbol`
- `/agent`
- `/command`
- `/lsp`
- `/formatter`
- `/mcp`
- `/experimental/tool/ids`
- `/event`
- `/tui/*`

The OpenCode docs also note that the server’s OpenAPI spec is available at `/doc`.

## Ollama model selection

The UI lists local Ollama models from the Ollama API and converts them into OpenCode model refs like:

- `ollama/llama3.1`
- `ollama/qwen2.5-coder`

OpenCode’s model docs say the default model format is `provider_id/model_id`, and its provider docs support custom provider base URLs. For local Ollama, this wrapper generates a config snippet pointing at `http://127.0.0.1:11434/v1`.
