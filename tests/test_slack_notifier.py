from slack_notifier import _fmt_ts, build_slack_blocks

ALERT = {
    "alertname": "HighErrorRate",
    "service": "payments-service",
    "severity": "critical",
    "starts_at": "2026-07-06T22:02:34+00:00",
}

RESULT = {
    "confidence": "high",
    "likely_commit": {
        "sha": "cccc222222222222222222222222222222222222",
        "author": "bob@example.com",
        "message": "feat: switch to ExternalPaymentGateway v2",
        "deployed_at": "2026-07-06T22:02:00+00:00",
    },
    "error_match": "Retry block removed; every gateway error now propagates uncaught.",
    "impact": {
        "error_rate_pct": 91,
        "requests_per_min": 184,
        "failed_per_min": 167,
        "p99_latency_s": None,
    },
    "runbook": {
        "filename": "payment-gateway-timeout.md",
        "title": "Payment Gateway Timeout / Failure",
        "summary": (
            "Roll back the deploy immediately. Re-add the retry block before redeploying. "
            "Then check the provider status page. Finally write a regression test."
        ),
    },
}


def _texts(blocks):
    out = []
    for b in blocks:
        if "text" in b and isinstance(b["text"], dict):
            out.append(b["text"]["text"])
        for el in b.get("elements", []):
            out.append(el.get("text", ""))
    return "\n".join(out)


class TestHeader:
    def test_header_contains_severity_alert_service(self):
        blocks = build_slack_blocks(ALERT, RESULT)
        header = next(b for b in blocks if b["type"] == "header")
        assert "CRITICAL" in header["text"]["text"]
        assert "HighErrorRate" in header["text"]["text"]
        assert "payments-service" in header["text"]["text"]


class TestImpactStrip:
    def test_real_metrics_rendered(self):
        text = _texts(build_slack_blocks(ALERT, RESULT))
        assert "*91%* error rate" in text
        assert "~167 failed req/min" in text
        assert "184 total req/min" in text

    def test_null_latency_omitted(self):
        text = _texts(build_slack_blocks(ALERT, RESULT))
        assert "P99" not in text

    def test_all_null_metrics_says_unavailable(self):
        result = {**RESULT, "impact": {
            "error_rate_pct": None, "requests_per_min": None,
            "failed_per_min": None, "p99_latency_s": None,
        }}
        assert "_metrics unavailable_" in _texts(build_slack_blocks(ALERT, result))

    def test_missing_impact_block_says_unavailable(self):
        result = {k: v for k, v in RESULT.items() if k != "impact"}
        assert "_metrics unavailable_" in _texts(build_slack_blocks(ALERT, result))


class TestCommitAttribution:
    def test_short_sha_and_author_present(self):
        text = _texts(build_slack_blocks(ALERT, RESULT))
        assert "cccc2222" in text
        assert "cccc22222222222222" not in text  # full sha not dumped
        assert "bob@example.com" in text
        assert "high confidence" in text

    def test_null_commit_states_no_deploys(self):
        result = {**RESULT, "likely_commit": None}
        text = _texts(build_slack_blocks(ALERT, result))
        assert "No recent deploys found" in text


class TestRunbookTruncation:
    def test_summary_capped_at_two_sentences(self):
        text = _texts(build_slack_blocks(ALERT, RESULT))
        assert "Roll back the deploy immediately" in text
        assert "Re-add the retry block" in text
        # sentences 3 and 4 are cut
        assert "provider status page" not in text
        assert "regression test" not in text

    def test_filename_reference_present(self):
        text = _texts(build_slack_blocks(ALERT, RESULT))
        assert "payment-gateway-timeout.md" in text


class TestFmtTs:
    def test_iso_renders_hhmm_utc(self):
        assert _fmt_ts("2026-07-06T22:02:34+00:00") == "22:02 UTC"

    def test_none_is_unknown(self):
        assert _fmt_ts(None) == "unknown"

    def test_garbage_passes_through(self):
        assert _fmt_ts("whenever") == "whenever"
