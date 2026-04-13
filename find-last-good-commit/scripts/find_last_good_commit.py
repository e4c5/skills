#!/usr/bin/env python3
import subprocess
import sys
import os
import argparse

def run_command(cmd, cwd=None, capture_output=True):
    result = subprocess.run(
        cmd,
        shell=True,
        cwd=cwd,
        text=True,
        capture_output=capture_output,
        check=False
    )
    return result


def is_git_repo():
    return run_command("git rev-parse --is-inside-work-tree").returncode == 0

def main():
    parser = argparse.ArgumentParser(description="Find the last known good commit where tests pass.")
    parser.add_argument("--command", required=True, help="The test command to run (e.g., 'npm test').")
    parser.add_argument("--limit", type=int, default=20, help="Maximum number of commits to check backwards.")
    parser.add_argument("--no-stash", action="store_true", help="Do not stash uncommitted changes.")
    
    args = parser.parse_args()

    if not is_git_repo():
        print("Error: Not a git repository.", file=sys.stderr)
        sys.exit(1)

    # Get the current commit SHA or branch name
    original_ref = run_command("git rev-parse HEAD").stdout.strip()
    stashed = False
    
    # Check for uncommitted changes
    if not args.no_stash:
        status = run_command("git status --porcelain").stdout.strip()
        if status:
            print("Stashing uncommitted changes...")
            run_command("git stash")
            stashed = True

    print(f"Using test command: {args.command}")

    # Get the list of SHAs to check
    commits_res = run_command(f"git log -n {args.limit} --format='%H %s'")
    if not commits_res:
        if stashed:
            run_command("git stash pop")
        sys.exit(1)
        
    commits = [line.split(" ", 1) for line in commits_res.stdout.strip().split("\n") if line.strip()]

    found_commit = None
    try:
        for sha, msg in commits:
            print(f"Checking commit {sha[:8]}: {msg}")
            run_command(f"git checkout {sha}", capture_output=True)
            
            test_res = run_command(args.command, capture_output=True)
            if test_res.returncode == 0:
                print(f"\n✅ Found last known good commit: {sha[:8]}")
                print(f"Message: {msg}")
                found_commit = sha
                break
            else:
                print(f"❌ Tests failed on {sha[:8]}")
    finally:
        # Ensure we always return to the original state
        print(f"Returning to original ref {original_ref[:8]}...")
        run_command(f"git checkout {original_ref}", capture_output=True)
        if stashed:
            print("Restoring stashed changes...")
            run_command("git stash pop")

    if found_commit:
        print(f"\nResult: {found_commit}")
    else:
        print(f"\nNo good commit found within the last {args.limit} commits.")
        sys.exit(1)

if __name__ == "__main__":
    main()
