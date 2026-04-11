---
name: respond-pr-review-comments
description: Analyze top-level pull request review comments from a PR URL, resolve comments that need no code change, and write a markdown action plan for comments that require code updates.
---

You take exactly one argument: a GitHub pull request URL.

## Goal

Process pull request **top-level review comments** one at a time (the first comment in each review thread, where `replyTo == null`; ignore replies), decide whether each needs a code change, and:

- Immediately resolve comments that do **not** require code changes.
- Create a markdown-formatted plan file for comments that **do** require code changes, using the required `review-actions-#nnn` pattern (replace `#nnn` with the PR number, for example `review-actions-123`).

## Steps

1. Parse `{owner}`, `{repo}`, and `{pr_number}` from the PR URL.
2. Retrieve PR context with:
   - `gh pr view {pr_url} --json number,title,body,baseRefName,headRefName,files`
   - `gh pr diff {pr_url}`
3. Retrieve review threads (not issue conversation comments) via GraphQL and paginate until done:
   - `repository.pullRequest.reviewThreads`
4. For each thread:
   - Skip if already resolved.
   - Select the top-level comment only (`replyTo == null`).
   - Ignore replies in that thread.
5. For each top-level comment, validate against diff and surrounding file context (`gh api`/`gh pr diff`/local file reads) and classify:
   - **Resolvable now**: comment can be addressed without code changes (already fixed, verifiable as based on a misunderstanding of current code/diff state, clearly not applicable to current changes, or explicit no-op).
   - **Needs code change**: valid feedback that requires code edits.
6. If **resolvable now**, resolve the thread immediately:
   - Use `resolveReviewThread` GraphQL mutation with the thread ID.
7. If **needs code change**, append an item to `review-actions-123` (replace `123` with the current PR number) with:
   - Thread/comment URL
   - File and line context with 1-indexed line numbers:
     - Single line: `path/to/file.ext:<line-number>` (example: `src/app.js:52`)
     - Line range: `path/to/file.ext:<start-line>-<end-line>` (example: `src/app.js:52-55`)
     - Deleted line: `path/to/file.ext:deleted@<old-line>` (example: `src/app.js:deleted@49`)
     - Mixed added/deleted range: include both in one field (example: `src/app.js:52-55; deleted@49`)
   - Why code change is needed
   - Concrete implementation plan for a follow-up coding agent
   - Risks/edge cases and validation notes

## Output requirements

- Produce `review-actions-123` in markdown format (replace `123` with the current PR number). If the file already exists, append a new run section with timestamp and keep prior sections for history.
- If every top-level review comment is resolved directly, still create the file and record that no code changes are required.
- Do not include reply comments in analysis or planning.
