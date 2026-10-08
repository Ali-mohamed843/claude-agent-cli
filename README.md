# claude-agent-cli

A small coding agent for the terminal, built from scratch on the [Claude API](https://docs.anthropic.com).
Ask questions about a project and Claude reads files, searches code, and (with your approval) runs commands to answer.

Every session can be recorded and turned into an **agent receipt**: a single shareable web page that shows exactly
what the agent read, ran and answered, where you approved or declined, and what it cost.

It's a tiny version of what tools like Claude Code do, written to understand how an agent loop actually works.

<!-- TODO: add a demo GIF here (record with ScreenToGif or asciinema) -->

## Features

- **Agent loop with tool use.** Claude decides which tools to call; the loop runs them and sends results back until Claude answers.
- **Three tools:** `read_file`, `search_files` (regex), and `run_command`, which always asks before running.
- **Streaming output**, including a summary of Claude's thinking.
- **Token and cost report** after every turn, plus `/cost` for the session total.
- **Prompt caching**, so each turn re-reads the conversation from cache at a fraction of the price.
- **Safe by default:** tools can't touch files outside the project folder, and tool inputs are validated before they run.
- **Session recording:** every message, tool call, result, approval and cost is saved as it happens.
- **Agent receipts:** `receipts share` turns a recording into a redacted, self-contained HTML page you can send to anyone.
- **Resume:** closed the window by mistake? `claude-agent --resume` continues the same conversation.
- **Nothing to memorise:** type `/` for a menu of every command, `@` to pick a project file, `?` for help.

## Install

Requires [uv](https://docs.astral.sh/uv/) and an API key from [console.anthropic.com](https://console.anthropic.com)
(or a free [OpenRouter](https://openrouter.ai) key, see below).

```bash
git clone https://github.com/<you>/claude-agent-cli.git
cd claude-agent-cli
cp .env.example .env        # then put your key in .env
uv tool install --editable . --force
```

This installs two commands you can run from any folder: `claude-agent` and `receipts`.

- `--editable` means your code changes apply immediately, and the agent keeps reading its settings from this repo's `.env`.
- `--force` reinstalls over an existing install. **Run this command again after every `git pull`**, so new commands
  (like `receipts`) and new dependencies (like `prompt-toolkit` for the menus) get installed.

To try it without installing:

```bash
uv sync
uv run claude-agent --root path/to/some/project
```

**No Claude credits?** The agent also runs on free models through [OpenRouter](https://openrouter.ai), which offers an
Anthropic-compatible endpoint. Fill in Option B in `.env`. Claude-only features (thinking, effort, prompt caching,
refusal fallbacks) are switched off automatically for non-Claude models. Free models share a pool of requests, so you
may get rate limited (`429`): wait a minute, or switch model without leaving the chat: type `/model` and pick one.

## Using the chat

You don't need to remember any commands:

- **Type `/`** and a menu of every command opens, each with a short description. Keep typing to filter it,
  use ↑ ↓ to move, and Enter to pick. Commands that need a value (`/model`, `/effort`, `/resume`) then suggest
  the values, like your recorded sessions or the available models.
- **Type `@`** to pick a file from your project, e.g. `explain @src/app.py`. Claude reads the file you mention.
- **Type `?`** (or `/help`) for every command and keyboard shortcut.
- **The bar at the bottom** shows the key hints, whether the session is being recorded, the model, and what
  the session has cost so far.

What you see while Claude works:

```
❯ what does @src/app.py do?

✻ Thinking
  They want a summary of app.py. Read it first.

● Read src/app.py
  └ 42 lines
● Run uv run pytest -q
  └ Run this command? yes / no  yes
  └ exit 0 · 3 lines of output · 1.2 s

● src/app.py is the entry point: it loads settings from .env and starts the chat loop.

  ✓ 4.8 s · 12.4k tokens · $0.0421
```

Keyboard shortcuts:

| Key | What it does |
|---|---|
| `/` | Open the command menu |
| `@` | Mention a file in your project |
| `Enter` | Send · picks the highlighted menu item |
| `Alt+Enter` | New line (pasting several lines also works) |
| `↑` `↓` | Previous messages · move in a menu |
| `Ctrl+R` | Search your message history (kept in `~/.claude-agent/history`) |
| `Ctrl+C` | Stop Claude · clear what you typed |
| `Ctrl+L` | Clear the screen |
| `Ctrl+D` | Quit |

## Commands

### `claude-agent`: chat with the agent

| Command | What it does |
|---|---|
| `claude-agent` | Start a chat in the current folder (not recorded) |
| `claude-agent --record` | Start and record to `.agent-receipts/session-<time>.jsonl` |
| `claude-agent --record my-file.jsonl` | Record to a file you choose |
| `claude-agent --name "Anthropic"` | Name the session; this also turns recording on |
| `claude-agent --resume` | Continue the newest recorded session |
| `claude-agent --resume anthropic` | Continue the newest session whose name or first message contains "anthropic" |
| `claude-agent --resume path\to\file.jsonl` | Continue a specific recording |
| `claude-agent --resume anthropic --name "New title"` | Continue it and rename it |

**Options you can add to any of these:**

| Option | Default | What it does |
|---|---|---|
| `--model MODEL` | `AGENT_MODEL` in `.env`, else `claude-opus-5-5` | Use a different model, e.g. `nvidia/nemotron-3-super-120b-a12b:free` |
| `--effort LEVEL` | `high` | `low` / `medium` / `high` / `xhigh` / `max`: how hard Claude thinks (Claude models only) |
| `--root PATH` | current folder | Which folder the agent can read |

### Commands inside the chat

Type `/` to see these as a menu.

| Command | What it does |
|---|---|
| `/help` (or `?`) | Show all commands and keyboard shortcuts |
| `/share` | Turn this session into a receipt page and open it |
| `/record [title]` | Start recording this session (if you didn't start with `--record`) |
| `/name <title>` | Give this session a title (starts recording if it isn't already) |
| `/sessions` | List your recorded sessions |
| `/resume <session>` | Switch to a recorded session |
| `/model <model>` | Switch model, e.g. when a free model is rate limited |
| `/effort <level>` | How hard Claude thinks: `low`, `medium`, `high`, `xhigh`, `max` |
| `/cost` | Show tokens and cost so far |
| `/reset` | Start a new conversation (Claude forgets this one) |
| `/clear` | Clear the screen |
| `/exit` | Quit; you're shown where the session was saved and how to continue it |

### `receipts`: turn recordings into pages

| Command | What it does |
|---|---|
| `receipts list` | Show all recordings in this folder by name |
| `receipts share` | Make an HTML page from the newest recording and open it |
| `receipts share .agent-receipts\file.jsonl` | Make a page from a specific recording |
| `receipts share -o "$HOME\Desktop\receipt.html"` | Save the page somewhere else |
| `receipts share --no-open` | Make the page without opening the browser |

### A typical session

```bash
claude-agent --name "Anthropic research"    # 1. chat (recorded); type /share any time to see the receipt
claude-agent --resume anthropic             # 2. come back to it later
```

## Recording a session

`--record` (or `--name`) writes everything the agent does to a JSONL file, one event per line: your messages,
each model response (text, thinking summary, tool calls), every tool result with its duration, your
approve/decline answer for each command, and the token usage and cost of every turn. Failed and interrupted
turns are kept too. With no path, files go to `<root>/.agent-receipts/session-<time>-<name>.jsonl`. That folder
gets its own `.gitignore`, because recordings contain file contents and command output.

Each event is written to disk as it happens, so closing the window or a crash never loses what was already said.

## Sharing a receipt

`receipts share` turns the newest recording in `./.agent-receipts` (or a file you pass) into a single
self-contained HTML page and opens it in your browser.

- **A receipt-style card** sums up the session (files read, searches, commands approved and declined, tokens,
  duration and total cost) and links to each turn.
- **A step-by-step replay** shows every tool call with its output, exit code and your approval, and Claude's
  answers rendered as Markdown (headings, lists, tables, code).
- It works in light and dark mode and on phones, and prints cleanly.

**Safe to send.** Before rendering, it redacts API keys, tokens, `KEY=value` lines, private keys and e-mail
addresses, and replaces your project and home folders with `<project>` and `~`. The page has no scripts and loads
nothing from the network: everything is escaped, and answers go through a small Markdown renderer that never
passes raw HTML through. Redaction is best-effort, so skim the page before you send it.

To share it, send the `.html` file (WhatsApp, email, Discord): it opens in any browser on its own.

## Resuming a session

```bash
claude-agent --resume anthropic   # newest recording whose name (or first message) contains "anthropic"
claude-agent --resume             # the newest recording
```

The conversation is rebuilt from the recording and new events are appended to the same file, so the receipt
shows one session with a "Session resumed" marker. Only completed turns come back: a turn that failed (say, a
rate limit) or was cut off never got an answer, so it is left out and its message is shown so you can send it
again. Claude's earlier thinking is not restored (the API needs signatures the recording doesn't keep), and the
first request after resuming re-reads the whole conversation without the prompt cache, so it costs a bit more.

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
- [`cli.py`](src/claude_agent_cli/cli.py): the chat session: start-up flags, slash commands, recording, `--resume`, and error messages.
- [`ui.py`](src/claude_agent_cli/ui.py): the prompt, the `/` command menu, the `@` file picker, the status bar, the welcome card and help.
- [`commands.py`](src/claude_agent_cli/commands.py): the list of slash commands, shared by the menu, `/help` and the chat.
- [`streamtext.py`](src/claude_agent_cli/streamtext.py): wraps and styles Claude's text as it streams in.
- [`style.py`](src/claude_agent_cli/style.py): colors and symbols (off automatically when output isn't a terminal, or with `NO_COLOR`).
- [`recorder.py`](src/claude_agent_cli/recorder.py): writes the session to JSONL and finds recordings by name.
- [`resume.py`](src/claude_agent_cli/resume.py): rebuilds a conversation from a recording.
- [`receipt.py`](src/claude_agent_cli/receipt.py): the `receipts` command and redaction.
- [`receipt_page.py`](src/claude_agent_cli/receipt_page.py) and [`receipt_template.html`](src/claude_agent_cli/receipt_template.html): the receipt page.
- [`minimarkdown.py`](src/claude_agent_cli/minimarkdown.py): the safe Markdown renderer for Claude's answers.

Design decisions:

- **The history is append-only.** Earlier messages are never edited. That keeps the prompt cache valid and keeps Claude's earlier thinking usable. If a turn fails (refusal, Ctrl+C, API error), everything that turn added is removed as a unit.
- **Tool errors go back to Claude** as `is_error` results instead of crashing, so Claude can recover, for example by searching when a file isn't found.
- **Tool input is validated before it runs.** Tool inputs stream in as Claude writes them, so a truncated input can arrive; it's rejected, not executed.
- **Refusal fallback:** if a safety classifier declines a request, the API retries on a fallback model (`fallbacks: "default"`).
- **Recordings keep failed turns.** The conversation drops them, but the receipt shows what was tried, because an honest record includes what didn't work.

## Development

```bash
uv run pytest        # tests (the agent loop is tested against a fake client, no API calls)
uv run ruff check .  # lint
uv run mypy          # type check (strict)
```

CI runs all of these on Linux and Windows for every push and pull request.
