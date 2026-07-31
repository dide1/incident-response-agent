# log-analyzer

Strips noise from raw GitHub Actions logs (ANSI escape codes, ISO timestamps,
`##[error]` annotations, `gh` CLI job/step prefixes) and extracts structured
signal: failed test names, error signatures, and stack traces. Output is JSON.

## Usage

**Single file or stdin (original mode):**
```bash
# from a file
./log-analyzer path/to/ci.log

# from stdin (e.g. piped from gh CLI)
gh run view 12345 --log-failed | ./log-analyzer --all

# limit analysis to last N lines (default: 150)
./log-analyzer --tail 200 path/to/ci.log
```

**Concurrent batch mode:**
```bash
./log-analyzer --concurrent 10 log1.log log2.log log3.log ...
```
Processes all files in parallel using a fixed pool of N worker threads.
Output is a JSON array, one object per file, in completion order.

---

## Concurrent batch processing

### Problem

The incident response agent polls multiple repos every 60 seconds. When
several CI runs fail simultaneously, it needs to analyze all their logs
before generating a response. Processing them sequentially adds latency
proportional to the number of failures; concurrent processing keeps total
time close to the cost of the slowest single file.

### Design

A bounded work queue distributes file paths to a fixed thread pool.
Results flow back on a separate channel.

```
Main thread                          Worker threads (N)
──────────────────────────────────   ──────────────────────────────────
push path → Arc<Mutex<VecDeque>>  ←→  pop path → read file → analyze()
wait on not_full if queue full        signal not_full after each pop
push None poison pills (×N)          exit loop on None
                                      send BatchResult → mpsc::Sender
drop mpsc::Sender                  ←  mpsc::Sender dropped on exit
collect rx.iter() until all done
join all threads
```

**Work queue:** `Arc<(Mutex<VecDeque<Option<String>>>, Condvar, Condvar)>`

- `Mutex<VecDeque>` — single lock protecting the shared queue. Held only
  while pushing or popping, never during file I/O or parsing.
- `not_empty` Condvar — workers sleep here when the queue is empty; main
  signals it after each push. Workers use a `while`-loop (not `if`) to
  guard against spurious wakeups.
- `not_full` Condvar — main sleeps here when the queue is at capacity
  (`2 × num_workers`); workers signal it after each pop. Bounds memory
  when the producer outruns consumers.

**Results channel:** `std::sync::mpsc`

Multi-producer single-consumer is the right primitive: each worker holds a
`Sender` clone and sends independently without contention. Main holds the
`Receiver`. When all workers exit (consuming their poison pill), all
`Sender`s drop and `rx.iter()` ends automatically — no explicit "done"
signal needed.

**Shutdown:** one `None` per worker, pushed after all real work. Each
worker consumes exactly one and exits. Fewer pills → workers sleep forever.
More pills → a worker could consume two and exit before processing its last
real job (if it raced ahead of another worker's consumption). Exactly N is
the only safe count.

**Deadlock prevention:** one lock in the system, never held during
`analyze()`. Condvar.wait() atomically releases the lock and sleeps —
there is no window where the lock is released but the thread hasn't started
waiting, which would allow a wakeup to be missed before the sleep begins.

### Why `std::sync` only, no Rayon or Crossbeam

The implementation uses only `std::thread` and `std::sync`. This was
intentional: the goal was to build the primitive from scratch, not wrap an
abstraction. Understanding why a `while` loop is required around `wait()`,
why exactly N poison pills are needed, and why the lock must be released
before calling `analyze()` is more valuable than knowing a crate's API.

---

## Correctness tests

Six tests in `concurrent_tests` target the synchronization layer specifically,
not the parsing logic.

| Test | What it catches |
|------|----------------|
| More workers than files (8 workers, 3 files) | Extra idle workers consuming phantom work; wrong shutdown pill count |
| Fewer workers than files (2 workers, 6 files) | Workers exiting before all files are processed |
| Backpressure engages | `not_full.wait()` is actually reached, not just "doesn't crash" |
| Stress: 100 files, 4 workers | Deadlock (test times out), panics (surface via `join().unwrap()`) |
| Single worker | Poison pill off-by-one at the boundary: exactly 1 pill must be pushed |
| Zero workers clamped to 1 | `max(1)` guard: 0 workers → 0 pills → deadlock without the clamp |

**On the backpressure test specifically:** a naive implementation that ignores
queue capacity still produces correct results — it just uses unbounded memory.
A test that only checks the output count doesn't prove the wait branch was
ever reached. The implementation includes a `#[cfg(test)]`
`AtomicUsize` counter on the queue that increments each time the main thread
enters `not_full.wait()`. The test asserts this counter is `> 0` after a run
with 1 worker (capacity 2) and 20 files — a configuration where the main
thread *must* block at least 18 times. The counter is zero-cost in production
builds; it doesn't exist in the compiled binary.

---

## Benchmark

**Setup:** 100 synthetic CI log files, 600 lines each, shaped like real
GitHub Actions output (ANSI codes, ISO timestamps, GHA annotations, pytest
and Jest failure lines, stack traces). 7 runs per configuration; median
reported. Machine: Apple M-series, 10 logical CPUs.

**Important caveat up front:** these are small files (~30 KB each). At this
size, the bottleneck is CPU (parsing), not I/O — `fs::read_to_string` returns
nearly instantly and `analyze()` dominates. Real CI logs are often 1–10 MB
per job, where `read_to_string` blocks long enough for another thread to run
on the same core. For large files, the sweet spot would likely shift toward
more workers than cores, since I/O wait overlap becomes the dominant gain.
These numbers reflect the CPU-bound case.

### Results

| Configuration | Workers | Median | Throughput | Speedup |
|---|---|---|---|---|
| Sequential | 1 | 41.5 ms | 2,411 f/s | 1.00× |
| Core count | 10 | 5.5 ms | 18,317 f/s | 7.60× |
| 2× cores | 20 | 5.3 ms | 18,946 f/s | 7.86× |
| 3× cores | 30 | 5.6 ms | 17,921 f/s | 7.43× |

### Interpretation

**Why 10 workers gets 7.6× on 10 cores, not 10×:**
Parallelism gains are always less than linear. Synchronization overhead
(locking the work queue mutex, signaling Condvars, OS thread scheduling) is
real cost that every worker pays on every file. With 100 files and 10 workers,
each worker handles ~10 files; the overhead is small relative to the work, but
not zero.

**Why 20 workers is marginally faster than 10:**
Two competing forces are always in play: **synchronization overhead**
(more threads → more Mutex contention → slower) and **I/O wait overlap**
(more threads → when one blocks on `read_to_string`, another runs).
At 20 workers, the I/O overlap gain still slightly exceeds the contention
cost. The margin is small (7.86× vs 7.60×) because files are small and I/O
is fast — there isn't much wait time to overlap.

**Why 30 workers is worse than 20:**
At 30 workers, the balance tips. More threads contend for the work queue
mutex on every pop, and the OS spends more time context-switching between
threads that have nothing to do. The synchronization overhead outweighs
whatever I/O overlap remains. 7.43× < 7.86×.

**The practical takeaway:**
For this file size, 2× cores (20 workers) is the sweet spot. For larger
files where `read_to_string` takes meaningful wall-clock time — real CI logs
in the 1–10 MB range — the I/O overlap effect would strengthen and the
optimal worker count would shift further above core count. That's a testable
prediction: re-run the benchmark with files 10× larger and watch the 3× cores
line improve relative to core count.

---

## Running the benchmark yourself

```bash
cargo build --release --bin bench-concurrent
./target/release/bench-concurrent
```

## Running the tests

```bash
cargo test
```
