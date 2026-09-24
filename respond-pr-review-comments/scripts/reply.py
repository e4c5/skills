#!/usr/bin/env python3
"""Reply to (and optionally resolve) a PR/MR review thread, or post a general comment.

Reads provider details from the context file written by analyze_pr.py, so the same
command works for GitHub pull requests and GitLab merge requests.

Usage:
  reply.py CONTEXT_FILE --thread-id ID --body TEXT [--resolve]
  reply.py CONTEXT_FILE --general --body TEXT
"""

import argparse
import json
import sys

from analyze_pr import gitlab_project_endpoint, run_gh_graphql, run_glab_api

GITHUB_THREAD_REPLY = """
mutation($id: ID!, $body: String!) {
  addPullRequestReviewThreadReply(input: { pullRequestReviewThreadId: $id, body: $body }) {
    comment { url }
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


def github_reply(ctx: dict, args) -> None:
    if args.general:
        data = run_gh_graphql(
            {"query": GITHUB_ADD_COMMENT, "variables": {"id": ctx["pr_id"], "body": args.body}}
        )
        print(f"Commented: {data['data']['addComment']['commentEdge']['node']['url']}")
        return
    data = run_gh_graphql(
        {"query": GITHUB_THREAD_REPLY, "variables": {"id": args.thread_id, "body": args.body}}
    )
    print(f"Replied: {data['data']['addPullRequestReviewThreadReply']['comment']['url']}")
    if args.resolve:
        run_gh_graphql({"query": GITHUB_RESOLVE_THREAD, "variables": {"id": args.thread_id}})
        print(f"Resolved thread {args.thread_id}")


def gitlab_reply(ctx: dict, args) -> None:
    host = ctx["host"]
    mr_ep = f"{gitlab_project_endpoint(ctx['project'])}/merge_requests/{ctx['pr_number']}"
    if args.general:
        note = run_glab_api(host, f"{mr_ep}/notes", method="POST", fields={"body": args.body})
        print(f"Commented: {ctx['pr_url']}#note_{note['id']}")
        return
    discussion_ep = f"{mr_ep}/discussions/{args.thread_id}"
    note = run_glab_api(
        host, f"{discussion_ep}/notes", method="POST", fields={"body": args.body}
    )
    print(f"Replied: {ctx['pr_url']}#note_{note['id']}")
    if args.resolve:
        run_glab_api(host, discussion_ep, method="PUT", fields={"resolved": "true"})
        print(f"Resolved discussion {args.thread_id}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("context_file", help="comments-context-{number}.json from analyze_pr.py")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--thread-id", help="threadId of a `thread` entry")
    target.add_argument("--general", action="store_true", help="post a top-level comment")
    parser.add_argument("--body", required=True, help="comment text (Markdown)")
    parser.add_argument("--resolve", action="store_true", help="resolve the thread after replying")
    args = parser.parse_args()

    if args.resolve and args.general:
        parser.error("--resolve only applies to --thread-id")

    with open(args.context_file, encoding="utf-8") as f:
        ctx = json.load(f)

    provider = ctx.get("provider", "github")
    try:
        if provider == "github":
            github_reply(ctx, args)
        elif provider == "gitlab":
            gitlab_reply(ctx, args)
        else:
            sys.exit(f"Unknown provider in context file: {provider}")
    except RuntimeError as e:
        sys.exit(str(e))
    except (KeyError, TypeError) as e:
        # The API call succeeded but the response shape was unexpected.
        sys.exit(f"Unexpected API response ({e!r}); the comment may have been posted, check before retrying.")


if __name__ == "__main__":
    main()
