#!/usr/bin/env python3
"""
Discord Rich Presence for Claude Code
Manages Discord RPC connection and updates presence based on Claude Code activity.
"""

import sys
import os
import json
import re
import subprocess
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
SESSIONS_FILE = DATA_DIR / "sessions.json"  # Tracks active session PIDs

# Orphan check interval (seconds) - how often daemon checks for dead sessions
ORPHAN_CHECK_INTERVAL = 30

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

# Model pricing per 1M tokens (input, output, cache_read, cache_write)
# Cache reads at 0.1x input rate, cache writes at 1.25x input rate
MODEL_PRICING = {
    # Claude 4.5 series
    "claude-opus-4-5-20251101": (5.00, 25.00, 0.50, 6.25),
    "claude-sonnet-4-5-20250514": (3.00, 15.00, 0.30, 3.75),
    "claude-haiku-4-5-20250414": (1.00, 5.00, 0.10, 1.25),
    # Claude 4 series
    "claude-opus-4-20250514": (15.00, 75.00, 1.50, 18.75),
    "claude-sonnet-4-20250514": (3.00, 15.00, 0.30, 3.75),
    # Legacy/fallback
    "claude-sonnet-4-5-20241022": (3.00, 15.00, 0.30, 3.75),
    "claude-haiku-4-5-20241022": (1.00, 5.00, 0.10, 1.25),
}

# Tool to display name mapping (keep short for Discord limit)
TOOL_DISPLAY = {
    # File operations
    "Edit": "Editing",
    "Write": "Writing",
    "Read": "Reading",
    "Glob": "Searching",
    "Grep": "Grepping",
    "LS": "Browsing",
    # Execution
    "Bash": "Running",
    "Task": "Delegating",
    # Web
    "WebFetch": "Fetching",
    "WebSearch": "Researching",
    # Notebook
    "NotebookEdit": "Editing",
    "NotebookRead": "Reading",
    # Interaction
    "AskUserQuestion": "Asking",
    "TodoRead": "Reviewing",
    "TodoWrite": "Planning",
    # MCP (generic)
    "mcp": "Using MCP",
}

# Idle timeout in seconds (5 minutes) - after this, show "Idling" instead of last activity
IDLE_TIMEOUT = 5 * 60


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
        except (json.JSONDecodeError, OSError):
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
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True
            )
            if str(pid) in result.stdout:
                return pid
        else:
            os.kill(pid, 0)  # Doesn't kill, just checks
            return pid
    except (ValueError, ProcessLookupError, PermissionError, OSError):
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
    except OSError:
        pass


def get_project_name(project_path: str = "") -> str:
    """Get project name from git remote origin or folder name.

    Priority:
    1. Git remote origin repo name (e.g., 'my-repo' from github.com/user/my-repo.git)
    2. Folder name as fallback
    """
    if not project_path:
        project_path = os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())

    folder_name = Path(project_path).name

    # Try to get git remote origin URL
    try:
        result = subprocess.run(
            ["git", "-C", project_path, "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            remote_url = result.stdout.strip()
            # Parse repo name from URL
            # Handles: https://github.com/user/repo.git, git@github.com:user/repo.git
            match = re.search(r'[/:]([^/:]+?)(?:\.git)?$', remote_url)
            if match:
                return match.group(1)
    except Exception:
        pass

    return folder_name


def get_git_branch(project_path: str) -> str:
    """Get current git branch name."""
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


def is_process_alive(pid: int) -> bool:
    """Check if a process with given PID is still running."""
    if sys.platform == "win32":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        # PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return False
    else:
        try:
            os.kill(pid, 0)  # Doesn't kill, just checks
            return True
        except (ProcessLookupError, PermissionError, OSError):
            return False


def get_claude_ancestor_pid() -> int | None:
    """Find the Claude Code (node) process in our ancestor chain."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        # Get process info via Windows API
        CreateToolhelp32Snapshot = ctypes.windll.kernel32.CreateToolhelp32Snapshot
        Process32First = ctypes.windll.kernel32.Process32First
        Process32Next = ctypes.windll.kernel32.Process32Next
        CloseHandle = ctypes.windll.kernel32.CloseHandle

        TH32CS_SNAPPROCESS = 0x00000002

        class PROCESSENTRY32(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_char * 260),
            ]

        # Build a map of pid -> (parent_pid, exe_name)
        snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snapshot == -1:
            return None

        process_map = {}
        pe32 = PROCESSENTRY32()
        pe32.dwSize = ctypes.sizeof(PROCESSENTRY32)

        if Process32First(snapshot, ctypes.byref(pe32)):
            while True:
                pid = pe32.th32ProcessID
                ppid = pe32.th32ParentProcessID
                exe = pe32.szExeFile.decode("utf-8", errors="ignore").lower()
                process_map[pid] = (ppid, exe)
                if not Process32Next(snapshot, ctypes.byref(pe32)):
                    break

        CloseHandle(snapshot)

        # Walk up the tree from current process looking for node.exe (Claude Code)
        current_pid = os.getpid()
        visited = set()
        while current_pid in process_map and current_pid not in visited:
            visited.add(current_pid)
            ppid, exe = process_map[current_pid]
            if "node" in exe or "claude" in exe:
                return current_pid
            current_pid = ppid

        return None
    else:
        # Unix: walk up using /proc
        current_pid = os.getpid()
        visited = set()
        while current_pid > 1 and current_pid not in visited:
            visited.add(current_pid)
            try:
                with open(f"/proc/{current_pid}/comm", "r") as f:
                    comm = f.read().strip().lower()
                if "node" in comm or "claude" in comm:
                    return current_pid
                with open(f"/proc/{current_pid}/stat", "r") as f:
                    stat = f.read()
                    ppid = int(stat.split()[3])
                    current_pid = ppid
            except (OSError, ValueError, IndexError):
                break
        return None


def read_sessions() -> dict:
    """Read active sessions {pid: timestamp}."""
    if SESSIONS_FILE.exists():
        try:
            return json.loads(SESSIONS_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def write_sessions(sessions: dict):
    """Write active sessions to file."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not sessions:
        try:
            SESSIONS_FILE.unlink()
        except OSError:
            pass
    else:
        try:
            SESSIONS_FILE.write_text(json.dumps(sessions), encoding="utf-8")
        except OSError as e:
            log(f"Warning: Could not write sessions: {e}")


def add_session(pid: int):
    """Register a new session by its parent PID."""
    sessions = read_sessions()
    sessions[str(pid)] = int(time.time())
    write_sessions(sessions)
    return len(sessions)


def remove_session(pid: int):
    """Unregister a session by its parent PID."""
    sessions = read_sessions()
    sessions.pop(str(pid), None)
    write_sessions(sessions)
    return len(sessions)


def cleanup_dead_sessions() -> int:
    """Remove sessions whose parent PIDs are no longer alive. Returns remaining count."""
    sessions = read_sessions()
    if not sessions:
        return 0

    alive_sessions = {}
    for pid_str, timestamp in sessions.items():
        pid = int(pid_str)
        if is_process_alive(pid):
            alive_sessions[pid_str] = timestamp
        else:
            log(f"Session PID {pid} is dead, removing")

    if len(alive_sessions) != len(sessions):
        write_sessions(alive_sessions)

    return len(alive_sessions)


def get_model_from_jsonl() -> str:
    """Get model name from most recent JSONL file."""
    if not PROJECTS_DIR.exists():
        return ""

    # Find most recent .jsonl file
    jsonl_files = []
    for path in PROJECTS_DIR.rglob("*.jsonl"):
        try:
            jsonl_files.append((path, path.stat().st_mtime))
        except OSError:
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
    except OSError:
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

    Returns: dict with input, output, cache_read, cache_write, cost, simple_cost
    """
    empty_result = {
        "input": 0,
        "output": 0,
        "cache_read": 0,
        "cache_write": 0,
        "cost": 0.0,
        "simple_cost": 0.0,
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
            except OSError:
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
    except OSError:
        return empty_result

    # Calculate cost
    cost = 0.0
    if last_model in MODEL_PRICING:
        input_price, output_price, cache_read_price, cache_write_price = MODEL_PRICING[last_model]
        # Input tokens at input rate
        cost += total_input * input_price / 1_000_000
        # Output tokens at output rate
        cost += total_output * output_price / 1_000_000
        # Cache reads at reduced rate (0.1x input)
        cost += total_cache_read * cache_read_price / 1_000_000
        # Cache writes at premium rate (1.25x input)
        cost += total_cache_write * cache_write_price / 1_000_000

    # Calculate simple cost (without cache benefits - what it would cost without caching)
    simple_cost = 0.0
    if last_model in MODEL_PRICING:
        input_price, output_price, _, _ = MODEL_PRICING[last_model]
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
    except (json.JSONDecodeError, OSError):
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
    last_orphan_check = 0  # Track when we last checked for dead sessions

    while True:
        try:
            # Periodically check for dead sessions (orphan cleanup)
            now = time.time()
            if now - last_orphan_check > ORPHAN_CHECK_INTERVAL:
                last_orphan_check = now
                active_count = cleanup_dead_sessions()
                if active_count == 0:
                    log("No active sessions remaining, daemon exiting")
                    break

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

            # Check for idle timeout - show "Idling" instead of clearing
            last_update = state.get("last_update", 0)
            is_idle = time.time() - last_update > IDLE_TIMEOUT

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

            # Determine activity - show "Idling" if idle timeout reached
            if is_idle:
                activity = "Idling"
            elif tool in TOOL_DISPLAY:
                activity = TOOL_DISPLAY[tool]
            elif tool.startswith("mcp__"):
                activity = "Using MCP"
            else:
                activity = "Working"

            # Build details line: "Activity on project (branch)"
            if git_branch:
                details = f"{activity} on {project} ({git_branch})"
            else:
                details = f"{activity} on {project}"

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
        except Exception:
            pass
    log("Daemon stopped")


def cmd_start():
    """Handle 'start' command - spawn daemon if needed, update state."""
    hook_input = read_hook_input()
    project = hook_input.get("cwd", os.environ.get("CLAUDE_PROJECT_DIR", ""))
    project_name = get_project_name(project) if project else get_project_name()

    # Register this session by Claude Code's PID (walk up process tree)
    claude_pid = get_claude_ancestor_pid()
    if not claude_pid:
        # Fallback to parent PID if we can't find Claude
        claude_pid = os.getppid()
        log(f"Warning: Could not find Claude ancestor, using parent PID {claude_pid}")
    session_count = add_session(claude_pid)

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

    log(f"Session started for PID {claude_pid} (active sessions: {session_count})")

    # Check if daemon is running
    if get_daemon_pid():
        log("Daemon already running")
        return

    # Spawn daemon in background
    log(f"Starting daemon for project: {project_name}")

    if sys.platform == "win32":
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
    # Unregister this session by Claude Code's PID
    claude_pid = get_claude_ancestor_pid()
    if not claude_pid:
        claude_pid = os.getppid()
    remaining = remove_session(claude_pid)

    if remaining > 0:
        log(f"Session ended for PID {claude_pid} (active sessions: {remaining})")
        return  # Don't stop daemon, other sessions still active

    log("Last session ended, stopping daemon")

    # Clear state
    write_state({})

    # Kill daemon if running
    pid = get_daemon_pid()
    if pid:
        try:
            if sys.platform == "win32":
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
    sessions = read_sessions()

    if pid:
        print(f"Daemon running (PID {pid})")
    else:
        print("Daemon not running")

    print(f"Active sessions: {len(sessions)}")
    if sessions:
        for spid, ts in sessions.items():
            alive = "alive" if is_process_alive(int(spid)) else "DEAD"
            print(f"  - PID {spid}: {alive}")

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
