//! Compact host for fuel-map: static index.html + CORS proxy.
//! TLS comes from the OS (SChannel via native-tls), nothing bundled.

use std::io::{self, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::time::Duration;
use std::{env, fs, str, thread};

const DEFAULT_PORT: u16 = 8000;
const INDEX: &[u8] = include_bytes!("../index.html");

/// (url prefix, upstream host, strip prefix from forwarded path)
const ROUTES: [(&str, &str, bool); 3] = [
    ("/api/", "toplivo.tbank.ru", false),
    ("/sber/", "sberazs.ru", true),
    ("/osrm/", "router.project-osrm.org", true),
];

fn main() -> io::Result<()> {
    let port: u16 = match env::args().nth(1) {
        Some(a) => a.parse().unwrap_or_else(|_| {
            eprintln!("usage: fuel-host [port]   (default {DEFAULT_PORT})");
            std::process::exit(2);
        }),
        None => DEFAULT_PORT,
    };
    let listener = TcpListener::bind(("0.0.0.0", port))?;
    println!("Serving on http://localhost:{port}");
    for stream in listener.incoming().flatten() {
        thread::spawn(move || {
            let _ = handle(stream);
        });
    }
    Ok(())
}

fn handle(mut s: TcpStream) -> io::Result<()> {
    s.set_read_timeout(Some(Duration::from_secs(10)))?;
    s.set_write_timeout(Some(Duration::from_secs(20)))?;

    let mut head = Vec::new();
    let mut buf = [0u8; 2048];
    while !head.windows(4).any(|w| w == b"\r\n\r\n") {
        let n = s.read(&mut buf)?;
        if n == 0 || head.len() > 16384 {
            return Ok(());
        }
        head.extend_from_slice(&buf[..n]);
    }
    let line = head.split(|&b| b == b'\r').next().unwrap_or(b"");
    let mut parts = str::from_utf8(line).unwrap_or("").split(' ');
    let method = parts.next().unwrap_or("");
    let path = parts.next().unwrap_or("/");
    if method != "GET" {
        return respond(&mut s, 405, "text/plain", b"method not allowed");
    }

    for (prefix, host, strip) in ROUTES {
        if path.starts_with(prefix) {
            let upstream = if strip { &path[prefix.len() - 1..] } else { path };
            return match fetch(host, upstream) {
                Ok(body) => respond(&mut s, 200, "application/json; charset=utf-8", &body),
                Err(e) => {
                    respond(&mut s, 502, "text/plain", format!("upstream error: {e}").as_bytes())
                }
            };
        }
    }

    match path.split('?').next().unwrap_or("/") {
        "/" | "/index.html" => {
            // index.html next to the exe wins (easy to customize), else the embedded copy
            let external = env::current_exe()
                .ok()
                .and_then(|p| fs::read(p.with_file_name("index.html")).ok());
            respond(&mut s, 200, "text/html; charset=utf-8", external.as_deref().unwrap_or(INDEX))
        }
        _ => respond(&mut s, 404, "text/plain", b"not found"),
    }
}

fn fetch(host: &str, path: &str) -> Result<Vec<u8>, Box<dyn std::error::Error>> {
    let tcp = TcpStream::connect((host, 443))?;
    tcp.set_read_timeout(Some(Duration::from_secs(20)))?;
    tcp.set_write_timeout(Some(Duration::from_secs(20)))?;
    let mut tls = native_tls::TlsConnector::new()?.connect(host, tcp)?;

    // HTTP/1.1 (tbank rejects 1.0 with 426); Connection: close delimits the body
    write!(
        tls,
        "GET {path} HTTP/1.1\r\nHost: {host}\r\n\
         User-Agent: Mozilla/5.0 (fuel-map local proxy)\r\n\
         Accept: application/json\r\nConnection: close\r\n\r\n"
    )?;

    let mut resp = Vec::new();
    let mut buf = [0u8; 16384];
    loop {
        match tls.read(&mut buf) {
            Ok(0) => break,
            Ok(n) => resp.extend_from_slice(&buf[..n]),
            // some servers drop the link without close_notify — keep what we got
            Err(_) if !resp.is_empty() => break,
            Err(e) => return Err(e.into()),
        }
    }

    let sep = resp
        .windows(4)
        .position(|w| w == b"\r\n\r\n")
        .ok_or("bad upstream response")?;
    let head = str::from_utf8(&resp[..sep]).unwrap_or("");
    let code = head.split(' ').nth(1).unwrap_or("?");
    if code != "200" {
        return Err(format!("HTTP {code}").into());
    }
    let body = &resp[sep + 4..];
    if head.to_ascii_lowercase().contains("transfer-encoding: chunked") {
        dechunk(body)
    } else {
        Ok(body.to_vec())
    }
}

fn dechunk(mut b: &[u8]) -> Result<Vec<u8>, Box<dyn std::error::Error>> {
    let mut out = Vec::new();
    loop {
        let pos = b.windows(2).position(|w| w == b"\r\n").ok_or("bad chunk header")?;
        let size_line = str::from_utf8(&b[..pos])?;
        let size = usize::from_str_radix(size_line.split(';').next().unwrap_or("").trim(), 16)?;
        b = &b[pos + 2..];
        if size == 0 {
            return Ok(out);
        }
        if b.len() < size + 2 {
            return Err("truncated chunk".into());
        }
        out.extend_from_slice(&b[..size]);
        b = &b[size + 2..];
    }
}

fn respond(s: &mut TcpStream, code: u16, ctype: &str, body: &[u8]) -> io::Result<()> {
    let reason = match code {
        200 => "OK",
        404 => "Not Found",
        405 => "Method Not Allowed",
        _ => "Bad Gateway",
    };
    write!(
        s,
        "HTTP/1.1 {code} {reason}\r\nContent-Type: {ctype}\r\nContent-Length: {}\r\n\
         Cache-Control: no-store\r\nConnection: close\r\n\r\n",
        body.len()
    )?;
    s.write_all(body)
}
