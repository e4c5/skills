#!/usr/bin/env python3
"""Fetch PR/MR review threads and top-level comments for offline analysis.

Supports GitHub pull requests (via `gh`) and GitLab merge requests (via `glab`).
"""

import json
import os
import re
import subprocess
import sys
import urllib.parse
from datetime import datetime, timezone

TIMEOUT_S = 30

GITHUB_PR_RE = re.compile(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)")
GITLAB_MR_RE = re.compile(r"^https?://([^/]+)/(.+?)/-/merge_requests/(\d+)")
HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

THREAD_QUERY = """
query($owner: String!, $repo: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      reviewThreads(first: 50, after: $cursor) {
        pageInfo {
          hasNextPage
          endCursor
        }
        nodes {
          id
          isResolved
          isOutdated
          comments(first: 100) {
            nodes {
              id
              databaseId
              url
              path
              line
              originalLine
              diffHunk
              body
              author { login }
              replyTo { id }
            }
          }
        }
      }
    }
  }
}
"""

COMMENTS_QUERY = """
query($owner: String!, $repo: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      comments(first: 50, after: $cursor) {
        pageInfo {
          hasNextPage
          endCursor
        }
        nodes {
          id
          databaseId
          url
          body
          author { login }
        }
      }
    }
  }
}
"""

PR_BASE_QUERY = """
query($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      id
      url
    }
  }
}
"""


def run_gh_graphql(payload: dict) -> dict:
    try:
        result = subprocess.run(
            ["gh", "api", "graphql", "--input", "-"],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            check=True,
            timeout=TIMEOUT_S,
        )
        data = json.loads(result.stdout)
    except FileNotFoundError as e:
        raise RuntimeError("gh not found on PATH") from e
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"gh graphql failed: {e.stderr}") from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError("gh graphql timed out") from e
    except json.JSONDecodeError as e:
        raise RuntimeError("gh graphql returned invalid JSON") from e
    if data.get("errors"):
        raise RuntimeError(f"GitHub GraphQL errors: {data['errors']}")
    if "data" not in data:
        raise RuntimeError("GitHub GraphQL response missing data key")
    return data


def fetch_review_threads(owner: str, repo: str, pr_number: int) -> list[dict]:
    all_nodes: list[dict] = []
    cursor: str | None = None
    while True:
        variables = {
            "owner": owner,
            "repo": repo,
            "number": pr_number,
            "cursor": cursor,
        }
        data = run_gh_graphql({"query": THREAD_QUERY, "variables": variables})
        pr = (data["data"].get("repository") or {}).get("pullRequest") or {}
        conn = pr.get("reviewThreads") or {}
        all_nodes.extend(conn.get("nodes") or [])
        pinfo = conn.get("pageInfo") or {}
        if not pinfo.get("hasNextPage"):
            break
        cursor = pinfo.get("endCursor")
        if not cursor:
            raise RuntimeError(
                "GitHub GraphQL pagination inconsistency in fetch_review_threads: "
                f"hasNextPage=true but endCursor missing; pageInfo={pinfo!r}"
            )
    return all_nodes


def fetch_issue_comments(owner: str, repo: str, pr_number: int) -> list[dict]:
    all_nodes: list[dict] = []
    cursor: str | None = None
    while True:
        variables = {
            "owner": owner,
            "repo": repo,
            "number": pr_number,
            "cursor": cursor,
        }
        data = run_gh_graphql({"query": COMMENTS_QUERY, "variables": variables})
        pr = (data["data"].get("repository") or {}).get("pullRequest") or {}
        conn = pr.get("comments") or {}
        all_nodes.extend(conn.get("nodes") or [])
        pinfo = conn.get("pageInfo") or {}
        if not pinfo.get("hasNextPage"):
            break
        cursor = pinfo.get("endCursor")
        if not cursor:
            raise RuntimeError(
                "GitHub GraphQL pagination inconsistency in fetch_issue_comments: "
                f"hasNextPage=true but endCursor missing; pageInfo={pinfo!r}"
            )
    return all_nodes


def fetch_pr_base(owner: str, repo: str, pr_number: int) -> dict | None:
    data = run_gh_graphql(
        {
            "query": PR_BASE_QUERY,
            "variables": {
                "owner": owner,
                "repo": repo,
                "number": pr_number,
            },
        }
    )
    repo_data = data["data"].get("repository")
    if not repo_data:
        return None
    return repo_data.get("pullRequest")


def decompose_bot_comment(author, body, url):
    """Split large bot comments into multiple actionable items."""
    items = []
    normalized_author = (author or "").lower().replace("[bot]", "")
    cleaned_body = re.sub(r"^\s*<!--.*?-->\s*", "", body, flags=re.DOTALL)
    if normalized_author in ["coderabbitai", "codeant-ai", "viper-review"]:
        findings = re.findall(
            r"(?:###|####|\*\*)\s*(.*?)\n(.*?)(?=\n(?:###|####|\*\*)|$)",
            cleaned_body,
            re.DOTALL,
        )
        for title, content in findings:
            normalized_title = title.lower()
            if any(
                k in normalized_title
                for k in [
                    "actionable",
                    "nitpick",
                    "potential issue",
                    "suggestion",
                    "finding",
                ]
            ):
                items.append(
                    {
                        "title": title.strip(),
                        "content": content.strip(),
                    }
                )

        # Many review bots also emit single finding comments like:
        # "[High] message..."
        if not items:
            severity_match = re.match(
                r"^\s*(\[[^\]]+\])\s*(.+?)\s*$", cleaned_body, re.DOTALL
            )
            if severity_match:
                severity, content = severity_match.groups()
                items.append(
                    {
                        "title": severity.strip(),
                        "content": content.strip(),
                    }
                )

    if not items:
        items.append({"title": "General Comment", "content": body})
    return items


def build_github_context(owner: str, repo: str, pr_number: int, pr_url: str) -> dict:
    pr_node = fetch_pr_base(owner, repo, pr_number)
    if not pr_node:
        print("Pull request not found.")
        sys.exit(1)

    pr_id = pr_node["id"]
    thread_nodes = fetch_review_threads(owner, repo, pr_number)
    comment_nodes = fetch_issue_comments(owner, repo, pr_number)

    comments_to_process = []
    skipped_threads = []
    summary = {
        "threads_total": len(thread_nodes),
        "threads_resolved": 0,
        "threads_outdated": 0,
        "threads_active_unresolved": 0,
        "issue_comments_total": len(comment_nodes),
    }

    for thread in thread_nodes:
        is_resolved = bool(thread.get("isResolved"))
        is_outdated = bool(thread.get("isOutdated"))

        if is_resolved:
            summary["threads_resolved"] += 1
        elif is_outdated:
            summary["threads_outdated"] += 1
        else:
            summary["threads_active_unresolved"] += 1

        if is_resolved or is_outdated:
            all_comments = (thread.get("comments") or {}).get("nodes", [])
            top_comment = next(
                (comment for comment in all_comments if comment.get("replyTo") is None),
                all_comments[0] if all_comments else None,
            )
            skipped_threads.append(
                {
                    "threadId": thread["id"],
                    "isResolved": is_resolved,
                    "isOutdated": is_outdated,
                    "url": top_comment.get("url") if top_comment else None,
                    "path": top_comment.get("path") if top_comment else None,
                    "line": (
                        top_comment.get("line") or top_comment.get("originalLine")
                        if top_comment
                        else None
                    ),
                }
            )
            continue

        all_comments = (thread.get("comments") or {}).get("nodes", [])
        if not all_comments:
            continue

        # The root comment is the one that has no replyTo
        top_comments = [c for c in all_comments if c.get("replyTo") is None]
        if not top_comments:
            # Fallback to first comment if no replyTo is found (shouldn't happen)
            top_comment = all_comments[0]
        else:
            top_comment = top_comments[0]
        author = top_comment["author"]["login"] if top_comment.get("author") else "ghost"

        decomposed = decompose_bot_comment(
            author, top_comment["body"], top_comment["url"]
        )

        for index, item in enumerate(decomposed):
            comments_to_process.append(
                {
                    "type": "thread",
                    "threadId": thread["id"],
                    "isResolved": is_resolved,
                    "isOutdated": is_outdated,
                    "id": top_comment["id"],
                    "databaseId": top_comment["databaseId"],
                    "url": top_comment["url"],
                    "path": top_comment.get("path"),
                    "line": top_comment.get("line")
                    or top_comment.get("originalLine"),
                    "diffHunk": top_comment.get("diffHunk"),
                    "findingIndex": index,
                    "findingCount": len(decomposed),
                    "body": item["content"],
                    "title": item["title"],
                    "author": author,
                }
            )

    for comment in comment_nodes:
        author = comment["author"]["login"] if comment.get("author") else "ghost"
        decomposed = decompose_bot_comment(author, comment["body"], comment["url"])

        for item in decomposed:
            comments_to_process.append(
                {
                    "type": "general",
                    "id": comment["id"],
                    "databaseId": comment["databaseId"],
                    "url": comment["url"],
                    "body": item["content"],
                    "title": item["title"],
                    "author": author,
                }
            )

    return {
        "provider": "github",
        "host": "github.com",
        "owner": owner,
        "repo": repo,
        "pr_url": pr_url,
        "pr_number": pr_number,
        "pr_id": pr_id,
        "summary": summary,
        "skipped_threads": skipped_threads,
        "comments": comments_to_process,
    }


class GitLabNotFound(RuntimeError):
    """A `glab api` call failed with HTTP 404."""


def run_glab_api(
    host: str,
    endpoint: str,
    method: str = "GET",
    fields: dict | None = None,
    paginate: bool = False,
    raw: bool = False,
):
    """Call the GitLab REST API through `glab api`.

    Returns parsed JSON, a list of items when paginating, or text when raw=True.
    """
    cmd = ["glab", "api", "--hostname", host, "--method", method, endpoint]
    for key, value in (fields or {}).items():
        cmd += ["--raw-field", f"{key}={value}"]
    if paginate:
        cmd += ["--paginate", "--output", "ndjson"]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, check=True, timeout=TIMEOUT_S
        )
    except FileNotFoundError as e:
        raise RuntimeError("glab not found on PATH") from e
    except subprocess.CalledProcessError as e:
        message = f"glab api {endpoint} failed: {e.stderr or e.stdout}"
        if "404" in (e.stderr or ""):
            raise GitLabNotFound(message) from e
        raise RuntimeError(message) from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"glab api {endpoint} timed out") from e
    if raw:
        return result.stdout
    try:
        if paginate:
            return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"glab api {endpoint} returned invalid JSON") from e


def gitlab_project_endpoint(project_path: str) -> str:
    return f"projects/{urllib.parse.quote(project_path, safe='')}"


def fetch_gitlab_mr_diffs(host: str, project_ep: str, mr_iid: int) -> dict[str, dict]:
    """Map each changed path (old and new) to its diff entry in the current MR version."""
    try:
        diffs = run_glab_api(
            host,
            f"{project_ep}/merge_requests/{mr_iid}/diffs?per_page=100",
            paginate=True,
        )
    except RuntimeError:
        # GitLab < 15.7 has no /diffs endpoint; /changes is the deprecated equivalent.
        changes = run_glab_api(host, f"{project_ep}/merge_requests/{mr_iid}/changes")
        diffs = changes.get("changes") or []
    by_path: dict[str, dict] = {}
    for entry in diffs:
        for key in ("new_path", "old_path"):
            if entry.get(key):
                by_path.setdefault(entry[key], entry)
    return by_path


def extract_hunk(diff_text: str, line: int, old_side: bool = False) -> str | None:
    """Return the unified-diff hunk that covers `line` on the new (or old) side."""
    for hunk in re.split(r"(?m)^(?=@@ )", diff_text or ""):
        m = HUNK_HEADER_RE.match(hunk)
        if not m:
            continue
        if old_side:
            start, count = int(m.group(1)), int(m.group(2) or 1)
        else:
            start, count = int(m.group(3)), int(m.group(4) or 1)
        if start <= line < start + count:
            return hunk.rstrip("\n")
    return None


class GitLabFileCache:
    def __init__(self, host: str, project_ep: str):
        self.host = host
        self.project_ep = project_ep
        self._cache: dict[tuple[str, str], list[str] | None] = {}

    def lines(self, path: str, ref: str) -> list[str] | None:
        key = (path, ref)
        if key not in self._cache:
            endpoint = (
                f"{self.project_ep}/repository/files/"
                f"{urllib.parse.quote(path, safe='')}/raw?ref={ref}"
            )
            try:
                text = run_glab_api(self.host, endpoint, raw=True)
                self._cache[key] = text.splitlines()
            except GitLabNotFound:
                # Only a missing file/ref means the line is gone; other errors
                # propagate so a flaky fetch never hides an active thread.
                self._cache[key] = None
        return self._cache[key]


def gitlab_position_status(
    position: dict | None,
    head_sha: str,
    diffs_by_path: dict[str, dict],
    files: GitLabFileCache,
) -> tuple[bool, int | None]:
    """Approximate GitHub's isOutdated for a GitLab diff note.

    GitLab has no outdated flag, so a note counts as outdated when it was made on
    an older MR version and the commented line no longer exists unchanged in the
    current head (allowing for the line having moved). Returns (outdated, line).
    """
    if not position:
        return False, None
    new_path = position.get("new_path")
    old_path = position.get("old_path")
    new_line = position.get("new_line")
    old_line = position.get("old_line")

    if position.get("head_sha") == head_sha:
        return False, new_line or old_line

    if new_path not in diffs_by_path and old_path not in diffs_by_path:
        return True, new_line or old_line

    if new_line is None:
        # Comment on a removed line: the old side comes from the base, which
        # rarely changes, so it stays active while the file is still in the diff.
        return False, old_line

    original = files.lines(new_path, position["head_sha"])
    current = files.lines(new_path, head_sha)
    if original is None or current is None or new_line > len(original):
        return True, new_line

    text = original[new_line - 1]
    if new_line <= len(current) and current[new_line - 1] == text:
        return False, new_line
    if text.strip():
        matches = [i + 1 for i, candidate in enumerate(current) if candidate == text]
        if len(matches) == 1:
            return False, matches[0]
    return True, new_line


def build_gitlab_context(host: str, project_path: str, mr_iid: int, mr_url: str) -> dict:
    project_ep = gitlab_project_endpoint(project_path)
    try:
        mr = run_glab_api(host, f"{project_ep}/merge_requests/{mr_iid}")
    except RuntimeError as e:
        print(f"Merge request not found: {e}", file=sys.stderr)
        sys.exit(1)

    web_url = mr.get("web_url") or mr_url
    head_sha = (mr.get("diff_refs") or {}).get("head_sha") or mr.get("sha")
    discussions = run_glab_api(
        host,
        f"{project_ep}/merge_requests/{mr_iid}/discussions?per_page=100",
        paginate=True,
    )
    diffs_by_path = fetch_gitlab_mr_diffs(host, project_ep, mr_iid)
    files = GitLabFileCache(host, project_ep)

    comments_to_process = []
    skipped_threads = []
    summary = {
        "threads_total": 0,
        "threads_resolved": 0,
        "threads_outdated": 0,
        "threads_active_unresolved": 0,
        "issue_comments_total": 0,
    }

    for discussion in discussions:
        # System notes ("added 1 commit", "changed the description") are events, not comments.
        notes = [n for n in discussion.get("notes") or [] if not n.get("system")]
        if not notes:
            continue
        top_note = notes[0]
        author = (top_note.get("author") or {}).get("username") or "ghost"
        url = f"{web_url}#note_{top_note['id']}"
        decomposed = decompose_bot_comment(author, top_note.get("body") or "", url)

        if discussion.get("individual_note") or not top_note.get("resolvable"):
            summary["issue_comments_total"] += 1
            for item in decomposed:
                comments_to_process.append(
                    {
                        "type": "general",
                        "id": top_note["id"],
                        "databaseId": top_note["id"],
                        "url": url,
                        "body": item["content"],
                        "title": item["title"],
                        "author": author,
                    }
                )
            continue

        summary["threads_total"] += 1
        position = top_note.get("position")
        path = (position or {}).get("new_path") or (position or {}).get("old_path")
        is_resolved = all(n.get("resolved") for n in notes if n.get("resolvable"))
        is_outdated = False
        line = None
        if not is_resolved:
            is_outdated, line = gitlab_position_status(
                position, head_sha, diffs_by_path, files
            )
        elif position:
            line = position.get("new_line") or position.get("old_line")

        if is_resolved:
            summary["threads_resolved"] += 1
        elif is_outdated:
            summary["threads_outdated"] += 1
        else:
            summary["threads_active_unresolved"] += 1

        if is_resolved or is_outdated:
            skipped_threads.append(
                {
                    "threadId": discussion["id"],
                    "isResolved": is_resolved,
                    "isOutdated": is_outdated,
                    "url": url,
                    "path": path,
                    "line": line,
                }
            )
            continue

        diff_hunk = None
        if position and path in diffs_by_path and line:
            diff_hunk = extract_hunk(
                diffs_by_path[path].get("diff"),
                line,
                old_side=position.get("new_line") is None,
            )

        for index, item in enumerate(decomposed):
            comments_to_process.append(
                {
                    "type": "thread",
                    "threadId": discussion["id"],
                    "isResolved": is_resolved,
                    "isOutdated": is_outdated,
                    "id": top_note["id"],
                    "databaseId": top_note["id"],
                    "url": url,
                    "path": path,
                    "line": line,
                    "diffHunk": diff_hunk,
                    "findingIndex": index,
                    "findingCount": len(decomposed),
                    "body": item["content"],
                    "title": item["title"],
                    "author": author,
                }
            )

    namespace, _, repo = project_path.rpartition("/")
    return {
        "provider": "gitlab",
        "host": host,
        "project": project_path,
        "owner": namespace,
        "repo": repo,
        "pr_url": web_url,
        "pr_number": mr_iid,
        "pr_id": mr.get("id"),
        "summary": summary,
        "skipped_threads": skipped_threads,
        "comments": comments_to_process,
    }


def origin_remote_url() -> str:
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=True,
            timeout=TIMEOUT_S,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return ""
    return result.stdout.strip()


def current_branch_review_url() -> str | None:
    """Find the PR/MR for the current branch, asking gh or glab based on the origin remote."""
    if "github.com" in origin_remote_url():
        cmd = ["gh", "pr", "view", "--json", "url"]
        key = "url"
    else:
        cmd = ["glab", "mr", "view", "--output", "json"]
        key = "web_url"
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, check=True, timeout=TIMEOUT_S
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        print("Failed to get current pull/merge request.", file=sys.stderr)
        sys.exit(1)
    return json.loads(result.stdout).get(key)


def main(pr_url=None):
    if not pr_url:
        pr_url = current_branch_review_url()
        if not pr_url:
            print("No pull/merge request found for the current branch.")
            sys.exit(0)

    github_match = GITHUB_PR_RE.search(pr_url)
    gitlab_match = GITLAB_MR_RE.search(pr_url)
    try:
        if github_match:
            owner, repo, pr_number = github_match.groups()
            output_data = build_github_context(owner, repo, int(pr_number), pr_url)
        elif gitlab_match:
            host, project_path, mr_iid = gitlab_match.groups()
            output_data = build_gitlab_context(host, project_path, int(mr_iid), pr_url)
        else:
            print(f"Invalid PR/MR URL: {pr_url}")
            sys.exit(1)
    except RuntimeError as e:
        # Fail loudly rather than write a context file that is missing comments.
        print(f"API error, no context written: {e}", file=sys.stderr)
        sys.exit(1)

    output_data["generated_at"] = datetime.now(timezone.utc).isoformat()

    filename = f"comments-context-{output_data['pr_number']}.json"
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output_data, f, indent=2)

    print(f"Context saved to {out_path}")


if __name__ == "__main__":
    url_arg = sys.argv[1] if len(sys.argv) > 1 else None
    main(url_arg)
