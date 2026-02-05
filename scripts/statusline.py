#!/usr/bin/env python3
"""
Claude Code Statusline with Discord RPC Integration

Displays a macOS Finder-style status bar showing model, tokens, cost, and git branch.
Also updates state.json to provide token/cost data to the Discord RPC daemon.

Setup in ~/.claude/settings.json:
{
  "statusLine": {
    "type": "command",
    "command": "python /path/to/cc-discord-rpc/scripts/statusline.py"
  }
}
"""
import json
import sys
import os
from pathlib import Path
from datetime import datetime

# Fix Windows console encoding for Unicode characters
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding='utf-8')

# ═══════════════════════════════════════════════════════════════
# Apple System Colors (ANSI approximations)
# ═══════════════════════════════════════════════════════════════

class C:
    """ANSI color codes - Apple system colors"""
    RESET = '\x1b[0m'
    BOLD = '\x1b[1m'
    DIM = '\x1b[2m'

    # Apple palette
    WHITE = '\x1b[97m'      # Primary text
    GRAY = '\x1b[90m'       # Secondary/dim
    BLUE = '\x1b[94m'       # System Blue - accent
    GREEN = '\x1b[92m'      # System Green - positive
    ORANGE = '\x1b[93m'     # System Orange - warning
    RED = '\x1b[91m'        # System Red - critical


# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════

def format_tokens(count: int) -> str:
    """Format token count (e.g., 29.4k, 1.2M)"""
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 100_000:
        return f"{count / 1_000:.0f}k"
    if count >= 1_000:
        return f"{count / 1_000:.1f}k"
    return f"{count:,}"


def format_cost(cost: float) -> str:
    """Format cost with appropriate precision"""
    if cost >= 100:
        return f"${cost:.0f}"
    if cost >= 10:
        return f"${cost:.1f}"
    if cost >= 0.01:
        return f"${cost:.2f}"
    return f"${cost:.3f}"


def create_progress_bar(percent: float, width: int = 10) -> str:
    """Create Apple-style progress bar with color coding"""
    filled = round((percent / 100) * width)
    empty = width - filled

    filled_char = '█'
    empty_char = '░'

    # Color based on usage (Apple system colors)
    if percent > 95:
        bar_color = C.RED
    elif percent > 80:
        bar_color = C.ORANGE
    else:
        bar_color = C.WHITE

    return f"{bar_color}{filled_char * filled}{C.GRAY}{empty_char * empty}{C.RESET}"


def get_git_branch(cwd: str) -> str | None:
    """Get current git branch from .git/HEAD"""
    try:
        git_head = Path(cwd) / '.git' / 'HEAD'
        if git_head.exists():
            head = git_head.read_text().strip()
            if head.startswith('ref: refs/heads/'):
                return head.replace('ref: refs/heads/', '')
    except (OSError, UnicodeDecodeError):
        # Git branch detection is optional, fail silently
        pass
    return None


def truncate(s: str, max_len: int) -> str:
    """Truncate string with ellipsis"""
    if len(s) <= max_len:
        return s
    return s[:max_len - 1] + '…'


# ═══════════════════════════════════════════════════════════════
# State Management (for Discord RPC integration)
# ═══════════════════════════════════════════════════════════════

if sys.platform == "win32":
    _appdata = os.environ.get("APPDATA")
    if _appdata:
        DATA_DIR = Path(_appdata) / "cc-discord-rpc"
    else:
        DATA_DIR = Path.home() / ".cc-discord-rpc"
else:
    DATA_DIR = Path.home() / ".local" / "share" / "cc-discord-rpc"

STATE_FILE = DATA_DIR / "state.json"


def read_state() -> dict:
    """Read current state from state file"""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
            print(f"[statusline] Warning: Could not read state file: {e}", file=sys.stderr)
    return {}


def write_state(state: dict):
    """Write state to state file using atomic write pattern"""
    import shutil
    import tempfile
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        content = json.dumps(state, indent=2)

        # Write to temp file first, then atomic rename
        fd, tmp_path = tempfile.mkstemp(dir=DATA_DIR, suffix='.tmp')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(content)
            # shutil.move handles cross-platform atomic rename (including Windows overwrite)
            shutil.move(tmp_path, STATE_FILE)
        except (OSError, IOError) as e:
            # Clean up temp file on failure
            print(f"[statusline] Failed to write state file: {e}", file=sys.stderr)
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except OSError as e:
        print(f"[statusline] Error writing state: {e}", file=sys.stderr)


# ═══════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════

def main():
    # Read JSON from stdin
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError, UnicodeDecodeError, OSError) as e:
        print(f"[statusline] Error reading input: {e}", file=sys.stderr)
        print("")
        return

    # Extract data
    model_info = data.get("model", {})
    model = model_info.get("display_name", "")
    model_id = model_info.get("id", "")

    cost_info = data.get("cost", {})
    cost = cost_info.get("total_cost_usd", 0.0)

    context = data.get("context_window", {})
    total_input = context.get("total_input_tokens", 0)
    total_output = context.get("total_output_tokens", 0)
    used_percent = context.get("used_percentage", 0.0)

    current_usage = context.get("current_usage") or {}
    cache_read = current_usage.get("cache_read_input_tokens", 0)
    cache_write = current_usage.get("cache_creation_input_tokens", 0)

    cwd = data.get("workspace", {}).get("current_dir", os.getcwd())
    git_branch = get_git_branch(cwd)

    # Update state.json for Discord RPC
    state = read_state()
    if state.get("session_start"):  # Only update if session exists
        state["model"] = model
        state["model_id"] = model_id
        state["tokens"] = {
            "input": total_input,
            "output": total_output,
            "cache_read": cache_read,
            "cache_write": cache_write,
            "cost": cost,
            "simple_cost": cost,  # Claude Code provides pre-calculated cost, no separate calculation needed
        }
        state["statusline_update"] = int(datetime.now().timestamp())
        write_state(state)

    # ─────────────────────────────────────────────────────────────
    # Build Apple Finder Path Bar Statusline
    # ─────────────────────────────────────────────────────────────

    parts = []
    chevron = f"{C.GRAY}  ›  {C.RESET}"

    # Model name (primary, blue accent)
    if model:
        parts.append(f"{C.BLUE}{C.BOLD}{model}{C.RESET}")

    # Progress bar with percentage
    progress_bar = create_progress_bar(used_percent)
    percent_str = round(used_percent)
    parts.append(f"{progress_bar} {C.WHITE}{percent_str}%{C.RESET}")

    # Token count
    total_tokens = total_input + total_output
    if total_tokens > 0:
        tokens_str = format_tokens(total_tokens)
        parts.append(f"{C.WHITE}{tokens_str} tokens{C.RESET}")

    # Cost (green for Apple "positive" feel)
    if cost > 0:
        cost_str = format_cost(cost)
        parts.append(f"{C.GREEN}{cost_str}{C.RESET}")

    # Git branch (subtle, at the end)
    if git_branch:
        branch_display = truncate(git_branch, 16)
        parts.append(f"{C.GRAY}{branch_display}{C.RESET}")

    # Join with chevron separators (Finder breadcrumb style)
    status_line = chevron.join(parts)

    print(status_line)


if __name__ == "__main__":
    main()
