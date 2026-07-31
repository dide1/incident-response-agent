use std::env;
use std::fs;
use std::io::{self, Read};

fn main() {
    let args: Vec<String> = env::args().collect();

    let mut tail: Option<usize> = Some(150);
    let mut file_path: Option<String> = None;
    let mut concurrent: Option<usize> = None; // --concurrent N enables batch mode
    let mut batch_files: Vec<String> = Vec::new();

    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "--tail" | "-n" => {
                i += 1;
                if let Some(n) = args.get(i) {
                    tail = n.parse().ok();
                }
            }
            "--all" => {
                tail = None;
            }
            "--concurrent" => {
                // --concurrent N file1 file2 ...
                // N is the worker thread count; remaining args are file paths.
                i += 1;
                concurrent = args.get(i).and_then(|n| n.parse().ok());
                // All remaining args are treated as file paths for the batch.
                i += 1;
                while i < args.len() {
                    batch_files.push(args[i].clone());
                    i += 1;
                }
                continue; // skip the i += 1 at bottom
            }
            arg if !arg.starts_with('-') => {
                file_path = Some(arg.to_string());
            }
            _ => {}
        }
        i += 1;
    }

    if let Some(num_workers) = concurrent {
        // ── Batch / concurrent mode ──────────────────────────────────────────
        // Processes multiple files in parallel; output is a JSON array, one
        // object per file, in completion order (not input order).
        if batch_files.is_empty() {
            eprintln!("error: --concurrent N requires at least one file path");
            std::process::exit(1);
        }
        let results = log_analyzer::process_batch(batch_files, num_workers, tail);
        println!("{}", serde_json::to_string_pretty(&results).unwrap());
    } else {
        // ── Single-file mode (original behaviour, untouched) ─────────────────
        let input = if let Some(path) = file_path {
            fs::read_to_string(&path).unwrap_or_else(|e| {
                eprintln!("error reading {}: {}", path, e);
                std::process::exit(1);
            })
        } else {
            let mut buf = String::new();
            io::stdin().read_to_string(&mut buf).unwrap_or_else(|e| {
                eprintln!("error reading stdin: {}", e);
                std::process::exit(1);
            });
            buf
        };

        let result = log_analyzer::analyze(&input, tail);
        println!("{}", serde_json::to_string_pretty(&result).unwrap());
    }
}
