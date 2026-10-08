# claude-agent-cli

A small coding agent for the terminal, built from scratch on the [Claude API](https://docs.anthropic.com).
Ask questions about a project and Claude reads files, searches code, and (with your approval) runs commands to answer.

It's a tiny version of what tools like Claude Code do, written to understand how an agent loop actually works.

<!-- TODO: add a demo GIF here (record with ScreenToGif or asciinema) -->

## Features

- **Agent loop with tool use.** Claude decides which tools to call; the loop runs them and sends results back until Claude answers.
- **Three tools:** `read_file`, `search_files` (regex), and `run_command`, which always asks before running.
- **Streaming output**, including a summary of Claude's thinking.
- **Token and cost report** after every turn, plus `/cost` for the session total.
- **Prompt caching**, so each turn re-reads the conversation from cache at a fraction of the price.
- **Safe by default:** tools can't touch files outside the project folder, and tool inputs are validated before they run.

## Quick start

Requires [uv](https://docs.astral.sh/uv/) and an API key from [console.anthropic.com](https://console.anthropic.com).

```bash
git clone https://github.com/<you>/claude-agent-cli.git
cd claude-agent-cli
cp .env.example .env        # then put your key in .env
uv sync
uv run claude-agent --root path/to/some/project
```

**No Claude credits?** The agent also runs on free models through [OpenRouter](https://openrouter.ai), which offers an
Anthropic-compatible endpoint. Fill in Option B in `.env`. Claude-only features (thinking, effort, prompt caching,
refusal fallbacks) are switched off automatically for non-Claude models.

Options:

| Flag | Default | Meaning |
|---|---|---|
| `--model` | `AGENT_MODEL` in `.env`, else `claude-opus-5-5` | Model ID |
| `--effort` | `high` | `low` / `medium` / `high` / `xhigh` / `max`: how hard Claude thinks |
| `--root` | current folder | The folder the tools can access |

In the chat: `/cost` shows session usage, `/reset` starts a new conversation, `/exit` quits.

## How it works

```
you ──► messages ──► Claude ──► stop_reason?
                        ▲            │
                        │      "tool_use": run each tool locally,
                        │      send all results back in one message
                        └────────────┘
                             "end_turn": print the answer
```

- [`agent.py`](src/claude_agent_cli/agent.py): the loop. It streams each response, runs the requested tools, and handles stop reasons (`tool_use`, `end_turn`, `max_tokens`, `refusal`).
- [`tools.py`](src/claude_agent_cli/tools.py): tool schemas (what Claude sees) and implementations (what runs on your machine).
- [`cost.py`](src/claude_agent_cli/cost.py): token accounting and prices.
- [`cli.py`](src/claude_agent_cli/cli.py): the terminal chat loop and error messages.

Design decisions:

- **The history is append-only.** Earlier messages are never edited. That keeps the prompt cache valid and keeps Claude's earlier thinking usable. If a turn fails (refusal, Ctrl+C, API error), everything that turn added is removed as a unit.
- **Tool errors go back to Claude** as `is_error` results instead of crashing, so Claude can recover, for example by searching when a file isn't found.
- **Tool input is validated before it runs.** Tool inputs stream in as Claude writes them, so a truncated input can arrive; it's rejected, not executed.
- **Refusal fallback:** if a safety classifier declines a request, the API retries on a fallback model (`fallbacks: "default"`).

## Development

```bash
uv run pytest        # tests (the agent loop is tested against a fake client, no API calls)
uv run ruff check .  # lint
uv run mypy          # type check (strict)
```

CI runs all of these on Linux and Windows for every push and pull request.
