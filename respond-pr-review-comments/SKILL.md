---
name: respond-pr-review-comments
description: Analyze top-level pull request review comments using an automated script, resolve non-actionable comments, and create a plan for those requiring code changes.
---

You take one optional argument: a GitHub pull request URL.

## Goal

Automate the analysis of pull request comments. Use a Python script to gather active comments and their context, then use AI to decide whether each needs a code change.

- Non-actionable comments: Reply and resolve the thread immediately.
- Actionable comments: Add to a `fixes.md` file with a concrete implementation plan.

## Steps

1. **Gather Context:**
   - Run the analysis script: `python3 .agents/skills/respond-pr-review-comments/scripts/analyze_pr.py [PR_URL]`
   - Identify the generated context file: `comments-context-{pr_number}.json`. If the script terminates without generating this file (e.g., no open PRs), inform the user and stop.

2. **Process Comments:**
   - Read the context file.
   - For each comment in the `comments` list:
     - **Analyze & Verify:** 
       - For `type: "thread"` entries, read the file at `path` near `line` when `path` is present.
       - For `type: "general"` entries (issue comments), there is often no `path`/`line`; rely on `body`, `title`, and repository state instead.
       - Check if the issue is still present and if the suggested fix makes sense in the current context.
       - For bot findings, look for "Prompt for AI Agents" or "Committable suggestion" blocks in the comment body.
     - **If no code change is needed:**
       - Post a reply: `gh pr review {pr_url} --comment --body "@author [Detailed explanation why the change is not needed or already addressed]"`
       - If it's a `thread` type, resolve it:
         ```bash
         gh api graphql -f query='mutation($id: ID!) { resolveReviewThread(input: { threadId: $id }) { thread { isResolved } } }' -f id="{threadId}"
         ```
     - **If a code change is needed:**
       - Append a detailed entry to `fixes.md`:
         - **Comment URL:** {url}
         - **File & Context:** `{path}:{line}`
         - **Finding:** {title} - {body}
         - **Original Suggestion:** (Include the bot's suggestion or "Prompt for AI Agents" if present)
         - **Verified Plan:** A step-by-step technical plan to implement the fix, including:
           1. Specific lines to modify.
           2. Logic changes required.
           3. Any new imports or dependencies.
           4. **Testing Strategy:** Which specific tests to run or add to verify the fix.
         - **Risks:** Potential side effects or edge cases to watch out for.

3. **Cleanup:**
   - Once all comments are processed, delete the `comments-context-{pr_number}.json` file.
   - Inform the user that the `fixes.md` file is ready and provides a high-fidelity roadmap for implementation.

## Output requirements

- Produce a `fixes.md` file containing all actionable items.
- Ensure all non-actionable threads are resolved on GitHub.
- Do not process outdated or already resolved comments.
