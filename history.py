"""Phase 2: read-only HTTP API over the history built by importer.py, plus the
browsing UI in history.html.

Runs beside fuel-host rather than inside it: the map stays a map, and this stays
a query surface over SQLite. Nothing here writes.

Run:  python history.py [port]        (default 8010)
"""
import http.server
import json
import os
import socketserver
import sqlite3
import sys
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "fuel.db")
PAGE = os.path.join(HERE, "history.html")
DEFAULT_PORT = 8010

STATUSES = ["available", "maybe_available", "not_available", "stale", "no_data", "closed"]
MAX_RUNS = 400000       # guard: a wide window over months should not eat the box
MAX_LIST_RUNS = 60000   # runs sent to the list view for one page of places


def db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def holders(vals):
    return ",".join("?" * len(vals))


def qs_list(q, key):
    """?fuel=92,95 or repeated ?fuel=92&fuel=95 -- both are natural to type."""
    out = []
    for v in q.get(key, []):
        out += [x for x in v.split(",") if x]
    return out


class Filters:
    """The filter set shared by every endpoint, parsed once."""

    def __init__(self, q):
        now = int(time.time() * 1000)
        self.to = int(q.get("to", [now])[0] or now)
        self.frm = int(q.get("from", [self.to - 24 * 3600_000])[0] or 0)
        self.fuels = qs_list(q, "fuel")
        self.statuses = qs_list(q, "status")
        self.sources = qs_list(q, "source")
        self.brands = qs_list(q, "brand")
        self.q = (q.get("q", [""])[0] or "").strip()
        self.min_sources = int(q.get("minsrc", [0])[0] or 0)
        self.dated = q.get("dated", [""])[0] == "1"      # only per-fuel stamped
        self.changed = q.get("changed", [""])[0] == "1"  # only fuels that moved
        self.price_min = q.get("pmin", [""])[0]
        self.price_max = q.get("pmax", [""])[0]

    def run_where(self, alias="f", s="s"):
        """SQL fragment + params selecting fuel runs that overlap the window."""
        w = ["%s.last_seen >= ? AND %s.first_seen <= ?" % (alias, alias)]
        p = [self.frm, self.to]
        if self.fuels:
            w.append("%s.fuel IN (%s)" % (alias, holders(self.fuels)))
            p += self.fuels
        if self.statuses:
            w.append("%s.status IN (%s)" % (alias, holders(self.statuses)))
            p += self.statuses
        if self.sources:
            w.append("%s.source IN (%s)" % (s, holders(self.sources)))
            p += self.sources
        if self.dated:
            w.append("%s.reported_at IS NOT NULL" % alias)
        if self.price_min:
            w.append("%s.price >= ?" % alias)
            p.append(float(self.price_min))
        if self.price_max:
            w.append("%s.price <= ?" % alias)
            p.append(float(self.price_max))
        return " AND ".join(w), p

    def place_where(self, alias="p"):
        w, p = [], []
        if self.brands:
            w.append("%s.brand IN (%s)" % (alias, holders(self.brands)))
            p += self.brands
        if self.q:
            w.append("(%s.name LIKE ? OR %s.addr LIKE ?)" % (alias, alias))
            p += ["%" + self.q + "%"] * 2
        if self.min_sources:
            w.append("(LENGTH(%s.sources) - LENGTH(REPLACE(%s.sources, ',', '')) + 1) >= ?"
                     % (alias, alias))
            p.append(self.min_sources)
        return (" AND ".join(w) if w else "1"), p


# ---------- endpoints ----------

def ep_meta(c, q):
    span = c.execute("SELECT MIN(first_seen), MAX(last_seen) FROM fuel_state").fetchone()
    polls = c.execute("SELECT MIN(started_at), MAX(started_at) FROM poll").fetchone()
    return {
        "from": span[0], "to": span[1],
        "pollFrom": polls[0], "pollTo": polls[1],
        "fuels": [r[0] for r in c.execute(
            "SELECT fuel, COUNT(*) n FROM fuel_state GROUP BY fuel ORDER BY n DESC")],
        "brands": [r[0] for r in c.execute(
            "SELECT brand FROM place WHERE brand IS NOT NULL"
            " GROUP BY brand ORDER BY COUNT(*) DESC")],
        "sources": [r[0] for r in c.execute(
            "SELECT DISTINCT source FROM station ORDER BY 1")],
        "statuses": STATUSES,
        "places": c.execute("SELECT COUNT(*) FROM place").fetchone()[0],
        "stations": c.execute("SELECT COUNT(*) FROM station").fetchone()[0],
        "runs": c.execute("SELECT COUNT(*) FROM fuel_state").fetchone()[0],
        "events": c.execute("SELECT COUNT(*) FROM fuel_event").fetchone()[0],
        "lastImport": (c.execute("SELECT v FROM import_state WHERE k='last_poll'")
                       .fetchone() or [None])[0],
    }


def ep_places(c, q):
    f = Filters(q)
    limit = min(int(q.get("limit", [200])[0]), 1000)
    offset = int(q.get("offset", [0])[0])
    rw, rp = f.run_where()
    pw, pp = f.place_where()
    having = ""
    if f.changed:
        # more than one run for the same fuel inside the window means it moved
        having = " HAVING SUM(chg) > 0"
    sql = ("SELECT p.id, p.name, p.brand, p.addr, p.lat, p.lon, p.sources,"
           " COUNT(*) runs, SUM(CASE WHEN f.status='available' THEN 1 ELSE 0 END) avail,"
           " MAX(f.last_seen) seen, SUM(chg) chg FROM ("
           "  SELECT f.*, s.place_id, s.source,"
           "   (SELECT COUNT(*)-1 FROM fuel_state g WHERE g.station_id=f.station_id"
           "     AND g.fuel=f.fuel AND g.last_seen>=? AND g.first_seen<=?) chg"
           "  FROM fuel_state f JOIN station s ON s.id=f.station_id WHERE " + rw + ""
           " ) f JOIN place p ON p.id=f.place_id WHERE " + pw +
           " GROUP BY p.id" + having +
           " ORDER BY %s LIMIT ? OFFSET ?" % {
               "name": "p.name", "recent": "seen DESC", "changes": "chg DESC",
               "avail": "avail DESC"}.get(q.get("sort", ["name"])[0], "p.name"))
    rows = c.execute(sql, [f.frm, f.to] + rp + pp + [limit, offset]).fetchall()
    ids = [r["id"] for r in rows]
    state = {}
    if ids:
        # every run in the window, not just the newest one per source+fuel: the
        # list view draws these as a timeline, and keeping only the latest would
        # render one run stretched over the whole window as if nothing had moved
        for r in c.execute(
                "SELECT s.place_id, s.source, f.fuel, f.status, f.status_raw, f.price,"
                " f.limit_l, f.reported_at, f.first_seen, f.last_seen"
                " FROM fuel_state f JOIN station s ON s.id=f.station_id"
                " WHERE s.place_id IN (%s) AND f.last_seen >= ? AND f.first_seen <= ?"
                " ORDER BY f.first_seen LIMIT ?" % holders(ids),
                ids + [f.frm, f.to, MAX_LIST_RUNS]):
            state.setdefault(r["place_id"], []).append(dict(r))
    out = []
    for r in rows:
        d = dict(r)
        d["state"] = state.get(r["id"], [])
        out.append(d)
    total = c.execute("SELECT COUNT(*) FROM place p WHERE " + pw, pp).fetchone()[0]
    return {"places": out, "count": len(out), "placesTotal": total,
            "from": f.frm, "to": f.to}


def ep_timeline(c, q):
    f = Filters(q)
    pid = int(q.get("place", [0])[0])
    place = c.execute("SELECT * FROM place WHERE id=?", (pid,)).fetchone()
    if not place:
        return {"error": "no such place"}
    st = [dict(r) for r in c.execute(
        "SELECT * FROM station WHERE place_id=? ORDER BY source", (pid,))]
    sids = [s["id"] for s in st]
    if not sids:
        return {"place": dict(place), "stations": [], "runs": [], "events": []}
    rw, rp = f.run_where()
    runs = [dict(r) for r in c.execute(
        "SELECT f.*, s.source FROM fuel_state f JOIN station s ON s.id=f.station_id"
        " WHERE s.place_id=? AND " + rw + " ORDER BY f.fuel, f.first_seen",
        [pid] + rp)]
    sruns = [dict(r) for r in c.execute(
        "SELECT ss.*, s.source FROM station_state ss JOIN station s ON s.id=ss.station_id"
        " WHERE s.place_id=? AND ss.last_seen>=? AND ss.first_seen<=?"
        " ORDER BY ss.first_seen", (pid, f.frm, f.to))]
    ev = [dict(r) for r in c.execute(
        "SELECT e.*, s.source FROM fuel_event e JOIN station s ON s.id=e.station_id"
        " WHERE s.place_id=? AND e.reported_at BETWEEN ? AND ?"
        " ORDER BY e.reported_at DESC LIMIT 500", (pid, f.frm, f.to))]
    return {"place": dict(place), "stations": st, "runs": runs,
            "stationRuns": sruns, "events": ev, "from": f.frm, "to": f.to}


def ep_stats(c, q):
    f = Filters(q)
    rw, rp = f.run_where()
    pw, pp = f.place_where()
    rows = c.execute(
        "SELECT f.fuel, f.status, s.source, p.brand, p.id place_id,"
        " f.first_seen, f.last_seen FROM fuel_state f"
        " JOIN station s ON s.id=f.station_id JOIN place p ON p.id=s.place_id"
        " WHERE " + rw + " AND " + pw + " LIMIT ?", rp + pp + [MAX_RUNS]).fetchall()

    def acc(keyfn):
        d = {}
        for r in rows:
            k = keyfn(r)
            if k is None:
                continue
            lo, hi = max(r["first_seen"], f.frm), min(r["last_seen"], f.to)
            ms = max(0, hi - lo)
            e = d.setdefault(k, {"key": k, "ms": 0, "avail": 0, "places": set(), "runs": 0})
            e["ms"] += ms
            e["runs"] += 1
            e["places"].add(r["place_id"])
            if r["status"] == "available":
                e["avail"] += ms
        out = []
        for e in d.values():
            e["places"] = len(e["places"])
            e["pct"] = round(100.0 * e["avail"] / e["ms"], 1) if e["ms"] else None
            out.append(e)
        return sorted(out, key=lambda e: -e["ms"])

    # availability sampled at even instants: "how many places had it, when"
    n = 80
    step = max(1, (f.to - f.frm) // n)
    pts = [f.frm + i * step for i in range(n + 1)]
    series = {}
    for r in rows:
        if r["status"] == "closed":       # not sold here at all: not a denominator
            continue
        # every other observed state counts, no_data included -- the chart and the
        # table must share a denominator or the same fuel reads 100% and 1.1%
        s = series.setdefault(r["fuel"], [[0, 0] for _ in pts])
        i0 = max(0, (r["first_seen"] - f.frm) // step)
        i1 = min(len(pts) - 1, (r["last_seen"] - f.frm) // step)
        for i in range(int(i0), int(i1) + 1):
            s[i][1] += 1
            if r["status"] == "available":
                s[i][0] += 1
    return {"byFuel": acc(lambda r: r["fuel"]), "byBrand": acc(lambda r: r["brand"]),
            "bySource": acc(lambda r: r["source"]), "points": pts,
            "series": series, "runs": len(rows), "truncated": len(rows) >= MAX_RUNS}


def ep_coverage(c, q):
    f = Filters(q)
    polls = [dict(r) for r in c.execute(
        "SELECT id, source, tile, started_at, duration_ms, http_status, bytes,"
        " station_count, capped, error FROM poll WHERE started_at BETWEEN ? AND ?"
        " ORDER BY started_at DESC LIMIT 3000", (f.frm, f.to))]
    by = [dict(r) for r in c.execute(
        "SELECT source, COUNT(*) polls, SUM(error IS NOT NULL) failed,"
        " SUM(capped) capped, AVG(duration_ms) avg_ms, SUM(bytes) bytes,"
        " COUNT(DISTINCT sha256) bodies FROM poll"
        " WHERE started_at BETWEEN ? AND ? GROUP BY source", (f.frm, f.to))]
    disk = c.execute("SELECT COUNT(*), COALESCE(SUM(stored),0) FROM blob"
                     " WHERE pruned=0").fetchone()
    # Gaps are measured between polling *cycles*, not requests: tbank covers the
    # region with several tiles fired within seconds of each other, so per-request
    # medians are milliseconds and every normal 5-minute interval looks like an
    # outage. Collapse anything less than a minute apart into one cycle first.
    gaps = []
    for src in [r["source"] for r in by]:
        ts = [r[0] for r in c.execute(
            "SELECT DISTINCT started_at FROM poll WHERE source=? AND started_at"
            " BETWEEN ? AND ? ORDER BY 1", (src, f.frm, f.to))]
        cycles = [t for i, t in enumerate(ts) if i == 0 or t - ts[i - 1] > 60000]
        d = sorted(b - a for a, b in zip(cycles, cycles[1:]))
        if len(d) < 4:
            continue
        med = d[len(d) // 2]
        for a, b in zip(cycles, cycles[1:]):
            if b - a > 2 * med and b - a > 120000:
                gaps.append({"source": src, "from": a, "to": b, "ms": b - a})
    return {"polls": polls, "bySource": by, "gaps": sorted(gaps, key=lambda g: -g["ms"])[:50],
            "bodies": disk[0], "diskBytes": disk[1], "from": f.frm, "to": f.to}


def ep_links(c, q):
    """Identity audit: how provider records were merged, worst first."""
    rows = [dict(r) for r in c.execute(
        "SELECT s.id, s.source, s.ext_id, s.name, s.brand, s.link_method, s.link_dist_m,"
        " s.brand_match, s.confirmed, p.id place_id, p.name place_name, p.brand place_brand,"
        " p.sources FROM station s LEFT JOIN place p ON p.id=s.place_id"
        " ORDER BY s.link_dist_m IS NULL, s.link_dist_m DESC LIMIT 500")]
    hist = [dict(r) for r in c.execute(
        "SELECT CAST(link_dist_m/10 AS INT)*10 bucket, COUNT(*) n FROM station"
        " WHERE link_dist_m IS NOT NULL GROUP BY bucket ORDER BY bucket")]
    return {"stations": rows, "hist": hist,
            "multi": c.execute("SELECT COUNT(*) FROM place WHERE sources LIKE '%,%'").fetchone()[0],
            "total": c.execute("SELECT COUNT(*) FROM place").fetchone()[0]}


ENDPOINTS = {"meta": ep_meta, "places": ep_places, "timeline": ep_timeline,
             "stats": ep_stats, "coverage": ep_coverage, "links": ep_links}


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path in ("/", "/index.html", "/history.html"):
            return self.send_file(PAGE, "text/html; charset=utf-8")
        if u.path.startswith("/api/"):
            name = u.path[5:]
            fn = ENDPOINTS.get(name)
            if not fn:
                return self.send_error(404, "no endpoint %s" % name)
            q = urllib.parse.parse_qs(u.query, keep_blank_values=True)
            t0 = time.time()
            try:
                c = db()
                body = json.dumps(fn(c, q), ensure_ascii=False, default=str).encode()
                c.close()
            except Exception as e:
                return self.send_error(500, "%s: %s" % (type(e).__name__, e))
            print("  %-9s %4d ms  %6.1f KB  %s" % (name, (time.time() - t0) * 1000,
                                                   len(body) / 1024, u.query[:70]),
                  flush=True)
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return self.wfile.write(body)
        self.send_error(404)

    def send_file(self, path, ctype):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            return self.send_error(404, "missing %s" % os.path.basename(path))
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    if not os.path.exists(DB_PATH):
        sys.exit("no %s -- run collector.py, then importer.py" % DB_PATH)
    # NOT on Windows: there SO_REUSEADDR lets a second server bind a port that is
    # already served, and the two then split requests at random -- an edit appears
    # to take effect only half the time. Better to fail the second launch loudly.
    socketserver.ThreadingTCPServer.allow_reuse_address = os.name != "nt"
    with socketserver.ThreadingTCPServer(("", port), Handler) as srv:
        print("fuel history on http://localhost:%d  (db %s)" % (port, DB_PATH), flush=True)
        srv.serve_forever()


if __name__ == "__main__":
    main()
