---
name: respond-pr-review-comments
description: Analyze top-level pull request review comments from a PR URL, resolve comments that need no code change, and write a markdown action plan for comments that require code updates.
---

You take exactly one argument: a GitHub pull request URL.

## Goal

Process pull request **top-level review comments** one at a time (ignore replies), decide whether each needs a code change, and:

- Immediately resolve comments that do **not** require code changes.
- Create a markdown-formatted plan file named `review-actions-<pr-number>` for comments that **do** require code changes.

## Steps

1. Parse `{owner}`, `{repo}`, and `{pr_number}` from the PR URL.
2. Retrieve PR context with:
   - `gh pr view <url> --json number,title,body,baseRefName,headRefName,files`
   - `gh pr diff <url>`
3. Retrieve review threads (not issue conversation comments) via GraphQL and paginate until done:
   - `repository.pullRequest.reviewThreads`
4. For each thread:
   - Skip if already resolved.
   - Select the top-level comment only (`replyTo == null`).
   - Ignore replies in that thread.
5. For each top-level comment, validate against diff and surrounding file context (`gh api`/`gh pr diff`/local file reads) and classify:
   - **Resolvable now**: comment can be addressed without code changes (already fixed, misunderstanding, not applicable, or no-op).
   - **Needs code change**: valid feedback that requires code edits.
6. If **resolvable now**, resolve the thread immediately:
   - Use `resolveReviewThread` GraphQL mutation with the thread ID.
7. If **needs code change**, append an item to `review-actions-#nnn` (replace `nnn` with the PR number, for example `review-actions-123`) with:
   - Thread/comment URL
   - File and line context
   - Why code change is needed
   - Concrete implementation plan for a follow-up coding agent
   - Risks/edge cases and validation notes

## Output requirements

- Produce or update `review-actions-#nnn` in markdown format (replace `nnn` with the PR number).
- If every top-level review comment is resolved directly, still create the file and record that no code changes are required.
- Do not include reply comments in analysis or planning.
