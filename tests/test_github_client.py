import github_client as gh


class TestConfiguredRepos:
    def test_parses_comma_separated_list(self, monkeypatch):
        monkeypatch.setenv("GITHUB_REPOS", "dide1/loupe, dide1/incident-response-agent")
        assert gh.configured_repos() == {
            "loupe": "dide1/loupe",
            "incident-response-agent": "dide1/incident-response-agent",
        }

    def test_empty_env_means_no_repos(self, monkeypatch):
        monkeypatch.setenv("GITHUB_REPOS", "")
        assert gh.configured_repos() == {}

    def test_entries_without_slash_ignored(self, monkeypatch):
        monkeypatch.setenv("GITHUB_REPOS", "loupe,dide1/loupe")
        assert gh.configured_repos() == {"loupe": "dide1/loupe"}


def _fake_commit(sha, message="fix: something", email="a@b.com"):
    return {
        "sha": sha,
        "commit": {
            "author": {"date": "2026-07-06T00:00:00Z", "email": email},
            "message": message,
        },
    }


class TestListRecentCommits:
    def test_branch_scopes_the_query(self, monkeypatch):
        # The GitHub commits API defaults to the default branch; a CI failure on
        # a feature branch is invisible without the sha= param. Regression test
        # for the live loupe misattribution.
        monkeypatch.setenv("GITHUB_REPOS", "dide1/loupe")
        paths = []

        def fake_get_json(path):
            paths.append(path)
            return [_fake_commit("a" * 40)]

        monkeypatch.setattr(gh, "_get_json", fake_get_json)
        gh.list_recent_commits("loupe", 90, branch="test/agent-ci-detection")
        assert "sha=test%2Fagent-ci-detection" in paths[0]

    def test_no_branch_means_no_sha_param(self, monkeypatch):
        monkeypatch.setenv("GITHUB_REPOS", "dide1/loupe")
        paths = []

        def fake_get_json(path):
            paths.append(path)
            return [_fake_commit("a" * 40)]

        monkeypatch.setattr(gh, "_get_json", fake_get_json)
        gh.list_recent_commits("loupe", 90)
        assert "sha=" not in paths[0]

    def test_row_shape_matches_deploy_tracker(self, monkeypatch):
        monkeypatch.setenv("GITHUB_REPOS", "dide1/loupe")
        monkeypatch.setattr(
            gh, "_get_json",
            lambda p: [_fake_commit("b" * 40, message="feat: x\n\nlong body")],
        )
        rows = gh.list_recent_commits("loupe", 90)
        assert rows[0]["sha"] == "b" * 40
        assert rows[0]["service"] == "loupe"
        assert rows[0]["commit_message"] == "feat: x"  # first line only
        assert "is_fault" not in rows[0]  # the answer-key leak stays fixed

    def test_unconfigured_service_returns_empty(self, monkeypatch):
        monkeypatch.setenv("GITHUB_REPOS", "dide1/loupe")
        assert gh.list_recent_commits("unknown-service", 90) == []


class TestDiffTruncation:
    def test_long_diff_truncated_with_marker(self, monkeypatch):
        monkeypatch.setenv("GITHUB_REPOS", "dide1/loupe")
        big = "diff --git a/x b/x\n" + "x" * 10_000
        monkeypatch.setattr(gh, "_request", lambda *a, **k: big.encode())
        gh._sha_repo_cache["c" * 40] = "dide1/loupe"
        row = gh.fetch_commit_diff("c" * 40)
        assert len(row["diff"]) < 6200
        assert "diff truncated" in row["diff"]

    def test_short_diff_untouched(self, monkeypatch):
        monkeypatch.setenv("GITHUB_REPOS", "dide1/loupe")
        small = "diff --git a/x b/x\n- old\n+ new\n"
        monkeypatch.setattr(gh, "_request", lambda *a, **k: small.encode())
        gh._sha_repo_cache["d" * 40] = "dide1/loupe"
        assert gh.fetch_commit_diff("d" * 40)["diff"] == small
