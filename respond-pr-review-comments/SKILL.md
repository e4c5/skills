---
name: respond-pr-review-comments
description: Analyze top-level pull request (GitHub) or merge request (GitLab) review comments using an automated script, resolve non-actionable comments, and create a plan for those requiring code changes.
---

You take one optional argument: a GitHub pull request URL or a GitLab merge request URL (including self-hosted GitLab). Without it, the PR/MR for the current branch is used (`gh` for GitHub remotes, `glab` otherwise).

## Goal

Automate the analysis of pull/merge request comments. Use a Python script to gather active comments and their context, then use AI to decide whether each needs a code change.

- Non-actionable comments: Reply and resolve the thread immediately.
- Actionable comments: Add to a `fixes.md` file with a concrete implementation plan.

## Steps

1. **Gather Context:**
   - Run the analysis script from this skill's `scripts/` directory: `python3 {skill_dir}/scripts/analyze_pr.py [PR_OR_MR_URL]`
   - The script prints the path of the context file it wrote (`{skill_dir}/scripts/comments-context-{pr_number}.json`). Use that exact path below. If the script terminates without generating this file (e.g., no open PR/MR), inform the user and stop.
   - The context file's `provider` field is `github` or `gitlab`. On GitLab, `threadId` is the discussion id and `pr_number` is the MR iid.
   - Read the `summary` first. This distinguishes:
     - `threads_active_unresolved`: still need action or explicit resolution.
     - `threads_outdated`: still visible on GitHub/GitLab, but already superseded by code changes and intentionally skipped. GitLab has no native outdated flag; the script marks a GitLab thread outdated when it was made on an older MR version and the commented line no longer exists unchanged in the current head.
     - `threads_resolved`: already closed threads.

2. **Process Comments:**
   - Read the context file.
   - For each comment in the `comments` list:
     - **Analyze & Verify:** 
       - For `type: "thread"` entries, read the file at `path` near `line` when `path` is present.
       - For `type: "general"` entries (issue comments / non-resolvable MR notes), and for GitLab threads started on the MR overview rather than a line, there is no `path`/`line`; rely on `body`, `title`, and repository state instead.
       - Check if the issue is still present and if the suggested fix makes sense in the current context.
       - For bot findings, look for "Prompt for AI Agents", "Committable suggestion" (GitHub `suggestion` blocks), or GitLab ```` ```suggestion:-0+0 ```` blocks in the comment body.
     - **If no code change is needed:**
       - Reply with `scripts/reply.py`, which works for both GitHub and GitLab using the context file:
         - If it's a `thread` type, reply into the thread and resolve it:
           ```bash
           python3 {skill_dir}/scripts/reply.py {context_file} --thread-id "{threadId}" --resolve --body "@{author} [Detailed explanation why the change is not needed or already addressed]"
           ```
         - If it's a `general` type, post a top-level comment:
           ```bash
           python3 {skill_dir}/scripts/reply.py {context_file} --general --body "@{author} [Detailed explanation why the change is not needed or already addressed]"
           ```
   - If the user asks why comments are "still there," check `skipped_threads` before assuming the skill missed them. Outdated threads remain visible in GitHub/GitLab review history even when they no longer need action.
   - After a separate implementation pass, do a second verification pass on any still-active unresolved threads. If the fix is now present in code, reply with the verification and resolve the thread instead of re-adding it to `fixes.md`.
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
   - Once all comments are processed, delete the context file at the path the script printed.
   - Inform the user that the `fixes.md` file is ready and provides a high-fidelity roadmap for implementation.

## Output requirements

- Produce a `fixes.md` file containing all actionable items.
- Ensure all non-actionable threads are resolved on GitHub/GitLab.
- Do not process outdated or already resolved comments.
- Make it explicit to the user when a remaining comment is merely outdated versus still actively unresolved.
