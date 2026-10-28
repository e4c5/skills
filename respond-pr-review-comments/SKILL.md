---
name: respond-pr-review-comments
description: Analyze a GitHub pull request's or GitLab merge request's open review comments (threads, review bodies, top-level comments), verify each against the code, and produce a fixes.md plan plus proposed replies for the user to review before anything is posted or resolved.
---

You take one optional argument: a GitHub pull request URL or a GitLab merge request URL (including self-hosted GitLab). Without it, the PR/MR for the current branch is used (`gh` for GitHub remotes, `glab` otherwise).

## 1. Gather

```bash
python3 {skill_dir}/scripts/analyze_pr.py [PR_OR_MR_URL] [--out-dir <scratchpad>]
```

It prints a summary and the path of the context file it wrote (without `--out-dir`, a new private temp directory; the file is always mode 0600). Never write it into the repo or the skill folder. If the script fails or the PR/MR is not open (`pr_state`), tell the user and stop.

The context file's `provider` is `github` or `gitlab`. On GitLab, `threadId` is the discussion id and `pr_number` is the MR iid. It holds `comments` (to analyze) and `skipped_threads`: resolved or outdated threads, not analyzed, but list them if the user asks why comments are "still there". GitLab has no native outdated flag; the script marks a GitLab thread outdated when it was made on an older MR version and the commented line no longer exists unchanged in the current head.

Entry types in `comments`:
- `thread`: an active unresolved thread, with `path`/`line`/`diffHunk` when it is on a line, the root `body`, and all `replies`. `resolvable: false` marks a GitLab discussion that cannot be resolved (replies to a plain comment); answer it in the thread all the same. `last_reply_by_viewer: true` means the user already answered it; check whether the reviewer accepted before doing more.
- `review` (GitHub only): a review body (dismissed reviews are left out). Bots put findings here that are not inline threads (CodeRabbit nitpicks and outside-diff comments, Viper summary findings).
- `general`: a top-level comment, or a GitLab note that cannot be resolved.

The user's own review bodies and top-level comments are excluded. Threads the user started stay, since a reviewer may have replied in them; act only on what others wrote there. `viewer_mentioned_author_later` is only a hint that the user later @-mentioned that author, not proof that this comment was answered.

## 2. Verify

Make sure the working tree is at the PR/MR head (`head_ref`/`head_oid`). A bot comment can hold several findings; treat each one separately. For every finding, read the code and decide: still present and worth fixing, already fixed, or not applicable. Use the bot's "Prompt for AI Agents" block, GitHub `suggestion` block or GitLab ```` ```suggestion:-0+0 ```` block as input, not as the answer.

## 3. Write the plan for review

Write `fixes.md` at the repo root (untracked; never commit it) with two sections.

**Fixes**: one entry per actionable finding:
- Comment URL, `path:line`, the finding, the reviewer's suggestion if any
- Verified plan: what to change and where, and which tests to run or add
- Risks

**Proposed replies**: one entry per comment that needs no code change but deserves an answer: the URL, the reply text, and whether the thread will be resolved. Resolving closes every finding in a thread, so propose resolving only when none of its findings is in Fixes; otherwise the reply covers the answered findings and the thread stays open. Address the author as `@login` only for a real login.

**No reply needed**: list the rest with one line each on why: bot status, progress and promo posts, review wrappers and auto-approvals, and summaries that only repeat findings answered elsewhere. A reply there is only noise.

Then stop and show the user the summary counts, the fixes and the proposed replies. **Post nothing and resolve nothing until the user approves**; they may edit, drop or move items between the sections.

## 4. After approval

Post each approved reply with the helper, never with `gh pr comment`, `gh api`, `glab` or an inline body (the shell mangles backticks and `$`). Write the reply text to a file first:

```bash
python3 {skill_dir}/scripts/reply.py <context file> <entry url> --body-file <reply.md> [--resolve]
```

- `thread`: replies inside the thread, then resolves it with `--resolve`. It does not resolve a thread that gained replies after the analysis, or one marked `resolvable: false`; run the analysis again and review the new replies. On GitHub it checks the reply is visible in the thread, and refuses if the user has a pending review on the PR, which would hold the reply hidden until submitted; tell the user to submit or discard that review.
- `review` / `general`: neither GitHub nor GitLab can reply to these, so it posts a top-level comment that quotes and links the original. `--resolve` is refused.

If the helper exits with an error, report it to the user; do not post another way.

Implement the approved fixes if the user asks to. Afterwards, run the analysis again and check the still-active threads: for each fix now in the code, propose a reply plus resolve, again with approval first.

## 5. Clean up

Delete the context file at the path the script printed. Delete `fixes.md` once its items are done or the PR/MR is merged.
