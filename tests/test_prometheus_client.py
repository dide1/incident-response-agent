import json
from datetime import datetime, timedelta, timezone

import prometheus_client as pc


def _iso(seconds_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


class TestWindowFromSince:
    def test_none_falls_back_to_5m(self):
        assert pc._window_from_since(None) == "5m"

    def test_garbage_falls_back_to_5m(self):
        assert pc._window_from_since("not-a-timestamp") == "5m"

    def test_recent_alert_floors_at_45s(self):
        # 10s elapsed + 30s pad = 40s, below the 45s floor (3 scrape intervals)
        assert pc._window_from_since(_iso(10)) == "45s"

    def test_mid_range_uses_elapsed_plus_pad(self):
        window = pc._window_from_since(_iso(120))
        seconds = int(window.rstrip("s"))
        assert 145 <= seconds <= 155

    def test_old_alert_caps_at_600s(self):
        assert pc._window_from_since(_iso(7200)) == "600s"

    def test_z_suffix_accepted(self):
        iso_z = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        assert pc._window_from_since(iso_z) == "45s"


class _FakeResponse:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class TestQuery:
    def _patch(self, monkeypatch, payload):
        monkeypatch.setattr(
            pc.urllib.request, "urlopen", lambda *a, **k: _FakeResponse(payload)
        )

    def test_scalar_value_returned(self, monkeypatch):
        self._patch(monkeypatch, {"data": {"result": [{"value": [0, "42.5"]}]}})
        assert pc.query("up") == 42.5

    def test_empty_result_returns_none(self, monkeypatch):
        self._patch(monkeypatch, {"data": {"result": []}})
        assert pc.query("up") is None

    def test_nan_returns_none(self, monkeypatch):
        # division by zero in PromQL yields NaN, which is not JSON-serializable
        self._patch(monkeypatch, {"data": {"result": [{"value": [0, "NaN"]}]}})
        assert pc.query("a / b") is None

    def test_window_placeholder_substituted(self, monkeypatch):
        captured = {}

        def fake_urlopen(url, timeout=None):
            captured["url"] = url
            return _FakeResponse({"data": {"result": []}})

        monkeypatch.setattr(pc.urllib.request, "urlopen", fake_urlopen)
        pc.query("rate(x[{window}])", since=None)
        assert "%7Bwindow%7D" not in captured["url"]
        assert "5m" in captured["url"]
