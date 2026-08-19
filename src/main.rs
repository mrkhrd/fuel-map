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
const ROUTES: [(&str, &str, bool); 4] = [
    ("/api/", "toplivo.tbank.ru", false),
    ("/sber/", "sberazs.ru", true),
    ("/alfa/", "alfabank.ru", true),
    ("/osrm/", "router.project-osrm.org", true),
];

// alfabank's bot filter returns 403 to anything that doesn't look like a real browser
fn agent(host: &str) -> &'static str {
    if host == "alfabank.ru" {
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 \
         (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
    } else {
        "Mozilla/5.0 (fuel-map local proxy)"
    }
}

// upstream address pins (like docker extra_hosts), set once from CLI args
static PINS: OnceLock<HashMap<String, SocketAddr>> = OnceLock::new();

fn main() -> io::Result<()> {
    let usage = || -> ! {
        eprintln!(
            "usage: fuel-host [port] [host=ip ...]   (default port {DEFAULT_PORT}; \
             host=ip pins an upstream address, e.g. sberazs.ru=185.71.64.253)"
        );
        std::process::exit(2);
    };
    let mut port = DEFAULT_PORT;
    let mut pins = HashMap::new();
    for arg in env::args().skip(1) {
        if let Some((host, ip)) = arg.split_once('=') {
            match ip.parse() {
                Ok(ip) => pins.insert(host.to_string(), SocketAddr::new(ip, 443)),
                Err(_) => usage(),
            };
        } else {
            match arg.parse() {
                Ok(p) => port = p,
                Err(_) => usage(),
            }
        }
    }
    let version = option_env!("APP_VERSION").unwrap_or(env!("CARGO_PKG_VERSION"));
    let listener = TcpListener::bind(("0.0.0.0", port))?;
    println!("fuel-host {version} — serving on http://localhost:{port} (log times in UTC)");
    for (host, addr) in &pins {
        println!("  pin: {host} -> {addr}");
    }
    let _ = PINS.set(pins);
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
    let mut gzip = false;
    let (code, ctype, body): (u16, String, Vec<u8>) = if method != "GET" {
        (405, "text/plain".into(), b"method not allowed".to_vec())
    } else if let Some((prefix, host, strip)) = route {
        let upstream = if *strip { &path[prefix.len() - 1..] } else { path };
        match fetch(host, upstream) {
            // pass the upstream status and body through rather than flattening
            // every failure to 502: these APIs explain themselves (sber answers
            // a too-wide bbox with 400 + {"error":{"code":"invalid_bbox"}}), and
            // that detail is what makes a broken map diagnosable from devtools
            Ok(u) => {
                gzip = u.gzip;
                (u.code, u.ctype, u.body)
            }
            Err(e) => (502, "text/plain".into(), format!("upstream error: {e}").into_bytes()),
        }
    } else {
        match path.split('?').next().unwrap_or("/") {
            "/" | "/index.html" => {
                // index.html next to the exe wins (easy to customize), else the embedded copy
                let external = env::current_exe()
                    .ok()
                    .and_then(|p| fs::read(p.with_file_name("index.html")).ok());
                (200, "text/html; charset=utf-8".into(), external.unwrap_or_else(|| INDEX.to_vec()))
            }
            _ => (404, "text/plain".into(), b"not found".to_vec()),
        }
    };

    let result = respond(&mut s, code, &ctype, gzip, &body);
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

/// What an upstream answered, forwarded to the browser as-is.
struct Upstream {
    code: u16,
    ctype: String,
    body: Vec<u8>,
    gzip: bool,
}

fn fetch(host: &str, path: &str) -> Result<Upstream, Box<dyn std::error::Error>> {
    let t0 = Instant::now();
    log(format_args!("  > api {host} {path}"));
    let r = fetch_inner(host, path);
    match &r {
        Ok(u) => log(format_args!(
            "  < api {host} -> {}, {} bytes{}, {} ms",
            u.code,
            u.body.len(),
            if u.gzip { " (gzip)" } else { "" },
            t0.elapsed().as_millis()
        )),
        Err(e) => log(format_args!(
            "  < api {host} -> {e}, {} ms",
            t0.elapsed().as_millis()
        )),
    }
    r
}

// All four upstreams currently resolve to a single address, but sberazs.ru has
// handed back several (with dead ones among them) while it moved hosts; walk
// them all with a short timeout and remember the one that worked.
fn connect(host: &str) -> Result<TcpStream, Box<dyn std::error::Error>> {
    if let Some(addr) = PINS.get().and_then(|p| p.get(host)) {
        return Ok(TcpStream::connect_timeout(addr, Duration::from_secs(5))?);
    }
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

// alfabank.ru's certificate chains to the Russian Trusted Root CA (Ministry of
// Digital Development), which is not in the Mozilla bundle webpki-roots ships —
// so on Linux the alfa source failed with UnknownIssuer, and in the scratch
// container there is no OS trust store to fall back on. The root is public and
// self-signed (alfabank serves it in its own chain); embedded here as DER.
// SHA-256 D2:6D:2D:02:31:B7:C3:9F:92:CC:73:85:12:BA:54:10:35:19:E4:40:5D:68:B5:BD:70:3E:97:88:CA:8E:CF:31
// It is added ONLY to the config used for alfabank.ru: the other upstreams keep
// the stock roots, so this CA cannot vouch for tbank, sber or the router.
#[cfg(not(windows))]
const ALFA_ROOT_CA: &[u8] = include_bytes!("russian-trusted-root-ca.der");
#[cfg(not(windows))]
const ALFA_HOST: &str = "alfabank.ru";

#[cfg(not(windows))]
fn tls_connect(host: &str, tcp: TcpStream) -> Result<Box<dyn ReadWrite>, Box<dyn std::error::Error>> {
    use std::sync::Arc;
    fn config(extra: Option<&'static [u8]>) -> Arc<rustls::ClientConfig> {
        let mut roots = rustls::RootCertStore {
            roots: webpki_roots::TLS_SERVER_ROOTS.to_vec(),
        };
        if let Some(der) = extra {
            // a malformed embedded root would silently disable the source, so
            // fail loudly at first use instead of retrying forever
            roots
                .add(rustls::pki_types::CertificateDer::from(der))
                .expect("embedded alfabank root CA is not valid DER");
        }
        Arc::new(
            rustls::ClientConfig::builder()
                .with_root_certificates(roots)
                .with_no_client_auth(),
        )
    }
    static CFG: OnceLock<Arc<rustls::ClientConfig>> = OnceLock::new();
    static ALFA_CFG: OnceLock<Arc<rustls::ClientConfig>> = OnceLock::new();
    let cfg = if host == ALFA_HOST {
        ALFA_CFG.get_or_init(|| config(Some(ALFA_ROOT_CA))).clone()
    } else {
        CFG.get_or_init(|| config(None)).clone()
    };
    let name = rustls::pki_types::ServerName::try_from(host.to_string())?;
    let conn = rustls::ClientConnection::new(cfg, name)?;
    Ok(Box::new(rustls::StreamOwned::new(conn, tcp)))
}

// header value from a response line, name compared case-insensitively
fn header<'a>(line: &'a str, name: &str) -> Option<&'a str> {
    let (k, v) = line.split_once(':')?;
    if k.trim().eq_ignore_ascii_case(name) { Some(v.trim()) } else { None }
}

// "https://host/path" -> "/path" (Location headers come back absolute)
fn url_path(url: &str) -> &str {
    url.split_once("://")
        .and_then(|(_, rest)| rest.find('/').map(|i| &rest[i..]))
        .unwrap_or(url)
}

// Gzip is passed through to the browser undecoded — alfabank's country-wide
// dump is 21 MB raw vs ~3 MB compressed.
fn fetch_inner(host: &str, path: &str) -> Result<Upstream, Box<dyn std::error::Error>> {
    // alfabank fronts the API with a bot check: 307 to the same URL + session
    // cookies; the retry with those cookies gets the data. Keep them per host.
    static JAR: OnceLock<Mutex<HashMap<String, HashMap<String, String>>>> = OnceLock::new();
    let jar = JAR.get_or_init(|| Mutex::new(HashMap::new()));

    let mut path = path.to_string();
    for _ in 0..3 {
        let tcp = connect(host)?;
        tcp.set_read_timeout(Some(Duration::from_secs(20)))?;
        tcp.set_write_timeout(Some(Duration::from_secs(20)))?;
        let mut tls = tls_connect(host, tcp)?;

        let cookie = jar.lock().unwrap().get(host).map(|m| {
            let pairs: Vec<String> = m.iter().map(|(k, v)| format!("{k}={v}")).collect();
            format!("Cookie: {}\r\n", pairs.join("; "))
        });
        // HTTP/1.1 (tbank rejects 1.0 with 426); Connection: close delimits the body
        write!(
            tls,
            "GET {path} HTTP/1.1\r\nHost: {host}\r\n\
             User-Agent: {}\r\nAccept: application/json\r\n\
             Accept-Encoding: gzip\r\n{}Connection: close\r\n\r\n",
            agent(host),
            cookie.as_deref().unwrap_or("")
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
        let head = str::from_utf8(&resp[..sep]).unwrap_or("").to_string();
        for line in head.lines() {
            if let Some(v) = header(line, "set-cookie") {
                if let Some((name, val)) = v.split(';').next().unwrap_or("").split_once('=') {
                    jar.lock()
                        .unwrap()
                        .entry(host.to_string())
                        .or_default()
                        .insert(name.trim().to_string(), val.trim().to_string());
                }
            }
        }

        let status = head.split(' ').nth(1).unwrap_or("");
        // 3xx with a Location is the alfabank bot check handing us cookies —
        // retry the same URL with them (the loop cap stops a redirect cycle)
        if status.starts_with('3') {
            if let Some(loc) = head.lines().find_map(|l| header(l, "location")) {
                path = url_path(loc).to_string();
                continue;
            }
        }
        let code: u16 = status.parse().map_err(|_| "bad upstream status")?;
        let lower = head.to_ascii_lowercase();
        let body = &resp[sep + 4..];
        let body = if lower.contains("transfer-encoding: chunked") {
            dechunk(body)?
        } else {
            body.to_vec()
        };
        let ctype = head
            .lines()
            .find_map(|l| header(l, "content-type"))
            .unwrap_or("application/json; charset=utf-8")
            .to_string();
        return Ok(Upstream { code, ctype, body, gzip: lower.contains("content-encoding: gzip") });
    }
    Err("redirect loop".into())
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

fn respond(s: &mut TcpStream, code: u16, ctype: &str, gzip: bool, body: &[u8]) -> io::Result<()> {
    let reason = match code {
        200 => "OK",
        400 => "Bad Request",
        403 => "Forbidden",
        404 => "Not Found",
        405 => "Method Not Allowed",
        429 => "Too Many Requests",
        500 => "Internal Server Error",
        503 => "Service Unavailable",
        502 => "Bad Gateway",
        c if c < 400 => "OK",
        _ => "Error",
    };
    // the content type is echoed from the upstream — never let it break the head
    let ctype: String = ctype.chars().filter(|c| *c != '\r' && *c != '\n').collect();
    write!(
        s,
        "HTTP/1.1 {code} {reason}\r\nContent-Type: {ctype}\r\nContent-Length: {}\r\n{}\
         Cache-Control: no-store\r\nConnection: close\r\n\r\n",
        body.len(),
        if gzip { "Content-Encoding: gzip\r\n" } else { "" }
    )?;
    s.write_all(body)
}
