#!/usr/bin/env python3
import subprocess
import sys
import os
import shlex
import argparse

def run_command(cmd, cwd=None, capture_output=True):
    args = shlex.split(cmd) if isinstance(cmd, str) else cmd
    result = subprocess.run(
        args,
        shell=False,
        cwd=cwd,
        text=True,
        capture_output=capture_output,
        check=False
    )
    return result


def is_git_repo():
    return run_command("git rev-parse --is-inside-work-tree").returncode == 0


def get_original_ref() -> str:
    """Return the current branch name, or the SHA if in detached HEAD state."""
    branch_res = run_command("git symbolic-ref --quiet --short HEAD")
    if branch_res.returncode == 0:
        return branch_res.stdout.strip()
    return run_command("git rev-parse HEAD").stdout.strip()


def stash_changes() -> bool:
    """Stash uncommitted changes. Returns True if a stash was created."""
    status = run_command("git status --porcelain").stdout.strip()
    if status:
        print("Stashing uncommitted changes...")
        run_command("git stash")
        return True
    return False


def get_commits(limit: int) -> list[tuple[str, str]]:
    """Return a list of (sha, message) tuples for the last `limit` commits."""
    result = run_command(["git", "log", "-n", str(limit), "--format=%H %s"])
    if result.returncode != 0:
        return []
    return [line.split(" ", 1) for line in result.stdout.strip().split("\n") if line.strip()]


def find_good_commit(commits: list[tuple[str, str]], test_command: str) -> str | None:
    """Iterate commits and return the first SHA where test_command passes."""
    for sha, msg in commits:
        print(f"Checking commit {sha[:8]}: {msg}")
        checkout_res = run_command(f"git checkout {sha}", capture_output=True)
        if checkout_res.returncode != 0:
            print(f"Failed to checkout {sha[:8]}: {checkout_res.stderr.strip()}", file=sys.stderr)
            raise RuntimeError(f"git checkout {sha} failed")

        test_res = run_command(test_command, capture_output=True)
        if test_res.returncode == 0:
            print(f"\n✅ Found last known good commit: {sha[:8]}")
            print(f"Message: {msg}")
            return sha
        print(f"❌ Tests failed on {sha[:8]}")
    return None


def restore_state(original_ref: str, stashed: bool) -> None:
    """Checkout the original ref and pop any stash that was created."""
    print(f"Returning to original ref {original_ref[:8]}...")
    run_command(f"git checkout {original_ref}", capture_output=True)
    if stashed:
        print("Restoring stashed changes...")
        run_command("git stash pop")


def main():
    parser = argparse.ArgumentParser(description="Find the last known good commit where tests pass.")
    parser.add_argument("--command", required=True, help="The test command to run (e.g., 'npm test').")
    parser.add_argument("--limit", type=int, default=20, help="Maximum number of commits to check backwards.")
    parser.add_argument("--no-stash", action="store_true", help="Do not stash uncommitted changes.")

    args = parser.parse_args()

    if not is_git_repo():
        print("Error: Not a git repository.", file=sys.stderr)
        sys.exit(1)

    original_ref = get_original_ref()
    stashed = False if args.no_stash else stash_changes()
    print(f"Using test command: {args.command}")

    commits = get_commits(args.limit)
    if not commits:
        restore_state(original_ref, stashed)
        sys.exit(1)

    found_commit = None
    try:
        found_commit = find_good_commit(commits, args.command)
    finally:
        restore_state(original_ref, stashed)

    if found_commit:
        print(f"\nResult: {found_commit}")
    else:
        print(f"\nNo good commit found within the last {args.limit} commits.")
        sys.exit(1)

if __name__ == "__main__":
    main()
