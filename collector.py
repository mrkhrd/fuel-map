"""Phase 0 collector for fuel-map: poll the three sources, archive raw JSON.

Deliberately does no interpretation beyond the handful of fields that describe
the poll itself (station count, tbank's 300-cap, sber's snapshot hash). History
is collected exactly once: if the parser that comes later has a bug, the archive
can be replayed, but the days spent writing that parser cannot be re-lived.

Requests go through the fuel-host proxy on localhost rather than straight to the
providers -- TLS, the alfabank anti-bot dance, the Russian root CA, gzip and
address pins already work there and should live in exactly one place.

Run:  python collector.py [proxy_port]      (default 8000)
"""
import gzip
import hashlib
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "fuel.db")
RAW_DIR = os.path.join(HERE, "raw")

PROXY = "http://127.0.0.1:%d"
DEFAULT_PORT = 8000

# Saint Petersburg city. minLat, minLon, maxLat, maxLon
REGION = (59.80, 30.10, 60.10, 30.60)

# tbank caps a bbox response here and offers no paging, so a tile that comes
# back full is hiding an unknown number of stations and has to be split
TBANK_CAP = 300
TBANK_MAX_DEPTH = 4

# how often each source is worth asking, in seconds. alfa is one country-wide
# blob (~3 MB gzip) whose per-fuel clocks have a median age of ~16 h -- polling
# it faster buys nothing but bandwidth
INTERVALS = {"tbank": 300, "sber": 300, "alfa": 900}

TIMEOUTS = {"tbank": 20, "sber": 20, "alfa": 90}

RAW_RETENTION_DAYS = 14
PRUNE_EVERY = 6 * 3600

SCHEMA = """
PRAGMA journal_mode=WAL;

-- one row per HTTP fetch, kept forever even after its body is pruned:
-- failures and gaps are data, and phase 1 needs to know where they are
CREATE TABLE IF NOT EXISTS poll (
  id            INTEGER PRIMARY KEY,
  source        TEXT    NOT NULL,
  tile          TEXT,
  started_at    INTEGER NOT NULL,
  duration_ms   INTEGER,
  http_status   INTEGER,
  bytes         INTEGER,
  station_count INTEGER,
  capped        INTEGER NOT NULL DEFAULT 0,
  snapshot_ver  TEXT,
  sha256        TEXT,
  error         TEXT
);
CREATE INDEX IF NOT EXISTS poll_time ON poll(source, started_at);
CREATE INDEX IF NOT EXISTS poll_sha  ON poll(sha256);

-- bodies are content-addressed, so an unchanged response costs one row and no
-- disk. Measured: tbank tiles repeat often and alfa repeats between its 15-min
-- polls; sber does not (its dataVersion moved within 11 s of the previous poll)
CREATE TABLE IF NOT EXISTS blob (
  sha256     TEXT PRIMARY KEY,
  source     TEXT    NOT NULL,
  bytes      INTEGER NOT NULL,
  stored     INTEGER NOT NULL,
  first_seen INTEGER NOT NULL,
  path       TEXT    NOT NULL,
  pruned     INTEGER NOT NULL DEFAULT 0
);
"""


def now_ms():
    return int(time.time() * 1000)


def log(msg):
    print("%s  %s" % (time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()), msg), flush=True)


# ---------- fetching ----------

def fetch(port, path, timeout):
    """GET through the proxy. Returns (status, decompressed body, ms, error)."""
    t0 = time.time()
    req = urllib.request.Request((PROXY % port) + path, headers={
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "User-Agent": "fuel-map collector",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            if r.headers.get("Content-Encoding", "") == "gzip":
                body = gzip.decompress(body)
            return r.status, body, int((time.time() - t0) * 1000), None
    except urllib.error.HTTPError as e:
        return e.code, None, int((time.time() - t0) * 1000), "HTTP %d" % e.code
    except Exception as e:
        return None, None, int((time.time() - t0) * 1000), "%s: %s" % (type(e).__name__, e)


# ---------- archive ----------

def store_blob(db, source, body):
    """Write the body once per distinct content. Returns (sha, size)."""
    sha = hashlib.sha256(body).hexdigest()
    row = db.execute("SELECT pruned FROM blob WHERE sha256=?", (sha,)).fetchone()
    rel = os.path.join(source, sha[:2], sha + ".json.gz")
    if row is None or row[0]:
        full = os.path.join(RAW_DIR, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        # mtime=0 keeps the gzip container byte-identical for a given body
        with open(full, "wb") as f:
            with gzip.GzipFile(fileobj=f, mode="wb", compresslevel=6, mtime=0) as gz:
                gz.write(body)
        db.execute(
            "INSERT INTO blob (sha256, source, bytes, stored, first_seen, path, pruned) "
            "VALUES (?,?,?,?,?,?,0) ON CONFLICT(sha256) DO UPDATE SET pruned=0",
            (sha, source, len(body), os.path.getsize(full), now_ms(),
             rel.replace("\\", "/")))
    return sha, len(body)


def record(db, source, tile, started, dur, status, body, error,
           count=None, capped=0, snapshot=None):
    sha = size = None
    if body is not None:
        sha, size = store_blob(db, source, body)
    db.execute(
        "INSERT INTO poll (source, tile, started_at, duration_ms, http_status, bytes,"
        " station_count, capped, snapshot_ver, sha256, error) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (source, tile, started, dur, status, size, count, capped, snapshot, sha, error))
    db.commit()


# ---------- per-source polls ----------

def poll_tbank(db, port):
    """Walk the region as a quadtree, splitting any tile that comes back full."""
    stack = [(REGION, 0)]
    tiles = split = total = 0
    while stack:
        (a, c, b, d), depth = stack.pop()
        tile = "%.4f,%.4f,%.4f,%.4f" % (a, c, b, d)
        path = ("/api/v1/stations?minLat=%.6f&maxLat=%.6f&minLon=%.6f&maxLon=%.6f"
                % (a, b, c, d))
        started = now_ms()
        status, body, dur, err = fetch(port, path, TIMEOUTS["tbank"])
        count = None
        capped = 0
        if body is not None:
            try:
                count = len(json.loads(body).get("payload") or [])
                capped = 1 if count >= TBANK_CAP else 0
            except Exception as e:
                err = "parse: %s" % e
        record(db, "tbank", tile, started, dur, status, body, err, count, capped)
        tiles += 1
        if capped and depth < TBANK_MAX_DEPTH:
            mlat, mlon = (a + b) / 2, (c + d) / 2
            stack += [((a, c, mlat, mlon), depth + 1), ((a, mlon, mlat, d), depth + 1),
                      ((mlat, c, b, mlon), depth + 1), ((mlat, mlon, b, d), depth + 1)]
            split += 1
            continue  # this tile's stations are re-covered by its children
        if capped:
            log("tbank: tile %s still capped at depth %d" % (tile, depth))
        total += count or 0
    return "%d tiles, %d stations%s" % (tiles, total, ", %d split" % split if split else "")


def poll_sber(db, port):
    w, s, e, n = REGION[1], REGION[0], REGION[3], REGION[2]
    path = "/sber/api/stations?bbox=%.6f,%.6f,%.6f,%.6f" % (w, s, e, n)
    started = now_ms()
    status, body, dur, err = fetch(port, path, TIMEOUTS["sber"])
    count = snapshot = None
    if body is not None:
        try:
            d = json.loads(body)
            count = len(d.get("stations") or [])
            snapshot = d.get("dataVersion")
        except Exception as ex:
            err = "parse: %s" % ex
    tile = "%.4f,%.4f,%.4f,%.4f" % REGION
    record(db, "sber", tile, started, dur, status, body, err, count, 0, snapshot)
    return "%s stations" % count if count is not None else (err or "failed")


def poll_alfa(db, port):
    """alfa has no bbox API: one country-wide blob, sliced to the region later."""
    started = now_ms()
    status, body, dur, err = fetch(port, "/alfa/api/v1/azs-stations/public/stations",
                                   TIMEOUTS["alfa"])
    count = None
    if body is not None:
        try:
            count = len(json.loads(body))
        except Exception as ex:
            err = "parse: %s" % ex
    record(db, "alfa", None, started, dur, status, body, err, count)
    if body is None:
        return err or "failed"
    return "%s stations, %.1f MB" % (count, len(body) / 1e6)


POLLS = {"tbank": poll_tbank, "sber": poll_sber, "alfa": poll_alfa}


def preflight(port):
    """Fail loudly on the wrong port. Another service answering 404 on /api/
    would otherwise look exactly like a week of successfully recorded nothing."""
    status, body, _, err = fetch(port, "/api/v1/stations"
                                 "?minLat=59.93&maxLat=59.94&minLon=30.30&maxLon=30.31", 15)
    if err or status != 200:
        sys.exit("proxy check failed on %s: %s -- is fuel-host running on this port?"
                 % (PROXY % port, err or "HTTP %s" % status))
    try:
        if json.loads(body).get("status") != "ok":
            raise ValueError("unexpected payload")
    except Exception as e:
        sys.exit("proxy on %s does not look like fuel-host: %s" % (PROXY % port, e))


# ---------- retention ----------

def prune(db):
    """Drop bodies older than the window. poll rows stay: they are the record of
    what we asked and when, and they cost nothing."""
    cutoff = now_ms() - RAW_RETENTION_DAYS * 86400000
    stale = db.execute(
        "SELECT sha256, path, stored FROM blob WHERE pruned=0 AND sha256 NOT IN"
        " (SELECT sha256 FROM poll WHERE sha256 IS NOT NULL AND started_at > ?)",
        (cutoff,)).fetchall()
    freed = 0
    for sha, rel, stored in stale:
        try:
            os.remove(os.path.join(RAW_DIR, rel.replace("/", os.sep)))
        except OSError:
            pass
        db.execute("UPDATE blob SET pruned=1 WHERE sha256=?", (sha,))
        freed += stored
    db.commit()
    if stale:
        log("prune: %d bodies, %.1f MB freed" % (len(stale), freed / 1e6))


def disk_report(db):
    n, size = db.execute(
        "SELECT COUNT(*), COALESCE(SUM(stored),0) FROM blob WHERE pruned=0").fetchone()
    polls = db.execute("SELECT COUNT(*) FROM poll").fetchone()[0]
    return "%d polls, %d bodies, %.1f MB on disk" % (polls, n, size / 1e6)


# ---------- loop ----------

def main():
    port = DEFAULT_PORT
    once = False
    for arg in sys.argv[1:]:
        if arg == "--once":
            once = True
            continue
        try:
            port = int(arg)
        except ValueError:
            sys.exit("usage: python collector.py [proxy_port] [--once]")

    preflight(port)
    os.makedirs(RAW_DIR, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.executescript(SCHEMA)
    db.commit()

    log("collector phase 0 -- proxy %s, region %s" % (PROXY % port, REGION))
    log("db %s, retention %d days" % (DB_PATH, RAW_RETENTION_DAYS))
    log(disk_report(db))

    due = dict.fromkeys(POLLS, 0.0)
    next_prune = 0.0
    try:
        while True:
            if time.time() >= next_prune:
                prune(db)
                next_prune = time.time() + PRUNE_EVERY
            for source in sorted(due, key=due.get):
                if time.time() < due[source]:
                    continue
                t0 = time.time()
                try:
                    summary = POLLS[source](db, port)
                except Exception as e:
                    db.rollback()
                    summary = "FAILED %s: %s" % (type(e).__name__, e)
                log("%-5s %s (%.1fs)" % (source, summary, time.time() - t0))
                # schedule from finish, not from start, so a slow poll cannot
                # queue up a backlog it will never work off
                due[source] = time.time() + INTERVALS[source]
            if once:
                log("done -- " + disk_report(db))
                return
            time.sleep(min(1.0, max(0.05, min(due.values()) - time.time())))
    except KeyboardInterrupt:
        log("stopping -- " + disk_report(db))
    finally:
        db.close()


if __name__ == "__main__":
    main()
