---
name: analyze-sonar-issues
description: >-
  Fetches SonarQube/SonarCloud open issues and duplication reports via the Web API (from a dashboard or API URL, or by
  discovering the Sonar link on the latest open PR), then builds a prioritized remediation plan.
  Use when the user mentions Sonar, SonarCloud, SonarQube, quality gate failures, or code duplications.
---

You take one optional argument: a Sonar **dashboard** or URL (SonarCloud or self-hosted SonarQube).

## Goal

Produce an actionable list of Sonar findings (issues and duplications): Write the findings to sonar-fixes.md 

## Steps

1. **Gather raw issues**
   - Run:
     ```bash
     python3 .agents/skills/analyze-sonar-issues/scripts/analyze_sonar.py [SONAR_URL]
     ```
   - **If the user provided a URL:** the script maps common UI paths (e.g. `project/issues`, `summary/new_code`) to `GET /api/issues/search` on the same host, merges sensible defaults (`resolved=false`, `ps=500`, `additionalFields=_all`), and paginates until all issues are collected.
   - **If no URL:** the script uses `gh` to pick the **latest open pull request**, merges **issue comments** and **pull review comments**, picks the **most recent** comment that looks like Sonar (body or author), extracts an `https` link to SonarCloud/SonarQube, then uses that link the same way.
   - Output file (next to the script): `sonar-context.json`. If the script exits with an error or no issues file, explain the error (missing `gh`, no open PRs, no Sonar link in comments, HTTP 401, etc.) and stop.

2. **Authentication**
   - Private projects or token-only instances: set **`SONAR_TOKEN`** or **`SONARCLOUD_TOKEN`** in the environment (Sonar user token; passed as HTTP Basic with the token as username and an empty password, per Sonar’s usual convention). Public projects do not need a token.
   - Do not commit tokens or echo them in logs.

3. **Analyze issues and duplications (AI)**
   - Read `sonar-context.json`. 
   - Use `sonar_response.issues` as the primary list of issues.
   - Use `duplications` for code duplication findings:
     - Check `duplications.summary` for overall project duplication metrics.
     - Check `duplications.files` for specific files with duplication.
     - For files with `duplication_details`, identify the specific blocks (line numbers and sizes) that are duplicated.
   - For each issue and duplication finding:
     - For issues, use fields such as `severity`, `type`, `message`, `component`, `line`, `rule`, etc.
     - For duplications, identify the repeating logic and suggest a refactoring (e.g., extract to a helper function).
   - **Prioritize:** Blocker/Critical issues first, then Major issues, then significant Duplications, then the rest. Group by component/file when helpful.
   - **Verify in repo:** Open the referenced paths when `component` or `path` maps cleanly to the workspace; confirm the finding still applies to current code.
   - **Produce a remediation plan** (markdown):
     - Summary counts by severity and duplication overview.
     - Ordered list of items with file/line, finding description, and a concrete fix approach.
     - Testing or verification notes where relevant.

4. **output file**
   - Create actional items list as **`sonar-fixes.md`** If the file already exists append. 

5. **Cleanup**
   - Delete `sonar-context.json` after the analysis is written, or leave it if the user prefers to keep the raw export.

## URL → API behavior (for the agent)

- **SonarCloud / SonarQube UI:** Same origin as the UI; API base is `{scheme}://{host}/api/`. Query parameters `id`, `component`, or `projectKey` map to `componentKeys`; `pullRequest` and `branch` are forwarded when present.
- **Already an API URL:** Any path containing `/api/` is normalized to `/api/issues/search` with query parameters preserved and defaults merged.
- **Wrong or minimal URLs:** If `componentKeys` cannot be inferred, tell the user to paste a full dashboard URL from the Sonar UI (issues view or PR analysis) or set `SONAR_TOKEN` and retry.

## Output requirements

- A clear, prioritized list of Sonar issues to address, grounded in `sonar-context.json` and verified against the codebase when paths exist.

