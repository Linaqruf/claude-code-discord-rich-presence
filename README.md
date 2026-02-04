# cc-discord-rpc

Claude Code plugin that displays your coding activity as Discord Rich Presence.

## Features

- **Activity display**: Editing, Reading, Running command, Searching, etc.
- **Project info**: Name (from git remote or folder) + branch
- **Model display**: Opus 4.5, Sonnet 4, Haiku 4.5, etc.
- **Token tracking**: Cycling display showing simple vs cached tokens
- **Cost tracking**: Real-time API cost based on model pricing
- **Multi-session**: Supports multiple Claude Code terminals
- **Idle state**: Shows "Idling" after 5 min (keeps timer running)
- **Elapsed time**: Time since session start

## Display

```
┌─────────────────────────────────────────┐
│ Kana Code                               │
│ Editing on my-project (main)            │
│ Opus 4.5 • 22.9k tokens • $0.18         │
│ ⏱ 1:23:45                               │
└─────────────────────────────────────────┘
```

The status line cycles every 8 seconds:
- **5s**: Simple view - `Opus 4.5 • 22.9k tokens • $0.18` (input + output only)
- **3s**: Cached view - `Opus 4.5 • 54.3M cached • $41.99` (includes cache)

## Prerequisites

- Python 3.10+
- Discord desktop app running
- pypresence library

## Installation

1. Install pypresence:
   ```bash
   pip install pypresence
   ```

2. Copy this plugin to your Claude Code plugins directory:
   ```bash
   # Option 1: Global plugins (recommended)
   cp -r cc-discord-rpc ~/.claude/plugins/

   # Option 2: Project-level
   cp -r cc-discord-rpc /path/to/your/project/.claude-plugins/
   ```

3. Restart Claude Code

## Discord Setup

The plugin uses Discord Application ID `1330919293709324449`. To use your own:

1. Go to [Discord Developer Portal](https://discord.com/developers/applications)
2. Create a new application
3. Copy the Application ID
4. Edit `scripts/presence.py` and update `DISCORD_APP_ID`
5. Upload assets (optional): Add a "claude" image in Rich Presence > Art Assets

## How It Works

```
Claude Code Hooks → presence.py → pypresence → Discord RPC
```

| Hook | Trigger | Action |
|------|---------|--------|
| SessionStart | Claude Code opens | Start daemon, increment session count |
| PreToolUse | Before Edit/Bash/etc | Update activity and tokens |
| SessionEnd | Claude Code exits | Decrement count, stop if last session |

## Manual Control

```bash
# Check status
python scripts/presence.py status

# Output:
# Daemon running (PID 12345)
# Active sessions: 1
# Project: my-project
# Branch: main
# Model: Opus 4.5
# Tokens (simple): 22.9k (20k in / 2.9k out)
# Tokens (cached): 54.3M (+51M read / +3.3M write)
# Cost: $41.99 ($0.18 without cache)

# Stop all sessions
python scripts/presence.py stop
```

## Model Pricing

Costs are calculated using official Anthropic API pricing:

| Model | Input | Output | Cache Read | Cache Write |
|-------|-------|--------|------------|-------------|
| Opus 4.5 | $5/M | $25/M | $0.50/M | $6.25/M |
| Sonnet 4.5 | $3/M | $15/M | $0.30/M | $3.75/M |
| Sonnet 4 | $3/M | $15/M | $0.30/M | $3.75/M |
| Haiku 4.5 | $1/M | $5/M | $0.10/M | $1.25/M |
| Opus 4 | $15/M | $75/M | $1.50/M | $18.75/M |

## Data Files

Location: `%APPDATA%/cc-discord-rpc/` (Windows)

| File | Purpose |
|------|---------|
| `state.json` | Current session state |
| `daemon.pid` | Background process ID |
| `daemon.log` | Debug log |
| `refcount` | Active session count |

## Troubleshooting

**Presence not showing:**
- Make sure Discord desktop app is running
- Check if pypresence is installed: `pip show pypresence`
- Check logs: `%APPDATA%/cc-discord-rpc/daemon.log`

**"Could not connect" errors:**
- Discord must be running before Claude Code starts
- Try restarting Discord

**Wrong project name:**
- Project name comes from git remote origin URL
- Falls back to folder name if not a git repo

## License

MIT
