#!/usr/bin/env python3
"""Fetch PR review threads and top-level comments for offline analysis (GitHub CLI)."""

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

TIMEOUT_S = 30

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
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"gh graphql failed: {e.stderr}") from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError("gh graphql timed out") from e
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
    if author in ["coderabbitai", "codeant-ai", "viper-review"]:
        findings = re.findall(
            r"(?:###|####|\*\*)\s*(.*?)\n(.*?)(?=\n(?:###|####|\*\*)|$)",
            body,
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

    if not items:
        items.append({"title": "General Comment", "content": body})
    return items


def main(pr_url=None):
    if not pr_url:
        try:
            result = subprocess.run(
                ["gh", "pr", "list", "--limit", "1", "--json", "url"],
                capture_output=True,
                text=True,
                check=True,
                timeout=TIMEOUT_S,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            print("Failed to list pull requests.", file=sys.stderr)
            sys.exit(1)
        pr_list = json.loads(result.stdout)
        if pr_list:
            pr_url = pr_list[0]["url"]
        else:
            print("No open pull requests found.")
            sys.exit(0)

    match = re.search(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)", pr_url)
    if not match:
        print(f"Invalid PR URL: {pr_url}")
        sys.exit(1)

    owner, repo, pr_number = match.groups()
    pr_number = int(pr_number)

    pr_node = fetch_pr_base(owner, repo, pr_number)
    if not pr_node:
        print("Pull request not found.")
        sys.exit(1)

    pr_id = pr_node["id"]
    thread_nodes = fetch_review_threads(owner, repo, pr_number)
    comment_nodes = fetch_issue_comments(owner, repo, pr_number)

    comments_to_process = []

    for thread in thread_nodes:
        if thread.get("isResolved") or thread.get("isOutdated"):
            continue

        top_comments = [
            c
            for c in (thread.get("comments") or {}).get("nodes", [])
            if c.get("replyTo") is None
        ]
        if not top_comments:
            continue

        top_comment = top_comments[0]
        author = top_comment["author"]["login"] if top_comment.get("author") else "ghost"

        decomposed = decompose_bot_comment(
            author, top_comment["body"], top_comment["url"]
        )

        for item in decomposed:
            comments_to_process.append(
                {
                    "type": "thread",
                    "threadId": thread["id"],
                    "id": top_comment["id"],
                    "url": top_comment["url"],
                    "path": top_comment.get("path"),
                    "line": top_comment.get("line")
                    or top_comment.get("originalLine"),
                    "diffHunk": top_comment.get("diffHunk"),
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
                    "url": comment["url"],
                    "body": item["content"],
                    "title": item["title"],
                    "author": author,
                }
            )

    output_data = {
        "owner": owner,
        "repo": repo,
        "pr_url": pr_url,
        "pr_number": pr_number,
        "pr_id": pr_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "comments": comments_to_process,
    }

    filename = f"comments-context-{pr_number}.json"
    out_path = os.path.join(os.path.dirname(__file__), filename)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output_data, f, indent=2)

    print(f"Context saved to {out_path}")


if __name__ == "__main__":
    url_arg = (
        sys.argv[1] if len(sys.argv) > 1 and sys.argv[1].startswith("http") else None
    )
    main(url_arg)
