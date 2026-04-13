---
name: find-last-good-commit
description: Automatically finds the most recent commit in a git repository where all tests pass. Use when the current codebase is in a failing state and you need to identify the last stable point for debugging or regression analysis.
---

# Find Last Known Good Commit

This skill automates the process of identifying the last commit where tests passed. It leverages your ability to determine the correct test suite for the current project.

## Workflow

1. **Determine Test Command**: Analyze the repository (e.g., check `package.json`, `Pyproject.toml`, `Makefile`) to identify the correct command that runs the full test suite and returns exit code 0 on success.
2. **Execute Search**:
   - Run the bundled script with the determined command:
     ```bash
     python3 .agents/skills/find-last-good-commit/scripts/find_last_good_commit.py --command "<test-command>"
     ```

## Usage

### Automated Execution

Use the provided Python script. It handles the git workflow (stashing, checking out commits, and restoring state) automatically.

**Options:**
- `--command "<cmd>"` (Required): The full test command as determined by your analysis.
- `--limit <n>`: The number of recent commits to investigate (default: 20).
- `--no-stash`: Skip stashing if the directory is already clean.

### Strategic Guidance

- Always verify the test command locally before starting the search to ensure it behaves as expected (returns non-zero on failure).
- If the project has many test suites, prioritize the one that accurately represents the "stable" state or the one currently failing.
- Be aware that some older commits may have different dependencies or require a different environment. If the tests fail due to infrastructure issues rather than code bugs, consider adjusting the command or investigating the environment.

## Cleanup

The script returns the repository to its original branch and restores stashed changes upon completion or error.
