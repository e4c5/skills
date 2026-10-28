#!/usr/bin/env python3
"""Fetch PR/MR review threads, review bodies and top-level comments for offline analysis.

Supports GitHub pull requests (via `gh`) and GitLab merge requests (via `glab`).
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.parse
from datetime import datetime, timezone

TIMEOUT_S = 30

GITHUB_PR_RE = re.compile(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)")
GITLAB_MR_RE = re.compile(r"^https?://([^/]+)/(.+?)/-/merge_requests/(\d+)")
HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

COMMENT_FIELDS = """
              id
              databaseId
              url
              path
              line
              originalLine
              diffHunk
              body
              createdAt
              author { login }
              replyTo { id }
"""

THREAD_QUERY = """
query($owner: String!, $repo: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      reviewThreads(first: 50, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          isResolved
          isOutdated
          comments(first: 100) {
            pageInfo { hasNextPage endCursor }
            nodes {""" + COMMENT_FIELDS + """}
          }
        }
      }
    }
  }
}
"""

THREAD_COMMENTS_QUERY = """
query($id: ID!, $cursor: String) {
  node(id: $id) {
    ... on PullRequestReviewThread {
      comments(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {""" + COMMENT_FIELDS + """}
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
        pageInfo { hasNextPage endCursor }
        nodes { id databaseId url body createdAt author { login } }
      }
    }
  }
}
"""

# Review bodies hold findings that are not inline threads (CodeRabbit's
# nitpicks and "outside diff range" comments, Viper's summary-only findings).
REVIEWS_QUERY = """
query($owner: String!, $repo: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      reviews(first: 50, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes { id databaseId url body state createdAt author { login } }
      }
    }
  }
}
"""

PR_BASE_QUERY = """
query($owner: String!, $repo: String!, $number: Int!) {
  viewer { login }
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) { id url state headRefName headRefOid }
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


def paginate(query: str, variables: dict, connection) -> list[dict]:
    """Follow a GraphQL connection's cursor; `connection` picks it out of the response data."""
    nodes: list[dict] = []
    cursor = None
    while True:
        data = run_gh_graphql({"query": query, "variables": dict(variables, cursor=cursor)})
        conn = connection(data["data"]) or {}
        nodes.extend(conn.get("nodes") or [])
        pinfo = conn.get("pageInfo") or {}
        if not pinfo.get("hasNextPage"):
            return nodes
        cursor = pinfo.get("endCursor")
        if not cursor:
            raise RuntimeError(f"hasNextPage=true but endCursor missing; pageInfo={pinfo!r}")


def pull_request(data: dict, field: str):
    return (((data.get("repository") or {}).get("pullRequest")) or {}).get(field)


def fetch_review_threads(variables: dict) -> list[dict]:
    threads = paginate(THREAD_QUERY, variables, lambda d: pull_request(d, "reviewThreads"))
    for thread in threads:
        conn = thread.get("comments") or {}
        if (conn.get("pageInfo") or {}).get("hasNextPage"):
            # the first page came with the thread; fetch the whole thread again by id
            conn["nodes"] = paginate(THREAD_COMMENTS_QUERY, {"id": thread["id"]},
                                     lambda d: (d.get("node") or {}).get("comments"))
    return threads


def mentioned_later(author: str, created_at: str, comments: list[dict], viewer: str) -> bool:
    """True if the viewer posted a top-level comment after `created_at` that @-mentions `author`."""
    pattern = re.compile(r"@" + re.escape(author) + r"\b", re.IGNORECASE)
    return any(
        c["author"] == viewer and c["createdAt"] > created_at and pattern.search(c["body"] or "")
        for c in comments
    )


def top_level_entries(kind: str, items: list[dict], answers: list[dict], viewer: str, summary: dict) -> list[dict]:
    """Context entries for review bodies or top-level comments, without the viewer's own.

    `items` and `answers` are dicts with author, body, createdAt, url and databaseId.
    """
    entries = []
    for item in items:
        if item["author"] == viewer or not (item["body"] or "").strip():
            continue
        hint = mentioned_later(item["author"], item["createdAt"], answers, viewer)
        summary["review_bodies" if kind == "review" else "issue_comments"] += 1
        summary["mentioned_later"] += hint
        entries.append(dict(item, type=kind, viewer_mentioned_author_later=hint))
    return entries


def github_login(node: dict) -> str:
    return (node.get("author") or {}).get("login") or "ghost"


def build_github_context(owner: str, repo: str, pr_number: int, pr_url: str) -> dict:
    variables = {"owner": owner, "repo": repo, "number": pr_number}
    base = run_gh_graphql({"query": PR_BASE_QUERY, "variables": variables})["data"]
    pr_node = (base.get("repository") or {}).get("pullRequest")
    if not pr_node:
        print("Pull request not found.", file=sys.stderr)
        sys.exit(1)
    viewer = base["viewer"]["login"]

    thread_nodes = fetch_review_threads(variables)
    comment_nodes = paginate(COMMENTS_QUERY, variables, lambda d: pull_request(d, "comments"))
    review_nodes = paginate(REVIEWS_QUERY, variables, lambda d: pull_request(d, "reviews"))

    comments_to_process = []
    skipped_threads = []
    summary = new_summary(len(thread_nodes))

    for thread in thread_nodes:
        is_resolved = bool(thread.get("isResolved"))
        is_outdated = bool(thread.get("isOutdated"))
        comments = (thread.get("comments") or {}).get("nodes") or []
        if not comments:
            continue
        # The root comment is the one that has no replyTo
        root = next((c for c in comments if c.get("replyTo") is None), comments[0])
        line = root.get("line") or root.get("originalLine")

        if is_resolved or is_outdated:
            summary["threads_resolved" if is_resolved else "threads_outdated"] += 1
            skipped_threads.append({"threadId": thread["id"], "isResolved": is_resolved,
                                    "isOutdated": is_outdated, "url": root["url"],
                                    "path": root.get("path"), "line": line})
            continue

        summary["threads_active_unresolved"] += 1
        replies = [{"author": github_login(c), "body": c["body"], "createdAt": c["createdAt"]}
                   for c in comments if c is not root]
        comments_to_process.append(thread_entry(
            thread["id"], root["databaseId"], root["url"], root.get("path"), line,
            root.get("diffHunk"), github_login(root), root["body"], replies, viewer, summary))

    def top_level(node):
        return {"databaseId": node["databaseId"], "url": node["url"], "author": github_login(node),
                "createdAt": node["createdAt"], "body": node["body"]}

    answers = [top_level(c) for c in comment_nodes]
    # A dismissed review's findings were set aside on purpose.
    summary["reviews_dismissed"] = sum(r["state"] == "DISMISSED" for r in review_nodes)
    reviews = [dict(top_level(r), state=r["state"]) for r in review_nodes if r["state"] != "DISMISSED"]
    comments_to_process += top_level_entries("review", reviews, answers, viewer, summary)
    comments_to_process += top_level_entries("general", answers, answers, viewer, summary)

    return {
        "provider": "github",
        "host": "github.com",
        "owner": owner,
        "repo": repo,
        "pr_url": pr_node["url"],
        "pr_number": pr_number,
        "pr_id": pr_node["id"],
        "pr_state": pr_node["state"],
        "head_ref": pr_node["headRefName"],
        "head_oid": pr_node["headRefOid"],
        "viewer": viewer,
        "summary": summary,
        "skipped_threads": skipped_threads,
        "comments": comments_to_process,
    }


def new_summary(threads_total: int) -> dict:
    return {
        "threads_total": threads_total,
        "threads_resolved": 0,
        "threads_outdated": 0,
        "threads_active_unresolved": 0,
        "threads_last_reply_by_viewer": 0,
        "review_bodies": 0,
        "issue_comments": 0,
        "mentioned_later": 0,
    }


def thread_entry(thread_id, database_id, url, path, line, diff_hunk, author, body, replies, viewer, summary,
                 resolvable=True) -> dict:
    """A context entry for an active unresolved thread, with all its replies.

    `commentCount` lets reply.py notice replies that arrived after the analysis.
    """
    last_by_viewer = bool(replies) and replies[-1]["author"] == viewer
    summary["threads_last_reply_by_viewer"] += last_by_viewer
    return {
        "type": "thread",
        "threadId": thread_id,
        "databaseId": database_id,
        "url": url,
        "path": path,
        "line": line,
        "diffHunk": diff_hunk,
        "author": author,
        "body": body,
        "replies": replies,
        "commentCount": 1 + len(replies),
        "resolvable": resolvable,
        "last_reply_by_viewer": last_by_viewer,
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
    viewer = run_glab_api(host, "user")["username"]

    web_url = mr.get("web_url") or mr_url
    head_sha = (mr.get("diff_refs") or {}).get("head_sha") or mr.get("sha")
    discussions = run_glab_api(
        host,
        f"{project_ep}/merge_requests/{mr_iid}/discussions?per_page=100",
        paginate=True,
    )
    diffs_by_path = fetch_gitlab_mr_diffs(host, project_ep, mr_iid)
    files = GitLabFileCache(host, project_ep)

    def gitlab_login(note):
        return (note.get("author") or {}).get("username") or "ghost"

    comments_to_process = []
    skipped_threads = []
    general_notes = []
    summary = new_summary(0)

    for discussion in discussions:
        # System notes ("added 1 commit", "changed the description") are events, not comments.
        notes = [n for n in discussion.get("notes") or [] if not n.get("system")]
        if not notes:
            continue
        top_note = notes[0]
        url = f"{web_url}#note_{top_note['id']}"

        if discussion.get("individual_note"):
            general_notes.append({"databaseId": top_note["id"], "url": url, "author": gitlab_login(top_note),
                                  "createdAt": top_note["created_at"], "body": top_note.get("body") or ""})
            continue

        summary["threads_total"] += 1
        position = top_note.get("position")
        path = (position or {}).get("new_path") or (position or {}).get("old_path")
        # A discussion that cannot be resolved (e.g. replies to a plain comment) is
        # still a thread: answers belong in it. It is never resolved or outdated.
        resolvable = bool(top_note.get("resolvable"))
        is_resolved = resolvable and all(n.get("resolved") for n in notes if n.get("resolvable"))
        is_outdated = False
        line = None
        if not resolvable:
            line = (position or {}).get("new_line") or (position or {}).get("old_line")
        elif not is_resolved:
            is_outdated, line = gitlab_position_status(
                position, head_sha, diffs_by_path, files
            )
        elif position:
            line = position.get("new_line") or position.get("old_line")

        if is_resolved or is_outdated:
            summary["threads_resolved" if is_resolved else "threads_outdated"] += 1
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

        summary["threads_active_unresolved"] += 1
        diff_hunk = None
        if position and path in diffs_by_path and line:
            diff_hunk = extract_hunk(
                diffs_by_path[path].get("diff"),
                line,
                old_side=position.get("new_line") is None,
            )
        replies = [{"author": gitlab_login(n), "body": n.get("body") or "", "createdAt": n["created_at"]}
                   for n in notes[1:]]
        comments_to_process.append(thread_entry(
            discussion["id"], top_note["id"], url, path, line, diff_hunk,
            gitlab_login(top_note), top_note.get("body") or "", replies, viewer, summary, resolvable))

    comments_to_process += top_level_entries("general", general_notes, general_notes, viewer, summary)

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
        "pr_state": mr.get("state"),
        "head_ref": mr.get("source_branch"),
        "head_oid": head_sha,
        "viewer": viewer,
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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("pr_url", nargs="?", help="PR/MR URL (default: the current branch's PR/MR)")
    parser.add_argument("--out-dir",
                        help="directory for the context file (default: a new private temp directory)")
    args = parser.parse_args(argv)

    pr_url = args.pr_url or current_branch_review_url()
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

    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", f"{output_data['owner']}-{output_data['repo']}")
    if args.out_dir:
        os.makedirs(args.out_dir, exist_ok=True)
        out_dir = args.out_dir
    else:
        out_dir = tempfile.mkdtemp(prefix="pr-review-")
    out_path = os.path.join(out_dir, f"comments-context-{slug}-{output_data['pr_number']}.json")
    # Private, and never through a symlink someone planted at the predictable name.
    # Not O_EXCL: the skill re-runs the analysis into the same directory.
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(out_path, flags, 0o600)
    if hasattr(os, "fchmod"):  # Unix only; the mode only applies when the file is created
        os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(output_data, f, indent=2)

    print(json.dumps(output_data["summary"]))
    print(f"Context saved to {out_path}")


if __name__ == "__main__":
    main()
