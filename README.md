# tgclaude

tgclaude is a Telegram bot that bridges your self-hosted Claude Code installation
to your phone. It lets you list, resume, and start Claude sessions, send messages,
approve or deny tool-use permission prompts via inline keyboard buttons, and
monitor your Claude Max-plan usage — all without leaving Telegram. The bot runs
on your VPS as the same OS user that owns your Claude installation, communicates
with Telegram through outbound long-polling (no inbound ports required), and
stores only a tiny SQLite database of your own.

---

## Screenshots

> _Add a screenshot of the bot in action: save it as `docs/screenshot.png` and uncomment the line below._

<!-- ![tgclaude bot in action](docs/screenshot.png) -->

---

## Prerequisites

- A VPS (or any server) with **outbound internet access**. Tailscale-only hosts
  work fine — the bot never needs inbound HTTP.
- **Python 3.11+** installed on the VPS.
- The **`claude` CLI** installed and authenticated with a **Claude Max plan**.
  Run `claude` interactively at least once to generate `~/.claude/.credentials.json`.
- A **Telegram account** and a bot token from [@BotFather](https://t.me/BotFather).

> Tailscale is not a prerequisite of the bot itself. If your VPS has a public IP
> the setup is identical; Tailscale is only relevant if you want to keep inbound
> ports closed.

---

## Why the bot runs on the VPS

Telegram supports two ways to receive updates: webhooks (Telegram pushes to your
server) and long-polling (your bot pulls from Telegram). Webhooks require an
inbound HTTPS endpoint. Long-polling requires only an outbound HTTPS connection,
which works from any host that can reach `api.telegram.org` — including
Tailscale-only VPSes with no public inbound ports.

Running the bot on the same machine as your Claude installation also avoids any
tunnelling complexity: the bot calls the claude-agent-sdk in-process and reads
`~/.claude` directly, with no network hops between the bot and Claude.

---

## Setup

### 1. Create a Telegram bot

1. Open Telegram and start a chat with [@BotFather](https://t.me/BotFather).
2. Send `/newbot` and follow the prompts.
3. Copy the bot token (format: `123456789:AABBCCDDEEFFaabbccddeeff…`).

### 2. Find your Telegram user ID

Send `/start` to [@userinfobot](https://t.me/userinfobot). Copy the numeric ID.

### 3. Clone and install

```bash
git clone https://github.com/yourusername/telegram-claude-chatbot.git
cd telegram-claude-chatbot/tgclaude
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

### 4. Configure

```bash
cp .env.example .env
$EDITOR .env    # fill in BOT_TOKEN and ALLOWED_USER_IDS at minimum
```

### 5. First run (test in foreground)

```bash
source .venv/bin/activate
tgclaude
```

Send `/start` to your bot in Telegram. You should see a session picker.
Press Ctrl-C to stop when satisfied.

### 6. Install as a systemd service

The unit file in `systemd/tgclaude.service` is a template unit (uses `%i` /
`%h` for the username). Copy and enable it:

```bash
# Copy the .env to the location the unit expects
mkdir -p ~/.config/tgclaude
cp .env ~/.config/tgclaude/.env

# Install the unit (replace 'alice' with your VPS username)
sudo cp systemd/tgclaude.service /etc/systemd/system/tgclaude@.service
sudo systemctl daemon-reload
sudo systemctl enable --now tgclaude@alice.service

# Check the logs
journalctl -u tgclaude@alice.service -f
```

---

## Configuration reference

All configuration comes from environment variables. The `.env` file is loaded
automatically on startup by python-dotenv. All path values support `~` (tilde
expansion is applied at load time).

| Variable | Default | Purpose |
|---|---|---|
| `BOT_TOKEN` | *(required)* | Telegram bot token from @BotFather |
| `ALLOWED_USER_IDS` | *(required, non-empty)* | Comma-separated list of numeric Telegram user IDs allowed to use this bot. An empty or missing value causes a hard startup failure — the bot refuses to run without an explicit allow-list. |
| `CLAUDE_HOME` | `~/.claude` | Root of the Claude install (contains `.credentials.json` and `projects/`) |
| `CLAUDE_PROJECT_CWD` | `~` | Working directory passed to every Claude query. Sessions appear under `$CLAUDE_HOME/projects/<encoded-cwd>/`. |
| `DATABASE_PATH` | `~/.local/state/tgclaude/bot.db` | SQLite file. Parent directory is created with `0700` permissions on first run. |
| `ALERT_THRESHOLDS` | `50,80,95` | Comma-separated utilization percentages at which to send alerts. First-run seed; overridden at runtime by `/alerts thresholds N,N,N`. |
| `PERMISSION_MODE` | `interactive` | How tool-use permissions are handled. See the Permission mode section below. |
| `DISPLAY_TZ` | *(unset — uses system TZ)* | IANA timezone name (e.g. `America/Phoenix`, `Europe/Berlin`) for rendering reset times in `/usage` output. |
| `PERMISSION_TIMEOUT_S` | `600` | How long the bot waits for a permission tap before auto-denying (seconds). |
| `LOG_LEVEL` | `INFO` | Standard Python logging level: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`. |

---

## Permission mode explainer

Claude Code can use tools (file reads, writes, shell commands, web fetches) on
your behalf. tgclaude gives you three ways to control this:

### `interactive` (default, recommended)

Every tool call triggers a Telegram inline keyboard with four buttons:

- **Yes** — allow this single call
- **Always allow** — add this tool to the session's allow-list (no more prompts
  for this tool in this session, persisted to SQLite)
- **No** — deny silently
- **No, and say why** — type a reason; it is forwarded to Claude so it can
  take a different approach

The SDK call blocks until you tap. The default 10-minute timeout auto-denies
if you do not respond.

**Tradeoff:** requires your attention for every novel tool call. Best when
you want full visibility into what Claude does on your VPS.

### `bypass`

The SDK runs with `bypassPermissions`. All tools auto-approve without any
prompts. Tool-use announcements are still sent to Telegram (for awareness)
but carry no buttons.

**Tradeoff:** maximum autonomy, zero friction. Anyone who compromises your
allow-listed Telegram account has full command execution on the VPS. Only use
this if you trust the Claude sessions you run and accept the risk.

### `readonly`

`Read`, `Grep`, `Glob`, and `WebFetch` are auto-allowed. All other tools are
auto-denied with the message "this bot is in readonly mode — tool rejected."
No prompts are sent.

**Tradeoff:** safe for browsing and Q&A workloads. Claude cannot write files
or execute shell commands. The permission allow-list table stays empty.

**Recommendation:** start with `interactive`. Switch to `bypass` for long
autonomous tasks where you will monitor Telegram passively and switch back
when done.

---

## /usage command

`/usage` displays your current Claude Max-plan utilization across three buckets:

- **Current session** (five-hour rolling window)
- **Current week (all models)**
- **Current week (Sonnet only)**

Each bucket shows a progress bar, percentage, and time until the next reset.

The bot also sends proactive alerts when utilization crosses the configured
thresholds. Manage alerts with:

```
/alerts            — show current status
/alerts on         — enable alerts
/alerts off        — disable alerts
/alerts thresholds 50,80,95  — set thresholds
/alerts reset      — restore env-var defaults
```

> **Disclaimer:** `/usage` fetches data from `/api/oauth/usage`, the same
> undocumented endpoint the `claude` CLI itself uses internally. This endpoint
> is not part of any public Anthropic API and may change or disappear without
> notice. tgclaude is not affiliated with Anthropic; use at your own risk.

---

## Troubleshooting

### "Auth expired — SSH in and run `claude` once to refresh"

The OAuth token in `~/.claude/.credentials.json` has expired. Open a terminal
on the VPS, run `claude` interactively (any prompt), complete the re-auth flow,
then retry your Telegram message. The bot reads the token from disk on each
request, so no restart is needed after re-auth.

### The bot is completely silent

1. Check the service is running: `systemctl status tgclaude@<user>.service`
2. Check logs: `journalctl -u tgclaude@<user>.service -n 50`
3. Verify your user ID is in `ALLOWED_USER_IDS`. Unknown users are silently
   dropped with no error message (security by design).
4. Confirm the bot token is correct (`BOT_TOKEN` in `.env`).

### No sessions appear in the picker

The session picker shows sessions under `$CLAUDE_HOME/projects/<encoded-cwd>/`.
If you started Claude sessions from a different working directory (e.g. inside
a project repository), they live in a different encoded directory and will not
appear here. Either set `CLAUDE_PROJECT_CWD` to that directory, or start new
sessions through the bot.

### Tool calls are auto-denied immediately

Check `PERMISSION_MODE` in your `.env`. If it is `readonly`, non-safe tools
are always denied. Switch to `interactive` or `bypass` as needed.

### The bot receives messages but Claude never replies

Ensure `claude` is installed and accessible in the PATH of the user running
tgclaude. Run `which claude` as that user to verify. Also check that
`~/.claude/.credentials.json` exists and is readable.

### Permission prompt timed out

The default timeout is 10 minutes (`PERMISSION_TIMEOUT_S=600`). If you
frequently miss prompts, increase this value in `.env` or switch to `bypass`
mode for trusted workloads.

---

## Contributing

- Follow the existing code style: Python 3.11+ type hints, `from __future__ import annotations`, `logging.getLogger(__name__)` in every module, no `print` statements.
- Clean Code principles apply: small single-responsibility functions, no magic numbers, intention-revealing names.
- Shared mutable state lives in `context.bot_data` where possible. The module-level dicts `pending_permissions`, `waiting_for_reason`, `detach_after_turn`, and `reattach_after_turn` are documented exceptions required by the cross-handler coordination design.
- Add or update tests in `tests/` for any changed behaviour. Run `pytest` before submitting.
- Open an issue before starting large changes so the approach can be discussed.
- PRs must pass all tests and linting on Python 3.11 and 3.12.

---

## Disclaimer

tgclaude is an independent open-source project and is **not affiliated with,
endorsed by, or supported by Anthropic**. Claude, Claude Code, and Claude Max
are trademarks of Anthropic, PBC.

The `/usage` command uses the undocumented `/api/oauth/usage` endpoint that the
`claude` CLI uses internally. It may change or be removed by Anthropic without
notice. Users are responsible for their own Max-plan usage, the conduct of their
Telegram bot, and the security of their VPS.

---

## License

MIT. See [LICENSE](LICENSE) for the full text.
