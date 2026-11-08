#!/usr/bin/env python3
"""Refresh the public GitHub activity list using only the Python standard library."""

import json
import os
from pathlib import Path
import re
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


USERNAME = "RayZh-hs"
START = "<!-- ACTIVITY:START -->"
END = "<!-- ACTIVITY:END -->"
README = Path(__file__).resolve().parents[1] / "README.md"
CONTRIBUTION_EVENTS = {
    "PushEvent", "CreateEvent", "ReleaseEvent", "PullRequestEvent",
    "PullRequestReviewEvent", "PullRequestReviewCommentEvent", "IssuesEvent",
    "IssueCommentEvent", "CommitCommentEvent", "DiscussionEvent",
}


def link(label, url):
    # Encode Markdown metacharacters, including those in user-created branch names.
    label = re.sub(r"([\\`*{}\[\]<>()!|_])", lambda match: f"&#{ord(match[0])};", label)
    return f"[{label}]({url})"


def describe(event):
    """Return a short, linked summary, or None for unsupported/noisy events."""
    repo = event["repo"]["name"]
    url = f"https://github.com/{quote(repo, safe='/')}"
    project = link(repo, url)
    payload = event.get("payload", {})
    kind = event["type"]
    action = payload.get("action", "")

    if kind == "PushEvent":
        head = payload.get("head")
        target = f"{url}/commit/{quote(head, safe='')}" if head else url
        return f"🔨 Pushed to {link(repo, target)}"
    if kind == "WatchEvent" and action == "started":
        return f"⭐ Starred {project}"
    if kind == "ForkEvent":
        return f"🍴 Forked {project}"
    if kind == "CreateEvent":
        ref_type = payload.get("ref_type")
        if ref_type == "repository":
            return f"🌱 Created {project}"
        if ref_type in {"branch", "tag"} and payload.get("ref"):
            ref = payload["ref"]
            target = f"{url}/tree/{quote(ref, safe='')}"
            return f"🌱 Created {ref_type} {link(ref, target)} in {project}"
    if kind == "ReleaseEvent" and action == "published":
        release = payload.get("release", {})
        tag = release.get("tag_name")
        if tag:
            target = f"{url}/releases/tag/{quote(tag, safe='')}"
            return f"📦 Released {link(tag, target)} in {project}"
    if kind == "PullRequestEvent":
        pr = payload.get("pull_request", {})
        number = payload.get("number") or pr.get("number")
        verb = {"opened": "Opened", "reopened": "Reopened", "closed": "Closed"}.get(action)
        if action == "closed" and pr.get("merged"):
            verb = "Merged"
        if verb and number:
            return f"🔀 {verb} pull request {link(f'{repo}#{number}', f'{url}/pull/{number}')}"
    if kind == "IssuesEvent" and action in {"opened", "closed", "reopened"}:
        number = payload.get("issue", {}).get("number")
        if number:
            return f"💬 {action.capitalize()} issue {link(f'{repo}#{number}', f'{url}/issues/{number}')}"
    if kind == "IssueCommentEvent" and action == "created":
        issue = payload.get("issue", {})
        number = issue.get("number")
        comment_id = payload.get("comment", {}).get("id")
        if number:
            target = f"{url}/issues/{number}"
            if comment_id:
                target += f"#issuecomment-{comment_id}"
            return f"💬 Commented on {link(f'{repo}#{number}', target)}"
    if kind == "PullRequestReviewEvent" and action == "created":
        number = payload.get("pull_request", {}).get("number")
        if number:
            return f"👀 Reviewed pull request {link(f'{repo}#{number}', f'{url}/pull/{number}')}"
    return None


def render_activity(events, limit=5, contribution_check=None):
    if contribution_check is None:
        contribution_check = has_historical_contribution
    contributed = {
        event["repo"]["name"].casefold()
        for event in events
        if event.get("public", False) and event["type"] in CONTRIBUTION_EVENTS
    }
    checked = {}
    rows = []
    seen = set()
    # Events can arrive out of order. Exclude this profile's own update commits.
    for event in sorted(events, key=lambda item: item["created_at"], reverse=True):
        if not event.get("public", False):
            continue
        if event["repo"]["name"].casefold() == f"{USERNAME}/{USERNAME}".casefold():
            continue
        summary = describe(event)
        if summary is None:
            continue
        if event["type"] == "WatchEvent":
            repo = event["repo"]["name"]
            key = repo.casefold()
            if key.split("/", 1)[0] == USERNAME.casefold() or key in contributed:
                continue
            if key not in checked:
                checked[key] = contribution_check(repo)
            if checked[key]:
                continue
        row = f"- `{event['created_at'][:10]}` · {summary}"
        if row not in seen:
            rows.append(row)
            seen.add(row)
        if len(rows) == limit:
            break
    return "\n".join(rows) or "No recent public activity. [Explore my repositories →](https://github.com/RayZh-hs?tab=repositories)"


def replace_activity(readme, activity):
    if readme.count(START) != 1 or readme.count(END) != 1:
        raise ValueError("README must contain exactly one pair of activity markers")
    before, rest = readme.split(START)
    _, after = rest.split(END)
    return f"{before}{START}\n{activity}\n{END}{after}"


def fetch_json(path, **params):
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": f"{USERNAME}-profile-activity",
        "X-GitHub-Api-Version": "2026-03-10",
    }
    if token := os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    request = Request(
        f"https://api.github.com/{path}?{urlencode(params)}",
        headers=headers,
    )
    with urlopen(request, timeout=30) as response:
        return json.load(response)


def fetch_events():
    events = fetch_json(f"users/{USERNAME}/events/public", per_page=100)
    if not isinstance(events, list):
        raise ValueError("GitHub returned an unexpected events response")
    return events


def has_historical_contribution(repo):
    """Check older public contributions that may no longer appear in the feed."""
    try:
        commits = fetch_json(f"repos/{quote(repo, safe='/')}/commits", author=USERNAME, per_page=1)
    except HTTPError as error:
        # An empty repository has no commits, but can still have issues or PRs.
        if error.code != 409:
            raise
        commits = []
    if not isinstance(commits, list):
        raise ValueError("GitHub returned an unexpected commits response")
    if commits:
        return True
    result = fetch_json(
        "search/issues",
        q=f"repo:{repo} (author:{USERNAME} OR commenter:{USERNAME} OR reviewed-by:{USERNAME})",
        advanced_search="true",
        per_page=1,
    )
    if not isinstance(result, dict) or "total_count" not in result:
        raise ValueError("GitHub returned an unexpected contribution search response")
    if result["total_count"]:
        return True
    if result.get("incomplete_results"):
        raise ValueError("GitHub returned an incomplete contribution search")
    return False


def main():
    original = README.read_text(encoding="utf-8")
    updated = replace_activity(original, render_activity(fetch_events()))
    if updated != original:
        README.write_text(updated, encoding="utf-8")
        print("Updated GitHub activity in README.md")
    else:
        print("GitHub activity is already up to date")


if __name__ == "__main__":
    main()
