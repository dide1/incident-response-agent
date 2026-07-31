/// Benchmark: sequential vs concurrent batch processing.
///
/// Generates 100 realistic synthetic CI log files, then times:
///   - Sequential:  process_batch with 1 worker  (baseline)
///   - Core count:  process_batch with N workers  (N = logical CPUs)
///   - 2× cores:    process_batch with 2N workers
///   - 3× cores:    process_batch with 3N workers
///
/// Each configuration runs RUNS times; the median wall-clock time is reported.
/// Files are generated once and reused across all configurations so I/O to
/// create them doesn't pollute the measurement.
use std::fs;
use std::path::PathBuf;
use std::time::{Duration, Instant};

const NUM_FILES: usize = 100;
const LINES_PER_FILE: usize = 600; // realistic CI log size
const RUNS: usize = 7;             // odd number → clean median

/// Build one synthetic CI log file with timestamps, ANSI codes, GHA
/// annotations, pytest/jest failures, an error signature, and a stack trace.
/// This is shaped like real GitHub Actions output so the parser actually works.
fn synthetic_log(file_index: usize, num_lines: usize) -> String {
    let mut lines: Vec<String> = Vec::with_capacity(num_lines);

    // Preamble: setup steps that look like real GHA output
    let ts = "2026-07-28T14:00:00.000Z";
    lines.push(format!("{ts} \x1b[36mRun pytest tests/ -v\x1b[0m"));
    lines.push(format!("{ts} \x1b[32m============================= test session starts ==============================\x1b[0m"));
    lines.push(format!("{ts} platform linux -- Python 3.12.3, pytest-8.2.0"));
    lines.push(format!("{ts} collected {num_lines} items"));
    lines.push(format!("{ts}"));

    // Passing tests (bulk of the file)
    let passing = num_lines.saturating_sub(20);
    for i in 0..passing {
        let padded = i % 60; // simulate the progress dots wrapping
        lines.push(format!(
            "{ts} \x1b[32mtests/test_module_{file_index}.py::test_case_{i}\x1b[0m ... \x1b[32mPASSED\x1b[0m"
        ));
    }

    // GHA annotation before failures (tests that the annotation stripper works)
    lines.push(format!(
        "{ts} ##[error]\x1b[31m=========================== short test summary info ============================\x1b[0m"
    ));

    // Failing tests — pytest format
    for i in 0..3 {
        lines.push(format!(
            "{ts} \x1b[31mFAILED\x1b[0m tests/test_module_{file_index}.py::test_failure_{i} - AssertionError: expected True, got False"
        ));
    }

    // Jest-style failure (mixed runner output)
    lines.push(format!(
        "{ts} \x1b[1m\x1b[31m● Auth flow › test_module_{file_index} › redirects unauthenticated users\x1b[0m"
    ));

    // Error signature block
    lines.push(format!("{ts} ##[error]AssertionError: expected 200 but got 401"));
    lines.push(format!("{ts}"));

    // Python stack trace
    lines.push(format!("{ts} Traceback (most recent call last):"));
    lines.push(format!("{ts}   File \"tests/test_module_{file_index}.py\", line 42, in test_failure_0"));
    lines.push(format!("{ts}     result = client.get('/protected')"));
    lines.push(format!("{ts}   File \"app/client.py\", line 17, in get"));
    lines.push(format!("{ts}     return self._request('GET', path)"));
    lines.push(format!("{ts} AssertionError: expected 200, got 401"));

    // gh CLI prefix variant (tests the tab-prefix timestamp stripper)
    lines.push(format!(
        "pytest\tRun pytest\t{ts} \x1b[31mFAILED\x1b[0m tests/test_module_{file_index}.py::test_with_tab_prefix - ValueError: bad input"
    ));

    // Pad to exactly num_lines with noise lines
    while lines.len() < num_lines {
        lines.push(format!("{ts} [verbose] worker {file_index} processing item {}", lines.len()));
    }
    lines.truncate(num_lines);
    lines.join("\n")
}

/// Write all synthetic files to a temp directory; return their paths.
fn generate_files(dir: &PathBuf) -> Vec<String> {
    fs::create_dir_all(dir).unwrap();
    (0..NUM_FILES)
        .map(|i| {
            let path = dir.join(format!("ci_log_{i:03}.log"));
            fs::write(&path, synthetic_log(i, LINES_PER_FILE)).unwrap();
            path.to_str().unwrap().to_string()
        })
        .collect()
}

/// Run process_batch with `workers` threads, `RUNS` times.
/// Returns sorted durations (for median), total files processed, and file list.
fn measure(paths: &[String], workers: usize) -> Vec<Duration> {
    let mut durations = Vec::with_capacity(RUNS);
    for _ in 0..RUNS {
        let p = paths.to_vec();
        let start = Instant::now();
        let results = log_analyzer::process_batch(p, workers, None);
        let elapsed = start.elapsed();
        // Sanity check: if any files were lost, the benchmark numbers are meaningless.
        assert_eq!(
            results.len(),
            NUM_FILES,
            "lost results with {workers} workers — concurrency bug"
        );
        durations.push(elapsed);
    }
    durations.sort();
    durations
}

fn median(sorted: &[Duration]) -> Duration {
    sorted[sorted.len() / 2]
}

fn throughput(dur: Duration, n: usize) -> f64 {
    n as f64 / dur.as_secs_f64()
}

fn main() {
    let cores: usize = std::thread::available_parallelism()
        .map(|n| n.get())
        .unwrap_or(4);

    let dir = std::env::temp_dir().join("log_analyzer_bench");
    println!("Generating {NUM_FILES} synthetic log files ({LINES_PER_FILE} lines each)...");
    let paths = generate_files(&dir);
    println!("Files written to {}", dir.display());
    println!();

    let labels = vec![
        format!("sequential (1 worker)"),
        format!("core count  ({cores} workers)"),
        format!("2× cores    ({} workers)", cores * 2),
        format!("3× cores    ({} workers)", cores * 3),
    ];
    let worker_counts = vec![1, cores, cores * 2, cores * 3];

    println!(
        "Running each configuration {RUNS}× and reporting the median.\n"
    );
    println!(
        "{:<30}  {:>10}  {:>14}  {:>12}  {:>12}",
        "Configuration", "Median", "Throughput", "Min", "Max"
    );
    println!("{}", "-".repeat(84));

    let mut rows: Vec<(String, Duration, f64)> = Vec::new();

    for (label, workers) in labels.iter().zip(worker_counts.iter()) {
        let sorted = measure(&paths, *workers);
        let med = median(&sorted);
        let tp = throughput(med, NUM_FILES);
        let min = sorted[0];
        let max = sorted[sorted.len() - 1];

        println!(
            "{:<30}  {:>10.1}ms  {:>11.1} f/s  {:>9.1}ms  {:>9.1}ms",
            label,
            med.as_secs_f64() * 1000.0,
            tp,
            min.as_secs_f64() * 1000.0,
            max.as_secs_f64() * 1000.0,
        );

        rows.push((label.clone(), med, tp));
    }

    println!();
    let seq_tp = rows[0].2;
    println!("Speedup over sequential:");
    for (label, _, tp) in &rows {
        println!("  {label:<30}  {:.2}×", tp / seq_tp);
    }

    println!();
    println!("Notes:");
    println!("  • Files: {NUM_FILES} × {LINES_PER_FILE} lines of synthetic GHA log output");
    println!("  • Logical CPUs: {cores}");
    println!("  • Runs per config: {RUNS} (median reported)");
    println!("  • parse_batch includes file I/O — I/O + CPU overlap is the speedup source");

    // Clean up temp files
    let _ = fs::remove_dir_all(&dir);
}
