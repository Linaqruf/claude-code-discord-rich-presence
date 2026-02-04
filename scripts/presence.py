#!/usr/bin/env python3
"""
Discord Rich Presence for Claude Code
Manages Discord RPC connection and updates presence based on Claude Code activity.
"""

import sys
import os
import json
import time
import atexit
import signal
from pathlib import Path
from datetime import datetime

# Discord Application ID
DISCORD_APP_ID = "1330919293709324449"

# Data directory
if sys.platform == "win32":
    DATA_DIR = Path(os.environ.get("APPDATA", "")) / "cc-discord-rpc"
else:
    DATA_DIR = Path.home() / ".local" / "share" / "cc-discord-rpc"

STATE_FILE = DATA_DIR / "state.json"
PID_FILE = DATA_DIR / "daemon.pid"
LOG_FILE = DATA_DIR / "daemon.log"
REFCOUNT_FILE = DATA_DIR / "refcount"

# Claude Code directories
CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"

# Model display names
MODEL_DISPLAY = {
    # Claude 4.5 series
    "claude-opus-4-5-20251101": "Opus 4.5",
    "claude-sonnet-4-5-20250514": "Sonnet 4.5",
    "claude-sonnet-4-5-20241022": "Sonnet 4.5",
    "claude-haiku-4-5-20250414": "Haiku 4.5",
    "claude-haiku-4-5-20241022": "Haiku 4.5",
    # Claude 4 series
    "claude-opus-4-20250514": "Opus 4",
    "claude-sonnet-4-20250514": "Sonnet 4",
}

# Model pricing per 1M tokens (input, output, cache_read)
# Cache writes charged at 1.25x input rate, cache reads at 0.1x input rate
MODEL_PRICING = {
    # Claude 4.5 series
    "claude-opus-4-5-20251101": (5.00, 25.00, 0.50),
    "claude-sonnet-4-5-20250514": (3.00, 15.00, 0.30),
    "claude-haiku-4-5-20250414": (1.00, 5.00, 0.10),
    # Claude 4 series
    "claude-opus-4-20250514": (15.00, 75.00, 1.50),
    "claude-sonnet-4-20250514": (3.00, 15.00, 0.30),
    # Legacy/fallback
    "claude-sonnet-4-5-20241022": (3.00, 15.00, 0.30),
    "claude-haiku-4-5-20241022": (1.00, 5.00, 0.10),
}

# Tool to display name mapping
TOOL_DISPLAY = {
    "Edit": "Editing",
    "Write": "Writing",
    "Read": "Reading",
    "Bash": "Running command",
    "Glob": "Searching files",
    "Grep": "Searching code",
    "Task": "Delegating task",
    "WebFetch": "Fetching web content",
    "WebSearch": "Researching",
}

# Idle timeout in seconds (15 minutes)
IDLE_TIMEOUT = 15 * 60


def log(message: str):
    """Append message to log file."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{timestamp}] {message}\n")


def read_state() -> dict:
    """Read current state from state file."""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, IOError):
            pass
    return {}


def write_state(state: dict):
    """Write state to state file."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def get_daemon_pid() -> int | None:
    """Get PID of running daemon, or None if not running."""
    if not PID_FILE.exists():
        return None
    try:
        pid = int(PID_FILE.read_text().strip())
        # Check if process is actually running
        if sys.platform == "win32":
            import subprocess
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True
            )
            if str(pid) in result.stdout:
                return pid
        else:
            os.kill(pid, 0)  # Doesn't kill, just checks
            return pid
    except (ValueError, ProcessLookupError, PermissionError, IOError):
        pass
    return None


def write_pid():
    """Write current PID to file."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))


def remove_pid():
    """Remove PID file."""
    try:
        PID_FILE.unlink()
    except IOError:
        pass


def get_project_name() -> str:
    """Get project name from current working directory."""
    cwd = os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())
    return Path(cwd).name


def get_git_branch(project_path: str) -> str:
    """Get current git branch name."""
    import subprocess
    if not project_path:
        return ""
    try:
        result = subprocess.run(
            ["git", "-C", project_path, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return ""


def read_refcount() -> int:
    """Read current session refcount."""
    if REFCOUNT_FILE.exists():
        try:
            return int(REFCOUNT_FILE.read_text().strip())
        except (ValueError, IOError):
            pass
    return 0


def write_refcount(count: int):
    """Write session refcount."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if count <= 0:
        try:
            REFCOUNT_FILE.unlink()
        except IOError:
            pass
    else:
        REFCOUNT_FILE.write_text(str(count))


def get_model_from_jsonl() -> str:
    """Get model name from most recent JSONL file."""
    if not PROJECTS_DIR.exists():
        return ""

    # Find most recent .jsonl file
    jsonl_files = []
    for path in PROJECTS_DIR.rglob("*.jsonl"):
        try:
            jsonl_files.append((path, path.stat().st_mtime))
        except IOError:
            continue

    if not jsonl_files:
        return ""

    # Sort by modification time, get most recent
    jsonl_files.sort(key=lambda x: x[1], reverse=True)
    recent_file = jsonl_files[0][0]

    # Parse last assistant message with model
    last_model = ""
    try:
        with open(recent_file, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    msg = json.loads(line)
                    if msg.get("type") == "assistant":
                        model = msg.get("message", {}).get("model", "")
                        if model:
                            last_model = model
                except json.JSONDecodeError:
                    continue
    except IOError:
        pass

    return format_model_name(last_model)


def format_model_name(model_id: str) -> str:
    """Convert model ID to display name."""
    if model_id in MODEL_DISPLAY:
        return MODEL_DISPLAY[model_id]
    if "opus" in model_id.lower():
        return "Opus"
    if "sonnet" in model_id.lower():
        return "Sonnet"
    if "haiku" in model_id.lower():
        return "Haiku"
    return ""


def get_session_tokens_and_cost(session_id: str = "") -> dict:
    """Get total tokens and cost from current session's JSONL file.

    Returns: dict with input, output, cache_read, cache_write, cost
    """
    empty_result = {
        "input": 0,
        "output": 0,
        "cache_read": 0,
        "cache_write": 0,
        "cost": 0.0,
    }
    if not PROJECTS_DIR.exists():
        return empty_result

    # Find JSONL file for current session or most recent
    jsonl_file = None
    if session_id:
        # Try to find file matching session ID
        for path in PROJECTS_DIR.rglob(f"{session_id}.jsonl"):
            jsonl_file = path
            break

    if not jsonl_file:
        # Fall back to most recent JSONL file
        jsonl_files = []
        for path in PROJECTS_DIR.rglob("*.jsonl"):
            try:
                jsonl_files.append((path, path.stat().st_mtime))
            except IOError:
                continue

        if not jsonl_files:
            return empty_result

        jsonl_files.sort(key=lambda x: x[1], reverse=True)
        jsonl_file = jsonl_files[0][0]

    # Parse all assistant messages and sum tokens
    total_input = 0
    total_output = 0
    total_cache_read = 0
    total_cache_write = 0
    last_model = ""

    try:
        with open(jsonl_file, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    msg = json.loads(line)
                    if msg.get("type") == "assistant":
                        message = msg.get("message", {})
                        model = message.get("model", "")
                        if model:
                            last_model = model

                        usage = message.get("usage", {})
                        total_input += usage.get("input_tokens", 0)
                        total_output += usage.get("output_tokens", 0)
                        total_cache_read += usage.get("cache_read_input_tokens", 0)
                        total_cache_write += usage.get("cache_creation_input_tokens", 0)
                except json.JSONDecodeError:
                    continue
    except IOError:
        return empty_result

    # Calculate cost
    cost = 0.0
    if last_model in MODEL_PRICING:
        input_price, output_price, cache_read_price = MODEL_PRICING[last_model]
        # Input + cache writes at input rate
        cost += (total_input + total_cache_write) * input_price / 1_000_000
        # Output at output rate
        cost += total_output * output_price / 1_000_000
        # Cache reads at reduced rate
        cost += total_cache_read * cache_read_price / 1_000_000

    # Calculate simple cost (without cache benefits - what it would cost without caching)
    simple_cost = 0.0
    if last_model in MODEL_PRICING:
        input_price, output_price, _ = MODEL_PRICING[last_model]
        simple_cost = total_input * input_price / 1_000_000 + total_output * output_price / 1_000_000

    return {
        "input": total_input,
        "output": total_output,
        "cache_read": total_cache_read,
        "cache_write": total_cache_write,
        "cost": cost,
        "simple_cost": simple_cost,
    }


def format_tokens(count: int) -> str:
    """Format token count for display (e.g., 12.5k, 1.2M)."""
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    elif count >= 1000:
        return f"{count / 1000:.1f}k"
    return str(count)


def read_hook_input() -> dict:
    """Read JSON input from stdin (provided by Claude Code hooks)."""
    try:
        if not sys.stdin.isatty():
            data = sys.stdin.read()
            if data.strip():
                return json.loads(data)
    except (json.JSONDecodeError, IOError):
        pass
    return {}


def run_daemon():
    """Run the Discord RPC daemon loop."""
    from pypresence import Presence

    log("Daemon starting...")
    write_pid()
    atexit.register(remove_pid)

    # Handle graceful shutdown
    def shutdown(signum, frame):
        log("Received shutdown signal")
        remove_pid()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    # Connect to Discord
    rpc = None
    connected = False
    last_sent = {}  # Track last sent state to avoid redundant updates

    while True:
        try:
            # Try to connect if not connected
            if not connected:
                try:
                    rpc = Presence(DISCORD_APP_ID)
                    rpc.connect()
                    connected = True
                    log("Connected to Discord")
                except Exception as e:
                    log(f"Failed to connect to Discord: {e}")
                    time.sleep(5)
                    continue

            # Read current state
            state = read_state()

            if not state:
                time.sleep(1)
                continue

            # Check for idle timeout
            last_update = state.get("last_update", 0)
            if time.time() - last_update > IDLE_TIMEOUT:
                log("Idle timeout reached, clearing presence")
                if rpc:
                    try:
                        rpc.clear()
                    except:
                        pass
                write_state({})
                last_sent = {}
                time.sleep(5)
                continue

            # Update presence
            tool = state.get("tool", "")
            project = state.get("project", "Claude Code")
            git_branch = state.get("git_branch", "")
            model = state.get("model", "")
            session_start = state.get("session_start", int(time.time()))

            # Get token data
            tokens = state.get("tokens", {})
            input_tokens = tokens.get("input", 0)
            output_tokens = tokens.get("output", 0)
            cache_read = tokens.get("cache_read", 0)
            cache_write = tokens.get("cache_write", 0)
            cost = tokens.get("cost", 0.0)
            simple_cost = tokens.get("simple_cost", 0.0)

            activity = TOOL_DISPLAY.get(tool, "Working")

            # Build details line: "Activity project (branch)"
            if git_branch:
                details = f"{activity} {project} ({git_branch})"
            else:
                details = f"{activity} {project}"

            # Cycle state line every 8s: 5s simple, 3s cached
            cycle_pos = int(time.time()) % 8
            show_simple = cycle_pos < 5  # 0-4 = simple (5s), 5-7 = cached (3s)

            # Simple = input + output only
            simple_tokens = input_tokens + output_tokens
            # Cached = total including cache
            cached_tokens = input_tokens + output_tokens + cache_read + cache_write

            if show_simple:
                # Simple view: input/output tokens only
                tokens_display = format_tokens(simple_tokens) if simple_tokens > 0 else "0"
                cost_display = f"${simple_cost:.2f}" if simple_cost > 0 else "$0.00"
                state_line = f"{model} • {tokens_display} tokens • {cost_display}" if model else f"{tokens_display} tokens • {cost_display}"
            else:
                # Cached view: total with cache
                tokens_display = format_tokens(cached_tokens) if cached_tokens > 0 else "0"
                cost_display = f"${cost:.2f}" if cost > 0 else "$0.00"
                state_line = f"{model} • {tokens_display} cached • {cost_display}" if model else f"{tokens_display} cached • {cost_display}"

            # Only update if something changed (check every cycle)
            current = {"details": details, "state_line": state_line}
            if current != last_sent:
                log(f"Sending to Discord: {details} | {state_line}")
                try:
                    rpc.update(
                        details=details,
                        state=state_line,
                        start=session_start,
                        large_image="claude",
                        large_text="Claude Code",
                    )
                    last_sent = current
                except Exception as e:
                    log(f"Failed to update presence: {e}")
                    connected = False
                    rpc = None

            time.sleep(1)

        except KeyboardInterrupt:
            break
        except Exception as e:
            log(f"Daemon error: {e}")
            time.sleep(5)

    # Cleanup
    if rpc:
        try:
            rpc.clear()
            rpc.close()
        except:
            pass
    log("Daemon stopped")


def cmd_start():
    """Handle 'start' command - spawn daemon if needed, update state."""
    hook_input = read_hook_input()
    project = hook_input.get("cwd", os.environ.get("CLAUDE_PROJECT_DIR", ""))
    project_name = Path(project).name if project else get_project_name()

    # Increment refcount
    refcount = read_refcount() + 1
    write_refcount(refcount)

    # Update state
    state = read_state()
    now = int(time.time())

    if not state.get("session_start"):
        state["session_start"] = now

    state["project"] = project_name
    state["project_path"] = project
    state["git_branch"] = get_git_branch(project) if project else ""
    state["model"] = get_model_from_jsonl()
    state["last_update"] = now
    state["tool"] = ""

    # Get session ID from hook input if available
    session_id = hook_input.get("session_id", "")
    state["session_id"] = session_id

    # Initialize token tracking
    tokens = get_session_tokens_and_cost(session_id)
    state["tokens"] = tokens

    write_state(state)

    log(f"Session started (active sessions: {refcount})")

    # Check if daemon is running
    if get_daemon_pid():
        log("Daemon already running")
        return

    # Spawn daemon in background
    log(f"Starting daemon for project: {project_name}")

    if sys.platform == "win32":
        import subprocess
        # Use pythonw if available for windowless execution
        python_exe = sys.executable
        script_path = Path(__file__).resolve()

        subprocess.Popen(
            [python_exe, str(script_path), "daemon"],
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        # Unix: fork and detach
        pid = os.fork()
        if pid == 0:
            # Child process
            os.setsid()
            sys.stdin.close()
            sys.stdout.close()
            sys.stderr.close()
            run_daemon()
            sys.exit(0)


def cmd_update():
    """Handle 'update' command - update current activity."""
    hook_input = read_hook_input()
    tool_name = hook_input.get("tool_name", "")

    state = read_state()
    if not state:
        # No active session, ignore
        return

    state["tool"] = tool_name
    state["last_update"] = int(time.time())

    # Refresh token counts
    session_id = state.get("session_id", "")
    tokens = get_session_tokens_and_cost(session_id)
    state["tokens"] = tokens

    write_state(state)

    log(f"Updated activity: {tool_name}")


def cmd_stop():
    """Handle 'stop' command - clear presence and stop daemon."""
    # Decrement refcount
    refcount = read_refcount() - 1
    write_refcount(refcount)

    if refcount > 0:
        log(f"Session ended (active sessions: {refcount})")
        return  # Don't stop daemon, other sessions still active

    log("Last session ended, stopping daemon")

    # Clear state
    write_state({})

    # Kill daemon if running
    pid = get_daemon_pid()
    if pid:
        try:
            if sys.platform == "win32":
                import subprocess
                subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                             capture_output=True)
            else:
                os.kill(pid, signal.SIGTERM)
            log(f"Stopped daemon (PID {pid})")
        except Exception as e:
            log(f"Failed to stop daemon: {e}")

    remove_pid()


def cmd_status():
    """Handle 'status' command - show current status."""
    pid = get_daemon_pid()
    state = read_state()
    refcount = read_refcount()

    if pid:
        print(f"Daemon running (PID {pid})")
    else:
        print("Daemon not running")

    print(f"Active sessions: {refcount}")

    if state:
        print(f"Project: {state.get('project', 'Unknown')}")
        git_branch = state.get('git_branch', '')
        if git_branch:
            print(f"Branch: {git_branch}")
        model = state.get('model', '')
        if model:
            print(f"Model: {model}")
        print(f"Last tool: {state.get('tool', 'None')}")

        # Show token stats
        tokens = state.get('tokens', {})
        input_t = tokens.get('input', 0)
        output_t = tokens.get('output', 0)
        cache_read = tokens.get('cache_read', 0)
        cache_write = tokens.get('cache_write', 0)
        cost = tokens.get('cost', 0.0)
        simple_cost = tokens.get('simple_cost', 0.0)

        if input_t or output_t or cache_read:
            simple = input_t + output_t
            cached = simple + cache_read + cache_write
            print(f"Tokens (simple): {format_tokens(simple)} ({format_tokens(input_t)} in / {format_tokens(output_t)} out)")
            print(f"Tokens (cached): {format_tokens(cached)} (+{format_tokens(cache_read)} read / +{format_tokens(cache_write)} write)")
            print(f"Cost: ${cost:.2f} (${simple_cost:.2f} without cache)")

        last_update = state.get("last_update", 0)
        if last_update:
            ago = int(time.time() - last_update)
            print(f"Last update: {ago}s ago")
    else:
        print("No active session")


def main():
    if len(sys.argv) < 2:
        print("Usage: presence.py <start|update|stop|status|daemon>")
        sys.exit(1)

    command = sys.argv[1]

    if command == "start":
        cmd_start()
    elif command == "update":
        cmd_update()
    elif command == "stop":
        cmd_stop()
    elif command == "status":
        cmd_status()
    elif command == "daemon":
        run_daemon()
    else:
        print(f"Unknown command: {command}")
        sys.exit(1)


if __name__ == "__main__":
    main()
