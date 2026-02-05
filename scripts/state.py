"""
Shared state management for Discord Rich Presence.
Provides process-safe state file operations with cross-platform file locking.
"""

import json
import os
import sys
import time
from pathlib import Path

# Platform-specific imports for file locking
if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

# ═══════════════════════════════════════════════════════════════
# Data Directory Setup
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
LOCK_FILE = DATA_DIR / "state.lock"


# ═══════════════════════════════════════════════════════════════
# File Locking
# ═══════════════════════════════════════════════════════════════

class StateLock:
    """
    Cross-platform file lock for state operations.

    Usage:
        with StateLock():
            state = read_state()
            state["key"] = "value"
            write_state(state)
    """

    def __init__(self, timeout: float = 5.0):
        self.timeout = timeout
        self._lock_fd = None

    def __enter__(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        start = time.time()

        while True:
            try:
                # Open lock file (create if doesn't exist)
                self._lock_fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_RDWR)

                if sys.platform == "win32":
                    # Windows: lock first byte exclusively
                    msvcrt.locking(self._lock_fd, msvcrt.LK_NBLCK, 1)
                else:
                    # Unix: exclusive non-blocking lock
                    fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

                return self

            except (OSError, IOError):
                # Lock acquisition failed, close and retry
                if self._lock_fd is not None:
                    try:
                        os.close(self._lock_fd)
                    except OSError:
                        pass
                    self._lock_fd = None

                if time.time() - start > self.timeout:
                    raise TimeoutError(f"Could not acquire state lock within {self.timeout}s")

                time.sleep(0.01)  # 10ms retry interval

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._lock_fd is not None:
            try:
                if sys.platform == "win32":
                    msvcrt.locking(self._lock_fd, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            except (OSError, IOError):
                pass
            finally:
                try:
                    os.close(self._lock_fd)
                except OSError:
                    pass
                self._lock_fd = None
        return False


# ═══════════════════════════════════════════════════════════════
# State Read/Write (Low-level, no locking)
# ═══════════════════════════════════════════════════════════════

def read_state_unlocked() -> dict:
    """
    Read current state from state file without locking.
    Use read_state() or wrap with StateLock for safe access.
    """
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            pass
    return {}


def write_state_unlocked(state: dict):
    """
    Write state to state file using atomic write pattern (no locking).
    Use write_state() or wrap with StateLock for safe access.
    """
    import shutil
    import tempfile

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    content = json.dumps(state, indent=2)

    fd, tmp_path = tempfile.mkstemp(dir=DATA_DIR, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(content)
        # shutil.move handles cross-platform atomic rename (including Windows overwrite)
        shutil.move(tmp_path, STATE_FILE)
    except (OSError, IOError):
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ═══════════════════════════════════════════════════════════════
# State Read/Write (Safe, with locking)
# ═══════════════════════════════════════════════════════════════

def read_state(logger=None) -> dict:
    """
    Read current state from state file with locking.

    Args:
        logger: Optional logging function for warnings

    Returns:
        State dict, or empty dict on error
    """
    try:
        with StateLock():
            return read_state_unlocked()
    except (OSError, TimeoutError) as e:
        if logger:
            logger(f"Warning: Could not read state: {e}")
        return {}


def write_state(state: dict, logger=None):
    """
    Write state to state file with locking.

    Args:
        state: State dict to write
        logger: Optional logging function for warnings
    """
    try:
        with StateLock():
            write_state_unlocked(state)
    except (OSError, TimeoutError) as e:
        if logger:
            logger(f"Warning: Could not write state: {e}")


def update_state(updates: dict, logger=None) -> dict:
    """
    Atomically update state with locking (read-modify-write).
    Only updates specified keys, preserving other state.

    Args:
        updates: Dict of key-value pairs to update
        logger: Optional logging function for warnings

    Returns:
        Updated state dict, or empty dict on error
    """
    try:
        with StateLock():
            state = read_state_unlocked()
            state.update(updates)
            write_state_unlocked(state)
            return state
    except (OSError, TimeoutError) as e:
        if logger:
            logger(f"Warning: Could not update state: {e}")
        return {}


def clear_state(logger=None):
    """
    Clear state file with locking.

    Args:
        logger: Optional logging function for warnings
    """
    try:
        with StateLock():
            write_state_unlocked({})
    except (OSError, TimeoutError) as e:
        if logger:
            logger(f"Warning: Could not clear state: {e}")
