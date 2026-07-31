use std::collections::{HashSet, VecDeque};
use std::fs;
use std::sync::{Arc, Condvar, Mutex};
use std::thread;

#[derive(Debug, serde::Serialize)]
pub struct Analysis {
    pub failed_tests: Vec<String>,
    pub error_signatures: Vec<String>,
    pub stack_traces: Vec<Vec<String>>,
    pub line_count: usize,
}

/// Strip GitHub Actions ISO timestamp from a log line.
/// Handles both bare lines ("2026-07-06T23:59:47.760Z content") and lines
/// with job/step prefixes that gh CLI prepends ("job\tstep\t2026-...Z content").
/// Scans up to 80 bytes to locate 'Z' followed by a space, with a 'T' within
/// 25 bytes before it (confirming an ISO 8601 timestamp).
fn strip_ci_timestamp(line: &str) -> &str {
    let b = line.as_bytes();
    for (i, &byte) in b.iter().enumerate().take(80) {
        if byte == b'Z' && i > 10 && i + 1 < b.len() && b[i + 1] == b' ' {
            let look_back = i.saturating_sub(25);
            if b[look_back..i].iter().any(|&c| c == b'T') {
                return &line[i + 1..];
            }
        }
    }
    line
}

/// Strip GitHub Actions problem-matcher annotations (##[error], ##[warning], ##[notice]).
fn strip_gha_annotation(line: &str) -> &str {
    for prefix in &["##[error]", "##[warning]", "##[notice]"] {
        if let Some(rest) = line.strip_prefix(prefix) {
            return rest;
        }
    }
    line
}

/// Strip both CI timestamp and GHA annotations from a log line.
pub fn normalize_line(line: &str) -> &str {
    strip_gha_annotation(strip_ci_timestamp(line).trim_start())
}

/// Strip ANSI escape sequences from a string.
/// Handles both real ESC bytes (\x1b[31m) and the gh CLI text representation (^[[31m).
/// Walks char-by-char so it works without the `regex` crate.
pub fn strip_ansi(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    let mut chars = s.chars().peekable();
    while let Some(ch) = chars.next() {
        if ch == '\x1b' && chars.peek() == Some(&'[') {
            // Real ANSI: ESC + [ + params + letter
            chars.next(); // consume '['
            for c in chars.by_ref() {
                if c.is_ascii_alphabetic() {
                    break;
                }
            }
        } else if ch == '^' && chars.peek() == Some(&'[') {
            // gh CLI text representation: ^[ maps to ESC, followed by [ + params + letter
            chars.next(); // consume first '['
            if chars.peek() == Some(&'[') {
                chars.next(); // consume second '[' (the actual ANSI bracket)
                for c in chars.by_ref() {
                    if c.is_ascii_alphabetic() {
                        break;
                    }
                }
            }
            // if not followed by a second '[', it's a literal ^[ — drop it (rare in CI logs)
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

        // Vitest:  " FAIL  src/__tests__/foo.ts > Suite > test name"  (after trim: "FAIL  src/...")
        } else if t.starts_with("FAIL ") && t.contains(" > ") {
            let name = t.find(" > ")
                .map(|pos| t[pos + 3..].trim().to_string())
                .unwrap_or_default();
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
    // Normalize: strip CI timestamps and ##[error] annotations so pattern
    // matching works regardless of the log runner's output format.
    let normalized: Vec<String> = clean.lines().map(|l| normalize_line(l).to_string()).collect();
    let all_lines: Vec<&str> = normalized.iter().map(|s| s.as_str()).collect();
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

// ─── Batch / concurrent processing ──────────────────────────────────────────

/// One file's result paired with the path that produced it.
/// `path` lets the caller match results back to inputs, because workers finish
/// in non-deterministic order — output order ≠ input order.
#[derive(Debug, serde::Serialize)]
pub struct BatchResult {
    pub path: String,
    pub analysis: Analysis,
}

/// Shared work queue state, bundled into one struct so a single Arc covers
/// everything a worker needs.
struct Queue {
    /// The actual work items. `Some(path)` = real job; `None` = poison pill
    /// (tells the receiving worker to exit its loop).
    items: Mutex<VecDeque<Option<String>>>,

    /// Workers wait on this when the queue is empty.
    /// WHY Condvar and not a spin loop: spinning wastes CPU and burns battery.
    /// Condvar.wait() hands the OS the thread until a signal arrives.
    not_empty: Condvar,

    /// Main thread waits on this when the queue is full (backpressure).
    /// WHY bounded: unbounded queues can exhaust memory if the producer is
    /// faster than consumers. Bounding to 2×workers keeps memory flat.
    not_full: Condvar,

    /// Maximum number of items allowed in the queue at once.
    capacity: usize,

    /// Counts how many times the main thread actually blocked on `not_full`.
    /// Compiled only in test mode — zero-cost in production.
    /// WHY: the backpressure code path can't be observed from outside the queue
    /// without instrumentation; a test that just "doesn't crash" is not a proof
    /// that the wait() branch was ever executed.
    #[cfg(test)]
    producer_waits: std::sync::atomic::AtomicUsize,
}

/// Inner implementation — takes an already-constructed `Arc<Queue>` so tests
/// can supply their own queue and inspect it after the run.
fn process_batch_inner(
    paths: Vec<String>,
    queue: Arc<Queue>,
    num_workers: usize,
    tail: Option<usize>,
) -> Vec<BatchResult> {
    // ── Result channel ───────────────────────────────────────────────────────
    let (tx, rx) = std::sync::mpsc::channel::<BatchResult>();

    // ── Spawn workers ────────────────────────────────────────────────────────
    let mut handles = Vec::with_capacity(num_workers);
    for _ in 0..num_workers {
        let q = Arc::clone(&queue);
        let tx = tx.clone();

        let handle = thread::spawn(move || {
            loop {
                let item = {
                    let mut guard = q.items.lock().unwrap();
                    while guard.is_empty() {
                        guard = q.not_empty.wait(guard).unwrap();
                    }
                    let item = guard.pop_front().unwrap();
                    q.not_full.notify_one();
                    item
                };

                match item {
                    None => break,
                    Some(path) => {
                        let result = fs::read_to_string(&path)
                            .map(|text| BatchResult {
                                path: path.clone(),
                                analysis: analyze(&text, tail),
                            })
                            .unwrap_or_else(|e| BatchResult {
                                path: path.clone(),
                                analysis: Analysis {
                                    failed_tests: vec![],
                                    error_signatures: vec![format!("read error: {}", e)],
                                    stack_traces: vec![],
                                    line_count: 0,
                                },
                            });
                        tx.send(result).unwrap();
                    }
                }
            }
        });

        handles.push(handle);
    }

    // Drop main's Sender clone now — rx.iter() ends only when ALL Senders drop.
    drop(tx);

    // ── Push work ────────────────────────────────────────────────────────────
    for path in paths {
        let mut guard = queue.items.lock().unwrap();
        while guard.len() >= queue.capacity {
            // Increment the wait counter before sleeping so tests can assert
            // this branch was reached. Zero-cost outside test builds.
            #[cfg(test)]
            queue.producer_waits.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
            guard = queue.not_full.wait(guard).unwrap();
        }
        guard.push_back(Some(path));
        queue.not_empty.notify_one();
    }

    // ── Push poison pills ────────────────────────────────────────────────────
    for _ in 0..num_workers {
        let mut guard = queue.items.lock().unwrap();
        while guard.len() >= queue.capacity {
            #[cfg(test)]
            queue.producer_waits.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
            guard = queue.not_full.wait(guard).unwrap();
        }
        guard.push_back(None);
        queue.not_empty.notify_one();
    }

    // ── Collect results then join ─────────────────────────────────────────────
    let results: Vec<BatchResult> = rx.iter().collect();
    for handle in handles {
        handle.join().unwrap();
    }
    results
}

/// Process a batch of log files concurrently using a fixed thread pool.
///
/// # Arguments
/// * `paths`       – file paths to process (order of results is not preserved)
/// * `num_workers` – number of parallel worker threads to spawn
/// * `tail`        – passed through to `analyze()`; limits lines examined
///
/// # Design
/// One shared `Arc<Queue>` (Mutex + two Condvars) dispatches work to workers.
/// Results flow back via `std::sync::mpsc` (multi-producer single-consumer):
/// each worker holds a Sender clone; main holds the Receiver.
///
/// Shutdown: after all paths are queued, main pushes one `None` per worker.
/// Each worker exits when it dequeues a `None`, dropping its Sender.
/// When all Senders are dropped, `receiver.iter()` ends automatically.
pub fn process_batch(
    paths: Vec<String>,
    num_workers: usize,
    tail: Option<usize>,
) -> Vec<BatchResult> {
    // Clamp to at least 1 worker so callers can pass 0 without panic.
    let num_workers = num_workers.max(1);

    // ── Shared work queue ────────────────────────────────────────────────────
    let queue = Arc::new(Queue {
        items: Mutex::new(VecDeque::new()),
        not_empty: Condvar::new(),
        not_full: Condvar::new(),
        // 2× workers: enough buffer that main can stay ahead of workers without
        // holding unbounded memory.
        capacity: num_workers * 2,
        #[cfg(test)]
        producer_waits: std::sync::atomic::AtomicUsize::new(0),
    });

    process_batch_inner(paths, queue, num_workers, tail)
}

/// Test-only wrapper: returns both results and the number of times the main
/// thread blocked on `not_full` (backpressure wait count).
#[cfg(test)]
fn process_batch_tracked(
    paths: Vec<String>,
    num_workers: usize,
    tail: Option<usize>,
) -> (Vec<BatchResult>, usize) {
    let num_workers = num_workers.max(1);
    let queue = Arc::new(Queue {
        items: Mutex::new(VecDeque::new()),
        not_empty: Condvar::new(),
        not_full: Condvar::new(),
        capacity: num_workers * 2,
        producer_waits: std::sync::atomic::AtomicUsize::new(0),
    });
    let results = process_batch_inner(paths, Arc::clone(&queue), num_workers, tail);
    let waits = queue.producer_waits.load(std::sync::atomic::Ordering::Relaxed);
    (results, waits)
}

// ─────────────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_strip_ci_timestamp_removes_prefix() {
        // Bare timestamp (no job/step prefix)
        let line = "2026-07-06T23:59:47.7612833Z  FAIL  src/foo.ts > suite > test";
        assert_eq!(strip_ci_timestamp(line), "  FAIL  src/foo.ts > suite > test");
    }

    #[test]
    fn test_strip_ci_timestamp_with_job_step_prefix() {
        // gh CLI prepends "job\tstep\t" before the timestamp
        let line = "test\tUNKNOWN STEP\t2026-07-06T23:59:47.762Z  FAIL  src/foo.ts > suite > test";
        assert_eq!(strip_ci_timestamp(line), "  FAIL  src/foo.ts > suite > test");
    }

    #[test]
    fn test_strip_ci_timestamp_passthrough_no_timestamp() {
        let line = "FAILED tests/foo.py::test_bar";
        assert_eq!(strip_ci_timestamp(line), line);
    }

    #[test]
    fn test_normalize_line_strips_gha_annotation() {
        let line = "2026-07-06T23:59:47.772Z ##[error]AssertionError: expected 0.5";
        assert_eq!(normalize_line(line), "AssertionError: expected 0.5");
    }

    #[test]
    fn test_failed_tests_vitest() {
        let lines = vec![
            " FAIL  src/__tests__/scoring.test.ts > computeDisplayScore > null aestheticScore defaults to 0.5 midpoint, not zero",
            " FAIL  src/__tests__/scoring.test.ts > computeDisplayScore > aesthetic score maps 1→0 and 10→1 linearly",
        ];
        let tests = extract_failed_tests(&lines);
        assert_eq!(tests.len(), 2);
        assert!(tests[0].contains("null aestheticScore"));
        assert!(tests[1].contains("aesthetic score maps"));
    }

    #[test]
    fn test_error_signatures_gha_annotation_stripped() {
        let raw = "2026-07-06T23:59:47.772Z ##[error]AssertionError: expected 0.5 to be close to 0.55";
        let result = analyze(raw, None);
        assert!(!result.error_signatures.is_empty(), "should detect AssertionError after stripping GHA prefix");
    }

    #[test]
    fn test_analyze_vitest_log() {
        let raw = concat!(
            "2026-07-06T23:59:47.760Z  FAIL  src/__tests__/scoring.test.ts > computeDisplayScore > null aestheticScore defaults to 0.5 midpoint, not zero\n",
            "2026-07-06T23:59:47.761Z  FAIL  src/__tests__/scoring.test.ts > computeDisplayScore > aesthetic score maps 1→0 and 10→1 linearly\n",
            "2026-07-06T23:59:47.772Z ##[error]AssertionError: expected 0.5 to be close to 0.55, received difference is 0.05\n",
        );
        let result = analyze(raw, None);
        assert_eq!(result.failed_tests.len(), 2, "should find 2 Vitest failures");
        assert!(!result.error_signatures.is_empty(), "should find AssertionError");
    }

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
    fn test_strip_ansi_gh_cli_text_format() {
        // gh CLI emits ^[[41m (literal ^+[) instead of real ESC bytes
        assert_eq!(
            strip_ansi("^[[41m^[[1m FAIL ^[[22m^[[49m src/foo.ts"),
            " FAIL  src/foo.ts"
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

// ─── Concurrency correctness tests ───────────────────────────────────────────
//
// These tests target the synchronization layer specifically, not the parsing
// logic. Each one is designed to catch a distinct class of concurrency bug.

#[cfg(test)]
mod concurrent_tests {
    use super::*;
    use std::collections::HashSet;

    /// Write N pytest-style failure lines to a temp file; return its path.
    /// Each test uses a unique tag in the filename to avoid collisions when
    /// cargo test runs multiple test threads simultaneously.
    fn make_log(tag: &str, num_failures: usize) -> String {
        let content: String = (0..num_failures)
            .map(|i| format!("FAILED tests/suite.py::test_{i} - AssertionError\n"))
            .collect();
        let path = std::env::temp_dir().join(format!("log_analyzer_test_{tag}.log"));
        fs::write(&path, content).unwrap();
        path.to_str().unwrap().to_string()
    }

    // ── Test 1a: more workers than files ─────────────────────────────────────
    // Catches bugs where extra workers consume phantom work or race on shutdown.
    // With 8 workers and 3 files, 5 workers will idle the entire run — their
    // poison pills must still be consumed without any worker eating two.
    #[test]
    fn test_all_results_present_more_workers_than_files() {
        let files: Vec<String> = (0..3)
            .map(|i| make_log(&format!("1a_{i}"), i + 1))
            .collect();
        let expected_paths: HashSet<String> = files.iter().cloned().collect();

        let results = process_batch(files, 8, None);

        assert_eq!(results.len(), 3, "must get exactly one result per file");

        let result_paths: HashSet<String> = results.iter().map(|r| r.path.clone()).collect();
        assert_eq!(result_paths, expected_paths, "result paths must match input paths exactly");

        // Verify no duplicates: if a result path appeared twice, the HashSet
        // would be smaller than the Vec.
        assert_eq!(
            results.len(),
            result_paths.len(),
            "no result may be duplicated"
        );
    }

    // ── Test 1b: fewer workers than files ─────────────────────────────────────
    // Catches bugs where the queue drains before all files are processed (e.g.,
    // wrong poison pill count, workers exiting too early).
    #[test]
    fn test_all_results_present_fewer_workers_than_files() {
        let files: Vec<String> = (0..6)
            .map(|i| make_log(&format!("1b_{i}"), i + 1))
            .collect();
        let expected_paths: HashSet<String> = files.iter().cloned().collect();

        let results = process_batch(files, 2, None);

        assert_eq!(results.len(), 6, "all 6 files must produce a result");
        let result_paths: HashSet<String> = results.iter().map(|r| r.path.clone()).collect();
        assert_eq!(result_paths, expected_paths);
        assert_eq!(results.len(), result_paths.len(), "no duplicates");
    }

    // ── Test 2: backpressure actually engages ─────────────────────────────────
    // This test is the one most likely to be faked: a naive implementation that
    // ignores queue capacity will still produce correct results — it just burns
    // unbounded memory. We verify the wait() branch actually executed by reading
    // the #[cfg(test)] producer_waits counter from the Queue.
    //
    // Setup: 1 worker → capacity = 2. We push 20 files. Main cannot push more
    // than 2 items before a worker drains one, so it MUST block at least 18
    // times. Files have 50 lines each so analyze() takes non-trivial CPU time,
    // making it unlikely the worker outpaces main and empties the queue
    // before the 3rd push.
    #[test]
    fn test_backpressure_actually_engages() {
        let files: Vec<String> = (0..20)
            .map(|i| make_log(&format!("bp_{i}"), 50))
            .collect();

        let (results, waits) = process_batch_tracked(files, 1, None);

        assert_eq!(results.len(), 20, "all files must complete despite backpressure");
        assert!(
            waits > 0,
            "main thread must have blocked on not_full at least once \
             (capacity=2, 20 items pushed — expected ≥1 wait, got 0). \
             If this fires, the not_full code path was never reached, meaning \
             the queue is effectively unbounded."
        );
    }

    // ── Test 3: stress — shutdown correctness under load ──────────────────────
    // 100 files, 4 workers. Tests that all threads join cleanly with no hang
    // or panic. A deadlock would cause cargo test to time out; a panic would
    // surface via join().unwrap() inside process_batch_inner.
    //
    // Also re-verifies no lost/duplicated results at scale.
    #[test]
    fn test_stress_no_deadlock_no_panic_all_results_present() {
        let n = 100;
        let files: Vec<String> = (0..n)
            .map(|i| make_log(&format!("stress_{i}"), 3))
            .collect();
        let expected_paths: HashSet<String> = files.iter().cloned().collect();

        let results = process_batch(files, 4, None);

        assert_eq!(results.len(), n, "all {n} files must produce a result");

        let result_paths: HashSet<String> = results.iter().map(|r| r.path.clone()).collect();
        assert_eq!(
            result_paths, expected_paths,
            "every input path must appear in output exactly once"
        );
        assert_eq!(results.len(), result_paths.len(), "no duplicates at scale");
    }

    // ── Test 4: single worker (degenerate case) ───────────────────────────────
    // num_workers=1 means exactly one poison pill is pushed. If the pill logic
    // uses the wrong count, the worker either never exits (too few pills) or a
    // pill is wasted and a real job is lost (too many pills processed by worker).
    #[test]
    fn test_single_worker_processes_all_files() {
        let files: Vec<String> = (0..5)
            .map(|i| make_log(&format!("1w_{i}"), i + 1))
            .collect();
        let expected_paths: HashSet<String> = files.iter().cloned().collect();

        let results = process_batch(files, 1, None);

        assert_eq!(results.len(), 5, "single worker must process all 5 files");
        let result_paths: HashSet<String> = results.iter().map(|r| r.path.clone()).collect();
        assert_eq!(result_paths, expected_paths);
    }

    // ── Test 5: zero workers clamped to one ───────────────────────────────────
    // Caller passes 0; the clamp to max(1) must prevent a panic (spawning 0
    // workers then pushing 1 pill would leave it unconsumed, blocking forever).
    #[test]
    fn test_zero_workers_clamped_to_one() {
        let files = vec![make_log("0w", 1)];
        let results = process_batch(files, 0, None);
        assert_eq!(results.len(), 1, "clamped-to-1 worker must still process the file");
    }
}
