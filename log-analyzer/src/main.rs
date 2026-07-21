use std::env;
use std::fs;
use std::io::{self, Read};

fn main() {
    let args: Vec<String> = env::args().collect();

    let mut tail: Option<usize> = Some(150); // default: analyze last 150 lines
    let mut file_path: Option<String> = None;

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
            arg if !arg.starts_with('-') => {
                file_path = Some(arg.to_string());
            }
            _ => {}
        }
        i += 1;
    }

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
