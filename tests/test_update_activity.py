import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from scripts import update_activity as activity


def event(kind="PushEvent", repo="RayZh-hs/flow-wm", date="2026-11-08T12:00:00Z", **payload):
    return {
        "type": kind,
        "repo": {"name": repo},
        "created_at": date,
        "public": True,
        "payload": payload,
    }


class ActivityTests(unittest.TestCase):
    def test_hides_stars_on_owned_repositories_but_keeps_pushes(self):
        check = Mock(side_effect=AssertionError("Owned repos need no lookup"))
        result = activity.render_activity([
            event(kind="WatchEvent", repo="rayzh-HS/Flow-WM", action="started"),
            event(repo="RayZh-hs/flow-wm"),
        ], contribution_check=check)
        self.assertNotIn("Starred", result)
        self.assertIn("Pushed to", result)

    def test_hides_stars_on_repos_with_older_contributions_in_the_feed(self):
        check = Mock(side_effect=AssertionError("Known contributions need no lookup"))
        for kind in activity.CONTRIBUTION_EVENTS:
            with self.subTest(kind=kind):
                result = activity.render_activity([
                    event(kind="WatchEvent", repo="Other/Project", action="started"),
                    event(kind=kind, repo="other/project", date="2026-11-01T12:00:00Z"),
                ], limit=1, contribution_check=check)
                self.assertNotIn("Starred", result)

    def test_hides_stars_on_historical_contributions_and_backfills_list(self):
        check = Mock(return_value=True)
        result = activity.render_activity([
            event(kind="WatchEvent", repo="Other/OldProject", action="started"),
            event(date="2026-11-07T12:00:00Z"),
        ], limit=1, contribution_check=check)
        self.assertNotIn("Starred", result)
        self.assertIn("Pushed to", result)
        check.assert_called_once_with("Other/OldProject")

    def test_keeps_unrelated_stars_and_checks_each_repo_once(self):
        check = Mock(return_value=False)
        result = activity.render_activity([
            event(kind="WatchEvent", repo="Other/Project", action="started"),
            event(kind="WatchEvent", repo="other/project", action="started", date="2026-11-07T12:00:00Z"),
            event(kind="ForkEvent", repo="Other/Project", date="2026-11-06T12:00:00Z"),
        ], contribution_check=check)
        self.assertEqual(result.count("Starred"), 2)
        check.assert_called_once_with("Other/Project")

    def test_private_events_do_not_mark_public_repos_as_contributed(self):
        private = event(repo="Other/Project")
        private["public"] = False
        result = activity.render_activity([
            private,
            event(kind="WatchEvent", repo="Other/Project", action="started"),
        ], contribution_check=Mock(return_value=False))
        self.assertIn("Starred", result)

    def test_detects_historical_commits_and_issue_or_pr_participation(self):
        for responses, expected in [
            ([[{"sha": "old-commit"}]], True),
            ([[], {"total_count": 1, "incomplete_results": False}], True),
            ([[], {"total_count": 0, "incomplete_results": False}], False),
        ]:
            with self.subTest(responses=responses):
                with patch.object(activity, "fetch_json", side_effect=responses):
                    self.assertEqual(activity.has_historical_contribution("Other/Project"), expected)

    def test_empty_repository_can_still_have_issue_contributions(self):
        empty_repo = HTTPError("https://api.github.com", 409, "Git Repository is empty", {}, None)
        with patch.object(activity, "fetch_json", side_effect=[empty_repo, {"total_count": 1}]):
            self.assertTrue(activity.has_historical_contribution("Other/Project"))

    def test_incomplete_or_failed_lookups_are_not_treated_as_no_contributions(self):
        with patch.object(activity, "fetch_json", side_effect=[[], {"total_count": 0, "incomplete_results": True}]):
            with self.assertRaises(ValueError):
                activity.has_historical_contribution("Other/Project")
        with patch.object(activity, "fetch_json", side_effect=OSError("API unavailable")):
            with self.assertRaises(OSError):
                activity.has_historical_contribution("Other/Project")

    def test_sorts_deduplicates_and_limits_rows(self):
        older = event(repo="RayZh-hs/older", date="2026-11-06T12:00:00Z")
        newer = event(repo="RayZh-hs/newer")
        duplicate = event(repo="RayZh-hs/newer", date="2026-11-08T13:00:00Z")
        oldest = event(repo="RayZh-hs/oldest", date="2026-11-05T12:00:00Z")
        result = activity.render_activity([older, newer, oldest, duplicate], limit=2)
        self.assertEqual(len(result.splitlines()), 2)
        self.assertIn("RayZh-hs/newer", result.splitlines()[0])
        self.assertIn("RayZh-hs/older", result.splitlines()[1])

    def test_excludes_private_profile_and_unsupported_events(self):
        private = event(repo="RayZh-hs/private")
        private["public"] = False
        result = activity.render_activity([
            private,
            event(repo="rayzh-HS/rayzh-HS"),
            event(kind="DeleteEvent"),
            event(kind="IssuesEvent", action="assigned", issue={"number": 1}),
            event(repo="RayZh-hs/visible"),
        ])
        self.assertEqual(len(result.splitlines()), 1)
        self.assertIn("RayZh-hs/visible", result)

    def test_push_links_to_commit_without_requiring_legacy_commit_list(self):
        result = activity.describe(event(head="abc123"))
        self.assertIn("https://github.com/RayZh-hs/flow-wm/commit/abc123", result)
        self.assertNotIn("commit/", activity.describe(event()))

    def test_handles_merged_and_sparse_pull_request_payloads(self):
        merged = activity.describe(event(
            kind="PullRequestEvent", action="closed", number=42,
            pull_request={"merged": True},
        ))
        self.assertIn("Merged pull request", merged)
        self.assertIn("/pull/42", merged)
        opened = activity.describe(event(kind="PullRequestEvent", action="opened", number=43))
        self.assertIn("Opened pull request", opened)
        self.assertIn("/pull/43", opened)

    def test_comment_links_to_specific_comment(self):
        result = activity.describe(event(
            kind="IssueCommentEvent", action="created",
            issue={"number": 35}, comment={"id": 123},
        ))
        self.assertIn("/issues/35#issuecomment-123", result)

    def test_branch_names_cannot_break_markdown(self):
        result = activity.describe(event(
            kind="CreateEvent", ref_type="branch", ref="feat/[a]*_<b>(c)",
        ))
        self.assertIn("/tree/feat%2F%5Ba%5D%2A_%3Cb%3E%28c%29", result)
        self.assertNotIn("<b>", result)
        self.assertNotIn("[a]", result)

    def test_empty_feed_has_a_useful_fallback(self):
        self.assertIn("No recent public activity", activity.render_activity([]))
        self.assertIn("?tab=repositories", activity.render_activity([]))

    def test_replaces_only_the_marked_section_and_is_idempotent(self):
        readme = f"Bio\n{activity.START}\nOld activity\n{activity.END}\nToolkit\n"
        updated = activity.replace_activity(readme, "New activity")
        self.assertEqual(updated, f"Bio\n{activity.START}\nNew activity\n{activity.END}\nToolkit\n")
        self.assertEqual(activity.replace_activity(updated, "New activity"), updated)

    def test_rejects_missing_duplicate_and_reversed_markers(self):
        for readme in ["No markers", activity.START * 2 + activity.END, activity.END + activity.START]:
            with self.subTest(readme=readme), self.assertRaises(ValueError):
                activity.replace_activity(readme, "New activity")

    def test_api_failure_leaves_readme_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            readme = Path(directory) / "README.md"
            original = f"Bio\n{activity.START}\nExisting activity\n{activity.END}\n"
            readme.write_text(original, encoding="utf-8")
            with patch.object(activity, "README", readme):
                with patch.object(activity, "fetch_events", side_effect=OSError("API unavailable")):
                    with self.assertRaises(OSError):
                        activity.main()
            self.assertEqual(readme.read_text(encoding="utf-8"), original)

    def test_fetches_only_public_events_and_passes_token(self):
        with patch.dict("os.environ", {"GITHUB_TOKEN": "test-token"}):
            with patch.object(activity, "urlopen") as request:
                request.return_value.__enter__.return_value.read.return_value = json.dumps([]).encode()
                self.assertEqual(activity.fetch_events(), [])
        sent = request.call_args.args[0]
        self.assertIn("/users/RayZh-hs/events/public?", sent.full_url)
        self.assertEqual(sent.get_header("Authorization"), "Bearer test-token")


if __name__ == "__main__":
    unittest.main()
