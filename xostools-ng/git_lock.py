#!/usr/bin/env python3
"""
Simple file-based locking for git repositories.
"""

import os
import time
import fcntl
from pathlib import Path
from typing import Optional


class GitRepoLock:
    """Simple file-based locking for git repositories."""

    def __init__(self, repo_path: Path, timeout: float = 30.0):
        self.repo_path = repo_path
        self.timeout = timeout
        self.lock_file_path = repo_path / ".git" / "xos-cherry-pick.lock"
        self.lock_file: Optional[object] = None
        self.acquired = False

    def __enter__(self):
        """Acquire the lock."""
        if self.acquire():
            return self
        raise RuntimeError(f"Could not acquire lock for {self.repo_path}")

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Release the lock."""
        self.release()

    def acquire(self) -> bool:
        """Try to acquire the lock with timeout."""
        if self.acquired:
            return True

        # Check if .git directory exists
        git_dir = self.repo_path / ".git"
        if not git_dir.exists():
            return False

        start_time = time.time()

        while True:
            try:
                # Open lock file
                self.lock_file = open(self.lock_file_path, 'w')

                # Try to acquire exclusive lock (non-blocking)
                fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

                # Write simple info to lock file
                self.lock_file.write(f"{os.getpid()}\n")
                self.lock_file.flush()

                self.acquired = True
                return True

            except (IOError, OSError):
                # Lock is held by another process
                if self.lock_file:
                    self.lock_file.close()
                    self.lock_file = None

                # Check timeout
                if (time.time() - start_time) >= self.timeout:
                    return False

                # Wait and retry
                time.sleep(0.1)

    def release(self):
        """Release the lock."""
        if not self.acquired:
            return

        try:
            if self.lock_file:
                fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_UN)
                self.lock_file.close()
                self.lock_file = None

            # Remove lock file
            if self.lock_file_path.exists():
                self.lock_file_path.unlink()

        except Exception:
            pass  # Ignore cleanup errors

        self.acquired = False