#!/usr/bin/env python3
"""Resolve Sonar dashboard/API URLs to issues search, or discover link from latest PR (gh)."""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

TIMEOUT_S = 60
USER_AGENT = "kotte-analyze-sonar-issues/1.0"

# First matching Sonar link in comment body (dashboard or API).
SONAR_LINK_RE = re.compile(
    r"https?://[^\s\)>\"']*(?:sonarcloud\.io|sonarqube)[^\s\)>\"']*",
    re.IGNORECASE,
)


def run_gh_json(args: list[str], timeout: int = TIMEOUT_S) -> dict | list:
    try:
        result = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            check=True,
            timeout=timeout,
        )
        return json.loads(result.stdout)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"gh failed: {e.stderr or e}") from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError("gh timed out") from e


def latest_open_pr_url() -> str | None:
    data = run_gh_json(
        ["pr", "list", "--limit", "1", "--json", "url,number,title"]
    )
    if not data:
        return None
    return data[0]["url"]


def parse_github_pr(url: str) -> tuple[str, str, int] | None:
    m = re.search(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)", url)
    if not m:
        return None
    return m.group(1), m.group(2), int(m.group(3))


def _paginate_gh_issue_comments(owner: str, repo: str, pr_number: int) -> list[dict]:
    per_page = 100
    page = 1
    collected: list[dict] = []
    while True:
        path = (
            f"/repos/{owner}/{repo}/issues/{pr_number}/comments"
            f"?per_page={per_page}&page={page}&sort=created&direction=asc"
        )
        try:
            batch = run_gh_json(["api", path])
        except RuntimeError:
            break
        if not batch:
            break
        collected.extend(batch)
        if len(batch) < per_page:
            break
        page += 1
    return collected


def _paginate_gh_pr_review_comments(owner: str, repo: str, pr_number: int) -> list[dict]:
    per_page = 100
    page = 1
    collected: list[dict] = []
    while True:
        path = (
            f"/repos/{owner}/{repo}/pulls/{pr_number}/comments"
            f"?per_page={per_page}&page={page}"
        )
        try:
            batch = run_gh_json(["api", path])
        except RuntimeError:
            break
        if not batch:
            break
        collected.extend(batch)
        if len(batch) < per_page:
            break
        page += 1
    return collected


def _comment_ts(c: dict) -> str:
    return c.get("updated_at") or c.get("created_at") or ""


def gh_pr_comments_for_sonar(owner: str, repo: str, pr_number: int) -> str | None:
    """Return Sonar https URL from the latest matching issue or review comment."""
    issue_comments = _paginate_gh_issue_comments(owner, repo, pr_number)
    review_comments = _paginate_gh_pr_review_comments(owner, repo, pr_number)

    merged: list[tuple[str, dict]] = []
    for c in issue_comments:
        merged.append(("issue", c))
    for c in review_comments:
        merged.append(("review", c))
    merged.sort(key=lambda x: _comment_ts(x[1]))

    def is_sonar(body: str, login: str) -> bool:
        b = (body or "").lower()
        a = (login or "").lower()
        if "sonarcloud.io" in b or "sonarqube" in b:
            return True
        if "sonar" in a:
            return True
        return False

    def extract_url(body: str) -> str | None:
        m = SONAR_LINK_RE.search(body)
        if m:
            return m.group(0).rstrip(".,);")
        m = SONAR_LINK_RE.search(body.replace("](", " ").replace(")", " "))
        return m.group(0).rstrip(".,);") if m else None

    for _kind, c in reversed(merged):
        body = c.get("body") or ""
        login = (c.get("user") or {}).get("login") or ""
        if not is_sonar(body, login):
            continue
        url = extract_url(body)
        if url:
            return url

    return None


def is_probably_api_url(url: str) -> bool:
    p = urllib.parse.urlparse(url)
    return "/api/" in (p.path or "")


def merge_query(
    existing: dict[str, list[str]], extra: dict[str, str]
) -> dict[str, list[str]]:
    out = {k: list(v) for k, v in existing.items()}
    for k, v in extra.items():
        if k not in out:
            out[k] = [v]
    return out


def dashboard_url_to_api_search(url: str) -> str:
    """Map SonarQube / SonarCloud UI URL to /api/issues/search."""
    parsed = urllib.parse.urlparse(url.strip())
    qs = urllib.parse.parse_qs(parsed.query)
    base = f"{parsed.scheme}://{parsed.netloc}".rstrip("/")

    component_keys = (
        qs.get("id") or qs.get("component") or qs.get("projectKey") or [None]
    )[0]
    pull_request = (qs.get("pullRequest") or [None])[0]
    branch = (qs.get("branch") or [None])[0]

    api_params: list[tuple[str, str]] = [
        ("resolved", "false"),
        ("ps", "500"),
        ("additionalFields", "_all"),
    ]
    if component_keys:
        api_params.insert(0, ("componentKeys", component_keys))
    if pull_request:
        api_params.append(("pullRequest", pull_request))
    if branch:
        api_params.append(("branch", branch))

    return f"{base}/api/issues/search?{urllib.parse.urlencode(api_params)}"


def ensure_issues_search_url(url: str) -> str:
    u = url.strip()
    p = urllib.parse.urlparse(u)
    base = f"{p.scheme}://{p.netloc}".rstrip("/")

    if is_probably_api_url(u):
        qs = urllib.parse.parse_qs(p.query)
        merged = merge_query(
            qs,
            {
                "resolved": "false",
                "ps": "500",
                "additionalFields": "_all",
            },
        )
        new_q = urllib.parse.urlencode(merged, doseq=True)
        return f"{base}/api/issues/search?{new_q}"
    return dashboard_url_to_api_search(u)


def sonar_token() -> str | None:
    return os.environ.get("SONAR_TOKEN") or os.environ.get("SONARCLOUD_TOKEN")


def http_get_json(url: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
        method="GET",
    )
    token = sonar_token()
    if token:
        raw = base64.b64encode(f"{token}:".encode("utf-8")).decode("ascii")
        req.add_header("Authorization", f"Basic {raw}")

    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        hint = ""
        if e.code == 401:
            hint = " Set SONAR_TOKEN (or SONARCLOUD_TOKEN) if the project is private."
        raise RuntimeError(f"HTTP {e.code} fetching {url}{hint}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Request failed: {e}") from e

    return json.loads(body)


def fetch_all_issues(initial_api_url: str) -> dict:
    """Follow paging until all issues are retrieved."""
    from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

    all_issues: list[dict] = []
    url = initial_api_url
    last_data: dict = {}

    while True:
        last_data = http_get_json(url)
        issues = last_data.get("issues") or []
        all_issues.extend(issues)
        paging = last_data.get("paging") or {}
        page_index = int(paging.get("pageIndex", 1))
        page_size = int(paging.get("pageSize", len(issues) or 500))
        total = int(paging.get("total", len(all_issues)))

        if not issues:
            break
        if len(issues) < page_size:
            break
        if total and len(all_issues) >= total:
            break
        if page_size <= 0:
            break

        next_index = page_index + 1
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        qs["p"] = [str(next_index)]
        new_query = urlencode(qs, doseq=True)
        url = urlunparse(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path,
                parsed.params,
                new_query,
                parsed.fragment,
            )
        )

    merged = dict(last_data)
    merged["issues"] = all_issues
    merged["paging"] = {
        "pageIndex": 1,
        "pageSize": len(all_issues),
        "total": len(all_issues),
    }
    return merged


def fetch_duplication_details(base_url: str, file_key: str, pull_request: str | None = None, branch: str | None = None) -> dict:
    """Fetch detailed duplication blocks for a specific file."""
    params = {"key": file_key}
    if pull_request:
        params["pullRequest"] = pull_request
    if branch:
        params["branch"] = branch
    
    url = f"{base_url}/api/duplications/show?{urllib.parse.urlencode(params)}"
    try:
        return http_get_json(url)
    except Exception:
        return {}


def fetch_duplications(base_url: str, component: str, pull_request: str | None = None, branch: str | None = None) -> dict:
    # ... (same as before but passing parameters to fetch_duplication_details)
    # I'll just replace the whole function to be safe.
    """Fetch duplication measures for the component/PR."""
    params = {
        "component": component,
        "metricKeys": "duplicated_lines,duplicated_blocks,duplicated_files,duplicated_lines_density,new_duplicated_lines,new_duplicated_blocks,new_duplicated_lines_density",
    }
    if pull_request:
        params["pullRequest"] = pull_request
    if branch:
        params["branch"] = branch
    
    url = f"{base_url}/api/measures/component?{urllib.parse.urlencode(params)}"
    try:
        measures_data = http_get_json(url)
    except Exception:
        return {}

    # Also fetch component tree to see which files have duplications
    tree_params = {
        "component": component,
        "metricKeys": "duplicated_lines_density,new_duplicated_lines_density",
        "qualifiers": "FIL",
        "ps": "500",
    }
    if pull_request:
        tree_params["pullRequest"] = pull_request
    if branch:
        tree_params["branch"] = branch
    
    tree_url = f"{base_url}/api/measures/component_tree?{urllib.parse.urlencode(tree_params)}"
    try:
        tree_data = http_get_json(tree_url)
    except Exception:
        tree_data = {}

    files = tree_data.get("components", [])
    for f in files:
        density = 0.0
        for m in f.get("measures", []):
            if m.get("metric") in ["duplicated_lines_density", "new_duplicated_lines_density"]:
                val = m.get("value") or (m.get("periods", [{}])[0].get("value") if m.get("periods") else "0")
                try:
                    density = max(density, float(val))
                except ValueError:
                    pass
        
        if density > 0:
            f["duplication_details"] = fetch_duplication_details(base_url, f["key"], pull_request, branch)

    return {
        "summary": measures_data.get("component", {}).get("measures", []),
        "files": files,
    }


def main() -> None:
    sonar_arg = None
    for a in sys.argv[1:]:
        if a.startswith("http"):
            sonar_arg = a
            break

    source = "cli"
    resolved_url: str | None = None
    
    # If it's a GitHub URL, we need to resolve it to a Sonar link.
    if sonar_arg and "github.com" not in sonar_arg:
        resolved_url = sonar_arg
    else:
        pr_url = sonar_arg or latest_open_pr_url()
        if not pr_url:
            print("No open pull requests and no Sonar URL provided.", file=sys.stderr)
            sys.exit(1)
        
        parsed = parse_github_pr(pr_url)
        if not parsed:
            # If we couldn't parse it as a PR, but it's an HTTP link, maybe it's just a weird Sonar link?
            if sonar_arg:
                resolved_url = sonar_arg
            else:
                print(f"Could not parse PR URL: {pr_url}", file=sys.stderr)
                sys.exit(1)
        else:
            owner, repo, pr_number = parsed
            resolved_url = gh_pr_comments_for_sonar(owner, repo, pr_number)
            source = f"github_pr:{pr_url}"
            if not resolved_url:
                print(
                    f"No Sonar link found in comments on PR #{pr_number}. "
                    "Paste a Sonar dashboard or API URL, or ensure the bot left a link.",
                    file=sys.stderr,
                )
                sys.exit(1)

    try:
        api_url = ensure_issues_search_url(resolved_url)
    except Exception as e:
        print(f"Could not build API URL: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        payload = fetch_all_issues(api_url)
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)

    # Extract component/PR/branch from resolved_url to fetch duplications
    parsed_sonar = urllib.parse.urlparse(resolved_url)
    qs = urllib.parse.parse_qs(parsed_sonar.query)
    component = (qs.get("id") or qs.get("component") or qs.get("projectKey") or [None])[0]
    pull_request = (qs.get("pullRequest") or [None])[0]
    branch = (qs.get("branch") or [None])[0]
    
    duplications = {}
    if component:
        base_sonar_url = f"{parsed_sonar.scheme}://{parsed_sonar.netloc}".rstrip("/")
        duplications = fetch_duplications(base_sonar_url, component, pull_request, branch)

    out_name = "sonar-context.json"
    out_path = os.path.join(os.path.dirname(__file__), out_name)

    envelope = {
        "source": source,
        "resolved_sonar_url": resolved_url,
        "api_url_used": api_url,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sonar_response": payload,
        "duplications": duplications,
    }

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(envelope, f, indent=2)

    issues = payload.get("issues") or []
    dup_files = len([f for f in duplications.get("files", []) if any(m.get("metric") == "duplicated_lines_density" and float(m.get("value", 0)) > 0 for m in f.get("measures", []))])
    print(f"Saved {len(issues)} issue(s) and duplication data for {dup_files} file(s) to {out_path}")


if __name__ == "__main__":
    main()
