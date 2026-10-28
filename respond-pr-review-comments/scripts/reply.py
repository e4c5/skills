#!/usr/bin/env python3
"""Post one approved reply from a context file, attached to the comment it answers.

Reads provider details from the context file written by analyze_pr.py, so the same
command works for GitHub pull requests and GitLab merge requests. Threads get a
reply inside the thread; on GitHub the reply is then checked to be visible in the
thread rather than held in a pending review. Review bodies and top-level comments
cannot be replied to, so they get a top-level comment that quotes and links them.

Usage:
  reply.py CONTEXT_FILE ENTRY_URL --body-file FILE [--resolve]
"""

import argparse
import json
import re
import sys

from analyze_pr import gitlab_project_endpoint, run_gh_graphql, run_glab_api

GITHUB_PENDING_REVIEWS = """
query($id: ID!) {
  node(id: $id) {
    ... on PullRequest {
      reviews(states: PENDING, first: 10) { nodes { url author { login } } }
    }
  }
}
"""

GITHUB_THREAD_REPLY = """
mutation($id: ID!, $body: String!) {
  addPullRequestReviewThreadReply(input: { pullRequestReviewThreadId: $id, body: $body }) {
    comment { id url state }
  }
}
"""

GITHUB_THREAD_LAST_COMMENTS = """
query($id: ID!) {
  node(id: $id) {
    ... on PullRequestReviewThread { isResolved comments(last: 100) { totalCount nodes { id } } }
  }
}
"""

GITHUB_ADD_COMMENT = """
mutation($id: ID!, $body: String!) {
  addComment(input: { subjectId: $id, body: $body }) {
    commentEdge { node { url } }
  }
}
"""

GITHUB_RESOLVE_THREAD = """
mutation($id: ID!) {
  resolveReviewThread(input: { threadId: $id }) {
    thread { isResolved }
  }
}
"""


def quote(entry: dict) -> str:
    """The first lines of the original, as a markdown quote with a link."""
    text = re.sub(r"<!--.*?-->", "", entry["body"], flags=re.DOTALL)
    lines = [ln for ln in text.strip().splitlines() if ln.strip()][:3]
    quoted = "\n".join("> " + ln for ln in lines)
    return f"{quoted}\n>\n> ([{entry['author']}'s comment]({entry['url']}))\n\n"


def newer_replies(entry: dict, count: int) -> None:
    """Exit without resolving if the thread gained replies since the analysis (besides ours)."""
    if count > entry["commentCount"] + 1:
        sys.exit(f"Not resolved: {count - entry['commentCount'] - 1} new reply(ies) arrived after the "
                 "analysis. Run analyze_pr.py again and review them first.")


def github_reply(ctx: dict, entry: dict, body: str, resolve: bool) -> None:
    if entry["type"] != "thread":
        data = run_gh_graphql(
            {"query": GITHUB_ADD_COMMENT, "variables": {"id": ctx["pr_id"], "body": quote(entry) + body}}
        )
        url = data["data"]["addComment"]["commentEdge"]["node"]["url"]
        print(f"Posted top-level comment (GitHub has no replies here): {url}")
        return

    # A pending review captures the reply and hides it until the review is submitted.
    data = run_gh_graphql({"query": GITHUB_PENDING_REVIEWS, "variables": {"id": ctx["pr_id"]}})
    pending = [r for r in data["data"]["node"]["reviews"]["nodes"]
               if (r.get("author") or {}).get("login") == ctx["viewer"]]
    if pending:
        sys.exit(f"You have a pending review on this PR ({pending[0]['url']}); submit or discard it "
                 "first, or the reply is held inside it.")

    thread_id = entry["threadId"]
    data = run_gh_graphql({"query": GITHUB_THREAD_REPLY, "variables": {"id": thread_id, "body": body}})
    comment = data["data"]["addPullRequestReviewThreadReply"]["comment"]
    thread = run_gh_graphql({"query": GITHUB_THREAD_LAST_COMMENTS, "variables": {"id": thread_id}})
    thread = thread["data"]["node"]
    if comment["state"] == "PENDING" or comment["id"] not in [c["id"] for c in thread["comments"]["nodes"]]:
        sys.exit(f"Reply {comment['url']} was posted but is not visible in the thread "
                 f"(state {comment['state']}); check it on GitHub")
    print(f"Replied in thread: {comment['url']}")

    if resolve and not thread["isResolved"]:
        newer_replies(entry, thread["comments"]["totalCount"])
        run_gh_graphql({"query": GITHUB_RESOLVE_THREAD, "variables": {"id": thread_id}})
        print(f"Resolved thread {thread_id}")


def gitlab_reply(ctx: dict, entry: dict, body: str, resolve: bool) -> None:
    host = ctx["host"]
    mr_ep = f"{gitlab_project_endpoint(ctx['project'])}/merge_requests/{ctx['pr_number']}"
    if entry["type"] != "thread":
        note = run_glab_api(host, f"{mr_ep}/notes", method="POST", fields={"body": quote(entry) + body})
        print(f"Posted top-level comment (GitLab has no replies here): {ctx['pr_url']}#note_{note['id']}")
        return
    discussion_ep = f"{mr_ep}/discussions/{entry['threadId']}"
    note = run_glab_api(host, f"{discussion_ep}/notes", method="POST", fields={"body": body})
    print(f"Replied in thread: {ctx['pr_url']}#note_{note['id']}")
    if resolve:
        discussion = run_glab_api(host, discussion_ep)
        newer_replies(entry, sum(not n.get("system") for n in discussion.get("notes") or []))
        run_glab_api(host, discussion_ep, method="PUT", fields={"resolved": "true"})
        print(f"Resolved discussion {entry['threadId']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("context_file", help="comments-context-*.json from analyze_pr.py")
    parser.add_argument("url", help="url of the entry being answered, as in the context file")
    parser.add_argument("--body-file", required=True,
                        help="file holding the reply in Markdown ('-' for stdin); never pass it inline")
    parser.add_argument("--resolve", action="store_true", help="resolve the thread after replying")
    args = parser.parse_args()

    with open(args.context_file, encoding="utf-8") as f:
        ctx = json.load(f)
    entry = next((c for c in ctx["comments"] if c["url"] == args.url), None)
    if not entry:
        parser.error(f"no entry with url {args.url} in {args.context_file}")
    if args.resolve and entry["type"] != "thread":
        parser.error("--resolve only applies to thread entries")
    if args.resolve and not entry.get("resolvable", True):
        parser.error("this discussion cannot be resolved; reply without --resolve")

    if args.body_file == "-":
        body = sys.stdin.read()
    else:
        with open(args.body_file, encoding="utf-8") as f:
            body = f.read()
    if not body.strip():
        parser.error("the reply body is empty")

    provider = ctx.get("provider", "github")
    try:
        if provider == "github":
            github_reply(ctx, entry, body, args.resolve)
        elif provider == "gitlab":
            gitlab_reply(ctx, entry, body, args.resolve)
        else:
            sys.exit(f"Unknown provider in context file: {provider}")
    except RuntimeError as e:
        sys.exit(str(e))
    except (KeyError, TypeError) as e:
        # The API call succeeded but the response shape was unexpected.
        sys.exit(f"Unexpected API response ({e!r}); the comment may have been posted, check before retrying.")


if __name__ == "__main__":
    main()
