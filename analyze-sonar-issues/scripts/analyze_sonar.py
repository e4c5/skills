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
        batch = run_gh_json(["api", path])
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
        batch = run_gh_json(["api", path])
        if not batch:
            break
        collected.extend(batch)
        if len(batch) < per_page:
            break
        page += 1
    return collected


def _comment_ts(c: dict) -> str:
    return c.get("updated_at") or c.get("created_at") or ""


def _is_sonar(body: str, login: str) -> bool:
    b = (body or "").lower()
    a = (login or "").lower()
    if "sonarcloud.io" in b or "sonarqube" in b:
        return True
    if "sonar" in a:
        return True
    return False


def _extract_sonar_url(body: str) -> str | None:
    m = SONAR_LINK_RE.search(body)
    if m:
        return m.group(0).rstrip(".,);")
    m = SONAR_LINK_RE.search(body.replace("](", " ").replace(")", " "))
    return m.group(0).rstrip(".,);") if m else None


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

    for _kind, c in reversed(merged):
        body = c.get("body") or ""
        login = (c.get("user") or {}).get("login") or ""
        if not _is_sonar(body, login):
            continue
        url = _extract_sonar_url(body)
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
        out[k] = [v]
    return out


def dashboard_url_to_api_search(url: str) -> str:
    """Map SonarQube / SonarCloud UI URL to /api/issues/search."""
    parsed = urllib.parse.urlparse(url.strip())
    qs = urllib.parse.parse_qs(parsed.query)
    base = f"{parsed.scheme}://{parsed.netloc}".rstrip("/")

    component = (
        qs.get("id")
        or qs.get("component")
        or qs.get("projectKey")
        or qs.get("componentKey")
        or qs.get("componentKeys")
        or [None]
    )[0]
    if component and "," in component:
        component = component.split(",")[0]
    pull_request = (qs.get("pullRequest") or [None])[0]
    branch = (qs.get("branch") or [None])[0]

    api_params: list[tuple[str, str]] = [
        ("resolved", "false"),
        ("ps", "500"),
        ("additionalFields", "_all"),
    ]
    if component:
        api_params.insert(0, ("componentKeys", component))
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


def fetch_security_hotspots(base_url: str, component: str, pull_request: str | None = None, branch: str | None = None) -> list[dict]:
    """Fetch security hotspots via /api/hotspots/search (separate from issues API)."""
    all_hotspots: list[dict] = []
    page = 1
    while True:
        params: dict[str, str] = {
            "projectKey": component,
            "status": "TO_REVIEW",
            "ps": "500",
            "p": str(page),
        }
        if pull_request:
            params["pullRequest"] = pull_request
        if branch:
            params["branch"] = branch

        url = f"{base_url}/api/hotspots/search?{urllib.parse.urlencode(params)}"
        try:
            data = http_get_json(url)
        except RuntimeError as e:
            # /api/hotspots/search may not exist on older SonarQube versions; skip gracefully.
            # Any other error (auth, 5xx, network) should propagate so the caller knows.
            if "HTTP 404" in str(e):
                break
            raise

        hotspots = data.get("hotspots") or []
        all_hotspots.extend(hotspots)

        paging = data.get("paging") or {}
        page_index = int(paging.get("pageIndex", page))
        page_size = int(paging.get("pageSize", len(hotspots) or 500))
        total = int(paging.get("total", len(all_hotspots)))

        if not hotspots or len(hotspots) < page_size or len(all_hotspots) >= total:
            break
        page += 1

    return all_hotspots


_HOTSPOT_SEVERITY: dict[str, str] = {
    "HIGH": "BLOCKER",
    "MEDIUM": "CRITICAL",
    "LOW": "MAJOR",
}


def normalize_hotspot(hotspot: dict) -> dict:
    """Map a hotspot record to the same shape as an issues/search item."""
    prob = (hotspot.get("vulnerabilityProbability") or "LOW").upper()
    return {
        "key": hotspot.get("key"),
        "rule": hotspot.get("ruleKey"),
        "severity": _HOTSPOT_SEVERITY.get(prob, "MAJOR"),
        "type": "SECURITY_HOTSPOT",
        "securityCategory": hotspot.get("securityCategory"),
        "vulnerabilityProbability": prob,
        "component": hotspot.get("component"),
        "project": hotspot.get("project"),
        "line": hotspot.get("line"),
        "message": hotspot.get("message"),
        "status": hotspot.get("status"),
        "author": hotspot.get("author"),
        "creationDate": hotspot.get("creationDate"),
        "updateDate": hotspot.get("updateDate"),
        "textRange": hotspot.get("textRange"),
        "flows": hotspot.get("flows"),
    }


def fetch_duplication_details(base_url: str, file_key: str, pull_request: str | None = None, branch: str | None = None) -> dict:
    """Fetch detailed duplication blocks for a specific file."""
    params = {"key": file_key}
    if pull_request:
        params["pullRequest"] = pull_request
    if branch:
        params["branch"] = branch
    
    url = f"{base_url}/api/duplications/show?{urllib.parse.urlencode(params)}"
    return http_get_json(url)


def _extract_density(component: dict) -> float:
    """Extract max duplication density from component measures."""
    density = 0.0
    for m in component.get("measures", []):
        if m.get("metric") in ["duplicated_lines_density", "new_duplicated_lines_density"]:
            periods = m.get("periods")
            val = m.get("value") or (periods[0].get("value") if periods else "0")
            try:
                density = max(density, float(val))
            except (ValueError, TypeError):
                pass
    return density


def fetch_duplications(base_url: str, component: str, pull_request: str | None = None, branch: str | None = None) -> dict:
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
    measures_data = http_get_json(url)

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
    tree_data = http_get_json(tree_url)

    files = tree_data.get("components", [])
    for f in files:
        if _extract_density(f) > 0:
            try:
                f["duplication_details"] = fetch_duplication_details(base_url, f["key"], pull_request, branch)
            except Exception:
                f["duplication_details"] = {}

    return {
        "summary": measures_data.get("component", {}).get("measures", []),
        "files": files,
    }


def _get_api_params_from_url(url: str) -> tuple[str | None, str | None, str | None]:
    """Extract component, pullRequest, and branch from a Sonar URL."""
    parsed = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(parsed.query)

    component = (
        qs.get("id")
        or qs.get("component")
        or qs.get("projectKey")
        or qs.get("componentKey")
        or qs.get("componentKeys")
        or [None]
    )[0]
    if component and "," in component:
        component = component.split(",")[0]

    pull_request = (qs.get("pullRequest") or [None])[0]
    branch = (qs.get("branch") or [None])[0]
    return component, pull_request, branch


def _resolve_sonar_url() -> tuple[str, str]:
    """Determine the Sonar URL from CLI args or GitHub PR."""
    sonar_arg = None
    for a in sys.argv[1:]:
        if a.startswith("http"):
            sonar_arg = a
            break

    if sonar_arg and "github.com" not in sonar_arg:
        return sonar_arg, "cli"

    pr_url = sonar_arg or latest_open_pr_url()
    if not pr_url:
        print("No open pull requests and no Sonar URL provided.", file=sys.stderr)
        sys.exit(1)

    parsed_pr = parse_github_pr(pr_url)
    if not parsed_pr:
        if sonar_arg:
            return sonar_arg, "cli"
        print(f"Could not parse PR URL: {pr_url}", file=sys.stderr)
        sys.exit(1)

    owner, repo, pr_number = parsed_pr
    resolved_url = gh_pr_comments_for_sonar(owner, repo, pr_number)
    if not resolved_url:
        print(
            f"No Sonar link found in comments on PR #{pr_number}. "
            "Paste a Sonar dashboard or API URL, or ensure the bot left a link.",
            file=sys.stderr,
        )
        sys.exit(1)

    return resolved_url, f"github_pr:{pr_url}"


def main() -> None:
    resolved_url, source = _resolve_sonar_url()

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

    component, pull_request, branch = _get_api_params_from_url(resolved_url)
    duplications = {}
    security_hotspots: list[dict] = []
    if component:
        parsed_sonar = urllib.parse.urlparse(resolved_url)
        base_sonar_url = f"{parsed_sonar.scheme}://{parsed_sonar.netloc}".rstrip("/")
        duplications = fetch_duplications(base_sonar_url, component, pull_request, branch)
        raw_hotspots = fetch_security_hotspots(base_sonar_url, component, pull_request, branch)
        normalized_hotspots = [normalize_hotspot(h) for h in raw_hotspots]
        payload["issues"] = (payload.get("issues") or []) + normalized_hotspots
        if payload.get("paging"):
            payload["paging"]["total"] = len(payload["issues"])
            payload["paging"]["pageSize"] = len(payload["issues"])

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
    hotspot_count = sum(1 for i in issues if i.get("type") == "SECURITY_HOTSPOT")
    files_with_dups = duplications.get("files", [])
    dup_files_count = len([
        f for f in files_with_dups 
        if any(m.get("metric") == "duplicated_lines_density" and float(m.get("value", 0)) > 0 for m in f.get("measures", []))
    ])
    print(f"Saved {len(issues)} finding(s) ({hotspot_count} security hotspot(s)) and duplication data for {dup_files_count} file(s) to {out_path}")


if __name__ == "__main__":
    main()
