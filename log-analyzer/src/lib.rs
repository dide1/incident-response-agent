use std::collections::HashSet;

#[derive(Debug, serde::Serialize)]
pub struct Analysis {
    pub failed_tests: Vec<String>,
    pub error_signatures: Vec<String>,
    pub stack_traces: Vec<Vec<String>>,
    pub line_count: usize,
}

/// Strip ANSI escape sequences (e.g., \x1b[31m color codes) from a string.
/// Walks char-by-char so it works without the `regex` crate.
pub fn strip_ansi(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    let mut chars = s.chars().peekable();
    while let Some(ch) = chars.next() {
        if ch == '\x1b' && chars.peek() == Some(&'[') {
            chars.next(); // consume '['
            for c in chars.by_ref() {
                if c.is_ascii_alphabetic() {
                    break; // command char ends the sequence
                }
            }
        } else {
            out.push(ch);
        }
    }
    out
}

/// Extract failed test names from common CI runners:
/// pytest, go test, cargo test, Jest.
pub fn extract_failed_tests(lines: &[&str]) -> Vec<String> {
    let mut tests: Vec<String> = Vec::new();
    let mut seen: HashSet<String> = HashSet::new();

    for line in lines {
        let t = line.trim();

        // pytest:  "FAILED tests/foo.py::test_bar - AssertionError"
        if let Some(rest) = t.strip_prefix("FAILED ") {
            let name = rest.split(" - ").next().unwrap_or(rest).trim().to_string();
            if !name.is_empty() && seen.insert(name.clone()) {
                tests.push(name);
            }

        // go test:  "--- FAIL: TestFoo (0.00s)"
        } else if let Some(rest) = t.strip_prefix("--- FAIL: ") {
            let name = rest.split_whitespace().next().unwrap_or(rest).to_string();
            if seen.insert(name.clone()) {
                tests.push(name);
            }

        // cargo test:  "test foo::bar ... FAILED"
        } else if t.starts_with("test ") && t.ends_with("FAILED") {
            let name = t
                .strip_prefix("test ").unwrap()
                .trim_end_matches("FAILED")
                .trim_end_matches("... ")
                .trim()
                .to_string();
            if !name.is_empty() && seen.insert(name.clone()) {
                tests.push(name);
            }

        // Jest:  "● Suite name > test name"
        } else if let Some(rest) = t.strip_prefix("● ") {
            let name = rest.trim().to_string();
            if !name.is_empty() && seen.insert(name.clone()) {
                tests.push(name);
            }
        }
    }

    tests
}

/// Extract distinct error signatures, deduped by the first 120 chars.
pub fn extract_error_signatures(lines: &[&str]) -> Vec<String> {
    const PREFIXES: &[&str] = &[
        "panic:", "Panic:", "PANIC:",
        "error:", "Error:", "ERROR:",
        "fatal:", "Fatal:", "FATAL:",
        "exception:", "Exception:",
        "AssertionError:", "AttributeError:", "TypeError:",
        "ValueError:", "RuntimeError:", "ImportError:",
        "KeyError:", "IndexError:", "PermissionError:",
        "thread '",   // Rust:  thread 'main' panicked at ...
        "E   ",       // pytest expanded error lines
    ];

    let mut sigs: Vec<String> = Vec::new();
    let mut seen: HashSet<String> = HashSet::new();

    for line in lines {
        let t = line.trim();
        if t.len() < 5 {
            continue;
        }
        if PREFIXES.iter().any(|p| t.starts_with(p)) {
            // Truncate key so near-identical lines (varying addresses/values) dedup
            let key: String = t.chars().take(120).collect();
            if seen.insert(key) {
                sigs.push(t.chars().take(200).collect());
            }
        }
    }

    sigs
}

/// Extract stack trace blocks: sequences of trace lines following an anchor.
/// Returns at most 5 blocks so the output stays manageable.
pub fn extract_stack_traces(lines: &[&str]) -> Vec<Vec<String>> {
    const ANCHORS: &[&str] = &[
        "Traceback (most recent call last)",
        "goroutine ",
        "stack backtrace",
        "thread '",
        "panic:",
    ];

    let is_anchor = |s: &str| ANCHORS.iter().any(|a| s.contains(a));

    let is_trace_line = |s: &str| {
        s.starts_with("  File \"")
            || s.starts_with("    at ")
            || s.starts_with("\tat ")
            || s.trim_start().starts_with("at ")
            || s.contains(".go:")
            || s.contains(".rs:")
            || s.contains(".py:")
            || s.contains(".ts:")
    };

    let mut traces: Vec<Vec<String>> = Vec::new();
    let mut current: Option<Vec<String>> = None;

    for line in lines {
        if is_anchor(line) {
            if let Some(block) = current.take() {
                if block.len() > 1 {
                    traces.push(block);
                }
            }
            current = Some(vec![line.to_string()]);
        } else if let Some(ref mut block) = current {
            if is_trace_line(line) || line.trim().is_empty() {
                block.push(line.to_string());
            } else {
                if block.len() > 1 {
                    traces.push(block.clone());
                }
                current = None;
            }
        }
    }
    if let Some(block) = current {
        if block.len() > 1 {
            traces.push(block);
        }
    }

    traces.truncate(5);
    traces
}

/// Analyze raw CI log text and return structured findings.
/// `tail` limits analysis to the last N lines (None = entire log).
pub fn analyze(raw: &str, tail: Option<usize>) -> Analysis {
    let clean = strip_ansi(raw);
    let all_lines: Vec<&str> = clean.lines().collect();
    let line_count = all_lines.len();

    let lines: Vec<&str> = match tail {
        Some(n) => {
            let start = all_lines.len().saturating_sub(n);
            all_lines[start..].to_vec()
        }
        None => all_lines,
    };

    Analysis {
        failed_tests: extract_failed_tests(&lines),
        error_signatures: extract_error_signatures(&lines),
        stack_traces: extract_stack_traces(&lines),
        line_count,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_strip_ansi_removes_color_codes() {
        assert_eq!(
            strip_ansi("\x1b[31mERROR\x1b[0m: something failed"),
            "ERROR: something failed"
        );
    }

    #[test]
    fn test_strip_ansi_passthrough_plain_text() {
        let plain = "no escape codes here";
        assert_eq!(strip_ansi(plain), plain);
    }

    #[test]
    fn test_strip_ansi_multiple_sequences() {
        assert_eq!(
            strip_ansi("\x1b[1m\x1b[31mFAILED\x1b[0m test_foo"),
            "FAILED test_foo"
        );
    }

    #[test]
    fn test_failed_tests_pytest() {
        let lines = vec![
            "FAILED tests/test_agent.py::test_blames_correct_commit - AssertionError",
            "FAILED tests/test_db.py::test_incident_scoping",
        ];
        let tests = extract_failed_tests(&lines);
        assert_eq!(tests.len(), 2);
        assert_eq!(tests[0], "tests/test_agent.py::test_blames_correct_commit");
        assert_eq!(tests[1], "tests/test_db.py::test_incident_scoping");
    }

    #[test]
    fn test_failed_tests_go() {
        let lines = vec![
            "--- FAIL: TestDedupCache (0.00s)",
            "--- FAIL: TestRateLimiter (0.01s)",
        ];
        let tests = extract_failed_tests(&lines);
        assert_eq!(tests, vec!["TestDedupCache", "TestRateLimiter"]);
    }

    #[test]
    fn test_failed_tests_cargo() {
        let lines = vec!["test parser::tests::test_strip_ansi ... FAILED"];
        let tests = extract_failed_tests(&lines);
        assert_eq!(tests, vec!["parser::tests::test_strip_ansi"]);
    }

    #[test]
    fn test_failed_tests_jest() {
        let lines = vec!["● Auth flow > redirects unauthenticated users"];
        let tests = extract_failed_tests(&lines);
        assert_eq!(tests, vec!["Auth flow > redirects unauthenticated users"]);
    }

    #[test]
    fn test_failed_tests_deduplication() {
        let lines = vec![
            "FAILED tests/test_foo.py::test_bar",
            "FAILED tests/test_foo.py::test_bar",
        ];
        let tests = extract_failed_tests(&lines);
        assert_eq!(tests.len(), 1);
    }

    #[test]
    fn test_error_signatures_dedup_identical_lines() {
        let lines = vec![
            "Error: connection refused (addr=localhost:5432)",
            "Error: connection refused (addr=localhost:5432)",
            "Error: timeout after 30s",
        ];
        let sigs = extract_error_signatures(&lines);
        assert_eq!(sigs.len(), 2);
    }

    #[test]
    fn test_error_signatures_pytest_expanded() {
        let lines = vec!["E   AssertionError: expected 200, got 500"];
        let sigs = extract_error_signatures(&lines);
        assert_eq!(sigs.len(), 1);
        assert!(sigs[0].contains("AssertionError"));
    }

    #[test]
    fn test_error_signatures_rust_panic() {
        let lines = vec!["thread 'main' panicked at 'index out of bounds', src/main.rs:42"];
        let sigs = extract_error_signatures(&lines);
        assert_eq!(sigs.len(), 1);
    }

    #[test]
    fn test_stack_trace_python() {
        let lines = vec![
            "Traceback (most recent call last):",
            "  File \"agent.py\", line 42, in run",
            "    result = call_api()",
            "ValueError: invalid token",
        ];
        let traces = extract_stack_traces(&lines);
        assert!(!traces.is_empty());
        assert!(traces[0][0].contains("Traceback"));
        assert!(traces[0].len() >= 2);
    }

    #[test]
    fn test_stack_traces_capped_at_five() {
        let mut lines: Vec<String> = Vec::new();
        for _ in 0..10 {
            lines.push("Traceback (most recent call last):".to_string());
            lines.push("  File \"x.py\", line 1, in f".to_string());
            lines.push("done".to_string());
        }
        let line_refs: Vec<&str> = lines.iter().map(|s| s.as_str()).collect();
        let traces = extract_stack_traces(&line_refs);
        assert!(traces.len() <= 5);
    }

    #[test]
    fn test_tail_limits_lines_analyzed() {
        let raw = "FAILED tests/early.py::old_test\nsome noise\nFAILED tests/late.py::new_test";
        let result = analyze(raw, Some(2));
        assert_eq!(result.line_count, 3);
        assert!(result.failed_tests.iter().all(|t| t.contains("late")));
    }

    #[test]
    fn test_no_tail_analyzes_all_lines() {
        let raw = "FAILED tests/early.py::old_test\nFAILED tests/late.py::new_test";
        let result = analyze(raw, None);
        assert_eq!(result.failed_tests.len(), 2);
    }
}
