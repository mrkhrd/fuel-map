//! Compact host for fuel-map: static index.html + CORS proxy.
//! TLS comes from the OS (SChannel via native-tls), nothing bundled.

use std::collections::HashMap;
use std::io::{self, Read, Write};
use std::net::{SocketAddr, TcpListener, TcpStream, ToSocketAddrs};
use std::sync::{Mutex, OnceLock};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
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
    let version = option_env!("APP_VERSION").unwrap_or(env!("CARGO_PKG_VERSION"));
    let listener = TcpListener::bind(("0.0.0.0", port))?;
    println!("fuel-host {version} — serving on http://localhost:{port} (log times in UTC)");
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

    let t0 = Instant::now();
    log(format_args!("> {method} {path}"));

    let route = ROUTES.iter().find(|(prefix, ..)| path.starts_with(prefix));
    let (code, ctype, body): (u16, &str, Vec<u8>) = if method != "GET" {
        (405, "text/plain", b"method not allowed".to_vec())
    } else if let Some((prefix, host, strip)) = route {
        let upstream = if *strip { &path[prefix.len() - 1..] } else { path };
        match fetch(host, upstream) {
            Ok(body) => (200, "application/json; charset=utf-8", body),
            Err(e) => (502, "text/plain", format!("upstream error: {e}").into_bytes()),
        }
    } else {
        match path.split('?').next().unwrap_or("/") {
            "/" | "/index.html" => {
                // index.html next to the exe wins (easy to customize), else the embedded copy
                let external = env::current_exe()
                    .ok()
                    .and_then(|p| fs::read(p.with_file_name("index.html")).ok());
                (200, "text/html; charset=utf-8", external.unwrap_or_else(|| INDEX.to_vec()))
            }
            _ => (404, "text/plain", b"not found".to_vec()),
        }
    };

    let result = respond(&mut s, code, ctype, &body);
    log(format_args!(
        "< {method} {path} -> {code}, {} bytes, {} ms",
        body.len(),
        t0.elapsed().as_millis()
    ));
    result
}

fn log(msg: std::fmt::Arguments) {
    let ms = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis();
    let s = ms / 1000;
    println!(
        "{:02}:{:02}:{:02}.{:03} {}",
        s / 3600 % 24,
        s / 60 % 60,
        s % 60,
        ms % 1000,
        msg
    );
}

fn fetch(host: &str, path: &str) -> Result<Vec<u8>, Box<dyn std::error::Error>> {
    let t0 = Instant::now();
    log(format_args!("  > api {host} {path}"));
    let r = fetch_inner(host, path);
    match &r {
        Ok(b) => log(format_args!(
            "  < api {host} -> 200, {} bytes, {} ms",
            b.len(),
            t0.elapsed().as_millis()
        )),
        Err(e) => log(format_args!(
            "  < api {host} -> {e}, {} ms",
            t0.elapsed().as_millis()
        )),
    }
    r
}

// DNS may return several addresses and some can be dead (sberazs.ru does this);
// walk them all with a short timeout and remember the one that worked
fn connect(host: &str) -> Result<TcpStream, Box<dyn std::error::Error>> {
    static GOOD: OnceLock<Mutex<HashMap<String, SocketAddr>>> = OnceLock::new();
    let good = GOOD.get_or_init(|| Mutex::new(HashMap::new()));

    // NB: copy the addr out so the guard drops here — in edition 2021 an
    // `if let` on `lock().unwrap().get(..)` holds the lock for the whole body,
    // and the remove() below would self-deadlock
    let cached = good.lock().unwrap().get(host).copied();
    if let Some(addr) = cached {
        if let Ok(s) = TcpStream::connect_timeout(&addr, Duration::from_secs(3)) {
            return Ok(s);
        }
        good.lock().unwrap().remove(host);
    }
    let mut last: Option<io::Error> = None;
    for addr in (host, 443).to_socket_addrs()? {
        match TcpStream::connect_timeout(&addr, Duration::from_secs(3)) {
            Ok(s) => {
                log(format_args!("  * {host} -> {addr}"));
                good.lock().unwrap().insert(host.to_string(), addr);
                return Ok(s);
            }
            Err(e) => last = Some(e),
        }
    }
    Err(last.map(Into::into).unwrap_or_else(|| "dns: no address".into()))
}

trait ReadWrite: Read + Write {}
impl<T: Read + Write> ReadWrite for T {}

#[cfg(windows)]
fn tls_connect(host: &str, tcp: TcpStream) -> Result<Box<dyn ReadWrite>, Box<dyn std::error::Error>> {
    Ok(Box::new(native_tls::TlsConnector::new()?.connect(host, tcp)?))
}

#[cfg(not(windows))]
fn tls_connect(host: &str, tcp: TcpStream) -> Result<Box<dyn ReadWrite>, Box<dyn std::error::Error>> {
    use std::sync::Arc;
    static CFG: OnceLock<Arc<rustls::ClientConfig>> = OnceLock::new();
    let cfg = CFG
        .get_or_init(|| {
            let roots = rustls::RootCertStore {
                roots: webpki_roots::TLS_SERVER_ROOTS.to_vec(),
            };
            Arc::new(
                rustls::ClientConfig::builder()
                    .with_root_certificates(roots)
                    .with_no_client_auth(),
            )
        })
        .clone();
    let name = rustls::pki_types::ServerName::try_from(host.to_string())?;
    let conn = rustls::ClientConnection::new(cfg, name)?;
    Ok(Box::new(rustls::StreamOwned::new(conn, tcp)))
}

fn fetch_inner(host: &str, path: &str) -> Result<Vec<u8>, Box<dyn std::error::Error>> {
    let tcp = connect(host)?;
    tcp.set_read_timeout(Some(Duration::from_secs(20)))?;
    tcp.set_write_timeout(Some(Duration::from_secs(20)))?;
    let mut tls = tls_connect(host, tcp)?;

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
