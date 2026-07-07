import pytest

import ci_poller


@pytest.fixture(autouse=True)
def reset_poller_state(monkeypatch):
    """Poller state is module-level; isolate every test."""
    monkeypatch.setattr(ci_poller, "_seen_failed_runs", set())
    monkeypatch.setattr(ci_poller, "_open_incidents", {})
    monkeypatch.setattr(ci_poller, "_seeded", False)
    monkeypatch.setattr(
        ci_poller, "configured_repos", lambda: {"loupe": "dide1/loupe"}
    )


def _run(run_id, conclusion, status="completed", name="ci", branch="main"):
    return {
        "id": run_id,
        "status": status,
        "conclusion": conclusion,
        "name": name,
        "head_branch": branch,
        "head_sha": "e" * 40,
        "html_url": f"https://github.com/dide1/loupe/actions/runs/{run_id}",
        "run_started_at": "2026-07-06T00:00:00Z",
    }


def _patch_runs(monkeypatch, runs):
    monkeypatch.setattr(ci_poller, "_fetch_recent_runs", lambda repo: runs)


class TestSeeding:
    def test_first_poll_never_alerts(self, monkeypatch):
        _patch_runs(monkeypatch, [_run(1, "failure")])
        alerts, resolutions = ci_poller._poll_once()
        assert alerts == []
        assert resolutions == []

    def test_preexisting_failure_not_replayed_after_seeding(self, monkeypatch):
        _patch_runs(monkeypatch, [_run(1, "failure")])
        ci_poller._poll_once()  # seed
        alerts, _ = ci_poller._poll_once()  # same run again
        assert alerts == []


class TestFailureDetection:
    def test_new_failure_after_seeding_alerts(self, monkeypatch):
        _patch_runs(monkeypatch, [])
        ci_poller._poll_once()  # seed on empty history
        _patch_runs(monkeypatch, [_run(2, "failure", branch="feat/x")])
        alerts, _ = ci_poller._poll_once()
        assert len(alerts) == 1
        ctx = alerts[0]
        assert ctx["alertname"] == "CIFailure"
        assert ctx["service"] == "loupe"
        assert "feat/x" in ctx["description"]
        assert "Run ID: 2" in ctx["description"]

    def test_in_progress_runs_ignored(self, monkeypatch):
        _patch_runs(monkeypatch, [])
        ci_poller._poll_once()
        _patch_runs(monkeypatch, [_run(3, None, status="in_progress")])
        alerts, _ = ci_poller._poll_once()
        assert alerts == []

    def test_same_failure_alerts_once(self, monkeypatch):
        _patch_runs(monkeypatch, [])
        ci_poller._poll_once()
        _patch_runs(monkeypatch, [_run(4, "failure")])
        first, _ = ci_poller._poll_once()
        second, _ = ci_poller._poll_once()
        assert len(first) == 1
        assert second == []


class TestResolution:
    def test_success_on_alerted_workflow_resolves(self, monkeypatch):
        _patch_runs(monkeypatch, [])
        ci_poller._poll_once()
        _patch_runs(monkeypatch, [_run(5, "failure", name="ci", branch="main")])
        ci_poller._poll_once()
        _patch_runs(monkeypatch, [_run(6, "success", name="ci", branch="main")])
        _, resolutions = ci_poller._poll_once()
        assert resolutions == [("CIFailure", "loupe")]

    def test_success_without_open_incident_is_silent(self, monkeypatch):
        _patch_runs(monkeypatch, [_run(7, "success")])
        ci_poller._poll_once()  # seed
        _, resolutions = ci_poller._poll_once()
        assert resolutions == []

    def test_success_on_different_branch_does_not_resolve(self, monkeypatch):
        _patch_runs(monkeypatch, [])
        ci_poller._poll_once()
        _patch_runs(monkeypatch, [_run(8, "failure", branch="feat/x")])
        ci_poller._poll_once()
        _patch_runs(monkeypatch, [_run(9, "success", branch="main")])
        _, resolutions = ci_poller._poll_once()
        assert resolutions == []
