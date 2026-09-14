"""Phase 1: turn the raw archive collected by collector.py into queryable history.

Two clocks are kept apart on every row and must never be mixed: `reported_at` is
the provider's own stamp for the thing it is describing, `first_seen`/`last_seen`
are when we watched them say it. Conflating those is the bug that the map's
freshness filter kept re-learning the hard way.

State is stored as change-runs, not samples: a row is inserted only when a value
differs from the open run for that key, otherwise the open run's `last_seen` is
extended. 288 polls a day over ~1800 stations would be ~2.6M near-identical rows;
as runs it is a couple of percent of that, and "was there 95 at 14:00" becomes an
interval lookup instead of a nearest-sample guess.

Safe to re-run: it resumes from the last imported poll, and --rebuild replays the
whole archive from scratch.

Run:  python importer.py [--rebuild] [--link-only]
"""
import gzip
import json
import os
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "fuel.db")
RAW_DIR = os.path.join(HERE, "raw")

# alfa ships the whole country; keep the same window the collector polls for
REGION = (59.80, 30.10, 60.10, 30.60)

# ---------- vocabularies (kept in step with index.html) ----------

TBANK_FUEL = {"92": "92", "95": "95", "98": "98", "100": "100", "diesel": "diesel",
              "propane": "propane", "methane": "methane"}
SBER_FUEL = {"ai80": "80", "ai92": "92", "ai95": "95", "ai98": "98", "ai100": "100",
             "ai98_100": "98/100", "diesel": "diesel", "propane": "propane",
             "methane": "methane"}
ALFA_FUEL = {"AI92": "92", "AI95": "95", "AI98_100": "98/100", "DIESEL": "diesel"}

# canonical statuses: available | maybe_available | not_available | no_data
#                   | stale (sber: was available, data is old)
#                   | closed (alfa: the station does not sell this fuel at all)
# the provider's own word is kept alongside in status_raw -- canonical buckets
# are for querying, and nothing that was said should be lost to them
TBANK_STATUS = {"available": "available", "not_available": "not_available",
                "maybe_available": "maybe_available", "no_data": "no_data"}
SBER_STATUS = {"available": "available", "unavailable": "not_available",
               "stale": "stale", "unknown": "no_data", None: "no_data"}
ALFA_STATUS = {"available": "available", "unavailable": "not_available",
               "probably_unavailable": "maybe_available", "unknown": "no_data",
               "closed": "closed"}

# Providers spell the same network differently: sber tacks the station type onto
# the name ("ЛУКОЙЛ АЗС", "Сургутнефтегаз заправочная станция"), and the three
# disagree on script ("Teboil" / "Тебойл"). Strip the type, then fold the rest.
#
# Brand is only ever a *veto* on a pair that mutual-nearest already matched, so
# these lean toward equating: a false veto throws away a real link, while a false
# equate merely lets distance decide, which is where we started.
BRAND_SUFFIX = tuple(sorted(
    ("азс", "агзс", "агнкс", "акз", "азк", "заправочнаястанция", "заправка",
     "газозаправочнаястанция", "автозаправочнаястанция", "станция",
     "самообслуживания", "газо"), key=len, reverse=True))
# a name that is only a station type says nothing about the network
GENERIC_BRAND = {"азс", "агзс", "агнкс", "азк", "акз", "заправка", "станция",
                 "заправочнаястанция", "автозаправочнаястанция",
                 "газозаправочнаястанция", "азссамообслуживания", "автомат",
                 "premium", "азссамообслуживание"}
BRAND_ALIAS = {
    "газпромнефть": "газпромнефть", "gazpromneft": "газпромнефть",
    "газпромгазомоторноетопливо": "газпромгмт", "газпромгмт": "газпромгмт",
    "лукойл": "лукойл", "lukoil": "лукойл",
    "татнефть": "татнефть", "tatneft": "татнефть",
    "роснефть": "роснефть", "rosneft": "роснефть",
    "тебойл": "teboil", "teboil": "teboil",
    "шелл": "shell", "shell": "shell",
    "северозападнаятопливнаякомпания": "сзтк", "сзтк": "сзтк",
    "сигмагаз": "sigmagas", "sigmagas": "sigmagas",
    "вервекс": "vervex", "vervex": "vervex",
    "бензоточка": "benzo", "benzo": "benzo",
    "киришпетролеум": "киришpetroleum", "киришиавтосервис": "кириши",
    "ленинградскийгаз": "ленинградскийгаз",
}

LINK_RADIUS_M = 150.0

SCHEMA = """
PRAGMA journal_mode=WAL;

-- the real-world station: one row per physical forecourt, however many
-- providers describe it
CREATE TABLE IF NOT EXISTS place (
  id    INTEGER PRIMARY KEY,
  name  TEXT, brand TEXT, addr TEXT,
  lat   REAL, lon REAL,
  sources TEXT
);

-- a provider's own record. Kept separate from place so that re-running the
-- linker can never disturb history: every historical row keys on station_id,
-- and a corrected link only moves which place it rolls up to
CREATE TABLE IF NOT EXISTS station (
  id        INTEGER PRIMARY KEY,
  source    TEXT NOT NULL,
  ext_id    TEXT NOT NULL,
  place_id  INTEGER,
  name TEXT, brand TEXT, addr TEXT, lat REAL, lon REAL,
  ulid      TEXT,          -- tbank only: its volatile per-reindex id
  first_seen INTEGER, last_seen INTEGER,
  link_method TEXT, link_dist_m REAL, brand_match INTEGER,
  confirmed INTEGER NOT NULL DEFAULT 0,
  UNIQUE (source, ext_id)
);
CREATE INDEX IF NOT EXISTS station_place ON station(place_id);

-- per-fuel state as change-runs
CREATE TABLE IF NOT EXISTS fuel_state (
  id          INTEGER PRIMARY KEY,
  station_id  INTEGER NOT NULL,
  fuel        TEXT NOT NULL,
  status      TEXT NOT NULL,
  status_raw  TEXT,
  price       REAL,
  limit_l     REAL,
  restriction TEXT,
  reported_at INTEGER,
  first_poll  INTEGER, last_poll INTEGER,
  first_seen  INTEGER NOT NULL, last_seen INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS fs_key  ON fuel_state(station_id, fuel, first_seen);
CREATE INDEX IF NOT EXISTS fs_time ON fuel_state(first_seen, last_seen);

-- station-level state as change-runs
CREATE TABLE IF NOT EXISTS station_state (
  id          INTEGER PRIMARY KEY,
  station_id  INTEGER NOT NULL,
  status      TEXT,
  status_raw  TEXT,
  confidence  REAL,
  last_payment_at INTEGER,
  ops_count   INTEGER,
  tx_24h      INTEGER,
  crowd       TEXT,
  first_poll  INTEGER, last_poll INTEGER,
  first_seen  INTEGER NOT NULL, last_seen INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ss_key ON station_state(station_id, first_seen);

-- point-in-time facts. kind='payment' means money moved; kind='report' means
-- somebody stated a verdict -- tbank's fuel events are reports, not payments,
-- and merging the two is what dated a green "есть" with a "нет" sighting
CREATE TABLE IF NOT EXISTS fuel_event (
  station_id  INTEGER NOT NULL,
  fuel        TEXT NOT NULL,   -- '' = station-level
  kind        TEXT NOT NULL,
  status      TEXT,
  reported_at INTEGER NOT NULL,
  text        TEXT,
  seen_poll   INTEGER,
  PRIMARY KEY (station_id, fuel, kind, reported_at)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ev_time ON fuel_event(reported_at);
CREATE INDEX IF NOT EXISTS ev_fuel ON fuel_event(fuel, kind, reported_at);

CREATE TABLE IF NOT EXISTS import_state (k TEXT PRIMARY KEY, v TEXT);
"""

DERIVED = ["fuel_event", "fuel_state", "station_state", "station", "place", "import_state"]


def log(msg):
    print("%s  %s" % (time.strftime("%H:%M:%S", time.gmtime()), msg), flush=True)


def ms(iso):
    """ISO8601 (Z or +03:00) -> epoch ms, or None."""
    if not iso:
        return None
    try:
        import datetime
        return int(datetime.datetime.fromisoformat(
            iso.replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return None


def norm_brand(s):
    if not s:
        return None
    k = "".join(c for c in s.lower() if c.isalnum())
    if k in GENERIC_BRAND:
        return None
    changed = True
    while changed:                            # "вервексгазозаправочнаястанция"
        changed = False                       #   -> "вервексгазо" -> "вервекс"
        for suf in BRAND_SUFFIX:
            if k.endswith(suf) and len(k) > len(suf) + 2:
                k, changed = k[:-len(suf)], True
                break
    if k in GENERIC_BRAND or len(k) < 3:
        return None
    return BRAND_ALIAS.get(k, k)


def brand_agrees(a, b):
    """Unknown on either side is not disagreement. Otherwise equal after
    folding, or one a prefix of the other ("пропан" / "пропан24")."""
    if not a or not b:
        return None
    if a == b:
        return True
    n = min(len(a), len(b))
    return True if n >= 6 and (a.startswith(b) or b.startswith(a)) else False


# Under this distance the two records are the same forecourt whatever their signs
# say -- provider names are noisy, geometry at 10 m is not. The brand veto exists
# to stop a 127 m nearest-match grabbing the competitor across the junction, so
# it only applies past this radius.
BRAND_VETO_ABOVE_M = 40.0


def haversine_m(a_lat, a_lon, b_lat, b_lon):
    from math import radians, sin, cos, asin, sqrt
    dlat = radians(b_lat - a_lat)
    dlon = radians(b_lon - a_lon)
    h = sin(dlat / 2) ** 2 + cos(radians(a_lat)) * cos(radians(b_lat)) * sin(dlon / 2) ** 2
    return 2 * 6371000 * asin(sqrt(h))


# ---------- parsing: provider JSON -> normalised records ----------

def rec(ext_id, **kw):
    r = {"ext_id": str(ext_id), "name": None, "brand": None, "addr": None,
         "lat": None, "lon": None, "ulid": None, "status": None, "status_raw": None,
         "confidence": None, "last_payment_at": None, "ops_count": None,
         "tx_24h": None, "crowd": None, "fuels": [], "events": []}
    r.update(kw)
    return r


def parse_tbank(body):
    out = []
    for s in json.loads(body).get("payload") or []:
        # the ULID is regenerated when the provider reindexes (every id in a
        # response shares a minting timestamp), so identity hangs off yandexOrgId
        ext = s.get("yandexOrgId") or s.get("id")
        r = rec(ext, ulid=s.get("id"), name=s.get("name"), brand=s.get("brand"),
                addr=s.get("addr"), lat=s.get("lat"), lon=s.get("lon"),
                status=TBANK_STATUS.get(s.get("status"), "no_data"),
                status_raw=s.get("status"), confidence=s.get("confidence"),
                last_payment_at=ms(s.get("lastTransactionAt")))
        events = s.get("recentEvents") or []
        for e in events:
            t = ms(e.get("lastUpdatedAt"))
            if t is None:
                continue
            if e.get("type") == "fuel":
                f = TBANK_FUEL.get(e.get("fuelType"))
                if f:
                    r["events"].append((f, "report", TBANK_STATUS.get(e.get("status")),
                                        t, e.get("text")))
            elif e.get("type") == "transaction":
                r["events"].append(("", "payment", None, t, e.get("text")))
            else:
                r["events"].append(("", e.get("type") or "other", None, t, e.get("text")))
        prices = s.get("priceByFuelType") or {}
        for raw, st in (s.get("statusByFuelType") or {}).items():
            f = TBANK_FUEL.get(raw)
            if not f:
                continue
            canon = TBANK_STATUS.get(st, "no_data")
            # a sighting dates the verdict it carries and no other: only an
            # event that said what is being shown may date it
            stamp = max((t for (ef, kind, es, t, _) in r["events"]
                         if kind == "report" and ef == f and es == canon), default=None)
            r["fuels"].append({"fuel": f, "status": canon, "status_raw": st,
                               "price": prices.get(raw), "limit_l": None,
                               "restriction": None, "reported_at": stamp})
        out.append(r)
    return out


def parse_sber(body):
    out = []
    for s in json.loads(body).get("stations") or []:
        loc = s.get("location") or {}
        crowd = s.get("crowdState") or {}
        r = rec(s.get("id"), name=s.get("name"), addr=s.get("address"),
                lat=loc.get("lat"), lon=loc.get("lon"),
                status=SBER_STATUS.get(s.get("availabilityStatus"), "no_data"),
                status_raw=s.get("availabilityStatus"),
                last_payment_at=ms(s.get("lastPaymentAt")),
                ops_count=s.get("operationsCount"),
                crowd="%s %+d/%d" % (crowd.get("status"), crowd.get("positiveVotes") or 0,
                                     crowd.get("negativeVotes") or 0) if crowd else None)
        if r["last_payment_at"]:
            r["events"].append(("", "payment", None, r["last_payment_at"], None))
        for f in s.get("fuels") or []:
            k = SBER_FUEL.get(f.get("type"))
            if not k:
                continue
            canon = SBER_STATUS.get(f.get("availabilityStatus"), "no_data")
            # sber does carry a per-fuel clock after all: lastFuelingAt, on a
            # minority of entries. It is a refuelling, so it is a payment event
            stamp = ms(f.get("lastFuelingAt"))
            if stamp:
                r["events"].append((k, "payment", canon, stamp, None))
            r["fuels"].append({"fuel": k, "status": canon,
                               "status_raw": f.get("availabilityStatus"),
                               "price": None, "limit_l": f.get("limitLiters"),
                               "restriction": None, "reported_at": stamp})
        out.append(r)
    return out


def parse_alfa(body):
    lo_lat, lo_lon, hi_lat, hi_lon = REGION
    out = []
    for s in json.loads(body):
        loc = ((s.get("address") or {}).get("location")) or {}
        lat, lon = loc.get("latitude"), loc.get("longitude")
        if lat is None or not (lo_lat <= lat <= hi_lat and lo_lon <= lon <= hi_lon):
            continue
        r = rec(s.get("station_id"), brand=(s.get("brand") or {}).get("name"),
                addr=(s.get("address") or {}).get("fullname"), lat=lat, lon=lon,
                last_payment_at=ms(s.get("last_alfa_transaction_time")),
                tx_24h=s.get("last_24h_alfa_transactions_count"))
        r["name"] = r["brand"]
        if r["last_payment_at"]:
            r["events"].append(("", "payment", None, r["last_payment_at"], None))
        for f in s.get("fuels") or []:
            k = ALFA_FUEL.get(f.get("category"))
            if not k:
                continue
            canon = ALFA_STATUS.get(f.get("status"), "no_data")
            stamp = ms(f.get("last_transaction_at"))
            if stamp:
                # a transaction for this fuel: a real payment bound to a fuel
                r["events"].append((k, "payment", canon, stamp, None))
            rs = sorted({("%s:%s" % (x.get("type"), x.get("limit"))
                          if x.get("limit") is not None else str(x.get("type")))
                         for x in f.get("restrictions") or []})
            lim = [x.get("limit") for x in f.get("restrictions") or []
                   if x.get("type") == "limit" and x.get("limit") is not None]
            r["fuels"].append({"fuel": k, "status": canon, "status_raw": f.get("status"),
                               "price": f.get("price"), "limit_l": min(lim) if lim else None,
                               "restriction": ";".join(rs) or None, "reported_at": stamp})
        out.append(r)
    return out


PARSERS = {"tbank": parse_tbank, "sber": parse_sber, "alfa": parse_alfa}


# ---------- run bookkeeping ----------

class Runs:
    """Open change-runs held in memory: extend while a value repeats, insert on
    change. Loaded up front so an incremental import continues existing runs
    rather than starting a duplicate one on every restart."""

    def __init__(self, db):
        self.db = db
        self.fuel = {}
        for row in db.execute(
                "SELECT id, station_id, fuel, status, status_raw, price, limit_l,"
                " restriction, reported_at FROM fuel_state WHERE id IN"
                " (SELECT MAX(id) FROM fuel_state GROUP BY station_id, fuel)"):
            self.fuel[(row[1], row[2])] = (row[0], row[3:])
        self.station = {}
        for row in db.execute(
                "SELECT id, station_id, status, status_raw, confidence, last_payment_at,"
                " ops_count, tx_24h, crowd FROM station_state WHERE id IN"
                " (SELECT MAX(id) FROM station_state GROUP BY station_id)"):
            self.station[row[1]] = (row[0], row[2:])

    def fuel_obs(self, sid, f, poll, at):
        key = (sid, f["fuel"])
        v = (f["status"], f["status_raw"], f["price"], f["limit_l"],
             f["restriction"], f["reported_at"])
        cur = self.fuel.get(key)
        if cur and cur[1] == v:
            self.db.execute("UPDATE fuel_state SET last_poll=?, last_seen=? WHERE id=?",
                            (poll, at, cur[0]))
            return 0
        cur = self.db.execute(
            "INSERT INTO fuel_state (station_id, fuel, status, status_raw, price, limit_l,"
            " restriction, reported_at, first_poll, last_poll, first_seen, last_seen)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, f["fuel"]) + v + (poll, poll, at, at))
        self.fuel[key] = (cur.lastrowid, v)
        return 1

    def station_obs(self, sid, r, poll, at):
        v = (r["status"], r["status_raw"], r["confidence"], r["last_payment_at"],
             r["ops_count"], r["tx_24h"], r["crowd"])
        cur = self.station.get(sid)
        if cur and cur[1] == v:
            self.db.execute("UPDATE station_state SET last_poll=?, last_seen=? WHERE id=?",
                            (poll, at, cur[0]))
            return 0
        cur = self.db.execute(
            "INSERT INTO station_state (station_id, status, status_raw, confidence,"
            " last_payment_at, ops_count, tx_24h, crowd, first_poll, last_poll,"
            " first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid,) + v + (poll, poll, at, at))
        self.station[sid] = (cur.lastrowid, v)
        return 1


# ---------- import ----------

def station_ids(db):
    return {(s, e): i for i, s, e in db.execute("SELECT id, source, ext_id FROM station")}


def import_polls(db, rebuild=False):
    if rebuild:
        log("rebuild: dropping derived tables")
        for t in DERIVED:
            db.execute("DROP TABLE IF EXISTS " + t)
        db.commit()
    db.executescript(SCHEMA)
    db.commit()

    last = db.execute("SELECT v FROM import_state WHERE k='last_poll'").fetchone()
    last = int(last[0]) if last else 0
    todo = db.execute(
        "SELECT p.id, p.source, p.started_at, b.path FROM poll p JOIN blob b USING(sha256)"
        " WHERE p.id > ? AND p.error IS NULL AND b.pruned = 0 ORDER BY p.id", (last,)
    ).fetchall()
    if not todo:
        log("nothing to import (last poll %d)" % last)
        return

    ids = station_ids(db)
    runs = Runs(db)
    t0 = time.time()
    n_new = n_ev = n_run = 0
    for i, (poll, source, at, rel) in enumerate(todo, 1):
        full = os.path.join(RAW_DIR, rel.replace("/", os.sep))
        try:
            with gzip.open(full, "rb") as f:
                body = f.read()
            records = PARSERS[source](body)
        except Exception as e:
            log("poll %d (%s): %s -- skipped" % (poll, source, e))
            continue
        for r in records:
            key = (source, r["ext_id"])
            sid = ids.get(key)
            if sid is None:
                cur = db.execute(
                    "INSERT INTO station (source, ext_id, name, brand, addr, lat, lon,"
                    " ulid, first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (source, r["ext_id"], r["name"], r["brand"], r["addr"],
                     r["lat"], r["lon"], r["ulid"], at, at))
                sid = ids[key] = cur.lastrowid
                n_new += 1
            else:
                db.execute("UPDATE station SET last_seen=?, name=?, brand=?, addr=?,"
                           " lat=?, lon=?, ulid=? WHERE id=?",
                           (at, r["name"], r["brand"], r["addr"], r["lat"], r["lon"],
                            r["ulid"], sid))
            n_run += runs.station_obs(sid, r, poll, at)
            for f in r["fuels"]:
                n_run += runs.fuel_obs(sid, f, poll, at)
            for (fuel, kind, st, rep, text) in r["events"]:
                db.execute(
                    "INSERT OR IGNORE INTO fuel_event (station_id, fuel, kind, status,"
                    " reported_at, text, seen_poll) VALUES (?,?,?,?,?,?,?)",
                    (sid, fuel, kind, st, rep, text, poll))
                n_ev += db.total_changes and 0 or 0
        db.execute("INSERT INTO import_state (k, v) VALUES ('last_poll', ?)"
                   " ON CONFLICT(k) DO UPDATE SET v=excluded.v", (str(poll),))
        if i % 25 == 0 or i == len(todo):
            db.commit()
            log("  %d/%d polls" % (i, len(todo)))
    db.commit()
    n_ev = db.execute("SELECT COUNT(*) FROM fuel_event").fetchone()[0]
    log("imported %d polls in %.1fs: %d new stations, %d runs written, %d events total"
        % (len(todo), time.time() - t0, n_new, n_run, n_ev))


# ---------- identity ----------

def link_places(db):
    """Group provider records into real-world places.

    Two guards the map's live matcher does not have, and the reason a
    Сургутнефтегаз once wore a Роснефть 127 m away: the match must be *mutual*
    (each side's nearest is the other), and brands must agree when both sides
    state one. Distance alone decides only when a brand is missing.
    """
    rows = [dict(id=i, source=s, name=n, brand=b, lat=la, lon=lo)
            for i, s, n, b, la, lo in db.execute(
                "SELECT id, source, name, brand, lat, lon FROM station"
                " WHERE lat IS NOT NULL AND confirmed = 0")]
    by_src = {}
    for r in rows:
        r["nb"] = norm_brand(r["brand"]) or norm_brand(r["name"])
        by_src.setdefault(r["source"], []).append(r)

    parent = {r["id"]: r["id"] for r in rows}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    link = {}
    rejected = 0
    for a_src, b_src in (("tbank", "sber"), ("tbank", "alfa"), ("sber", "alfa")):
        A, B = by_src.get(a_src) or [], by_src.get(b_src) or []
        if not A or not B:
            continue

        def nearest(src, dst):
            out = {}
            for x in src:
                best, bd = None, LINK_RADIUS_M
                for y in dst:
                    d = haversine_m(x["lat"], x["lon"], y["lat"], y["lon"])
                    if d < bd:
                        bd, best = d, y
                out[x["id"]] = (best, bd) if best else (None, None)
            return out

        fwd, rev = nearest(A, B), nearest(B, A)
        for x in A:
            y, d = fwd[x["id"]]
            if not y or rev[y["id"]][0] is not x:      # must be mutual
                continue
            agree = brand_agrees(x["nb"], y["nb"])
            if agree is False and d > BRAND_VETO_ABOVE_M:
                rejected += 1
                continue
            ra, rb = find(x["id"]), find(y["id"])
            if ra != rb:
                parent[rb] = ra
            match = 1 if agree else None
            link[y["id"]] = ("mutual", d, match)
            link.setdefault(x["id"], ("mutual", d, match))

    groups = {}
    for r in rows:
        groups.setdefault(find(r["id"]), []).append(r)

    db.execute("DELETE FROM place")
    db.execute("UPDATE station SET place_id=NULL, link_method=NULL, link_dist_m=NULL,"
               " brand_match=NULL WHERE confirmed=0")
    order = {"tbank": 0, "alfa": 1, "sber": 2}
    for members in groups.values():
        members.sort(key=lambda r: order.get(r["source"], 9))
        name = next((m["name"] for m in members if m["name"]), None)
        brand = next((m["brand"] for m in members if m["brand"]), None)
        lat = sum(m["lat"] for m in members) / len(members)
        lon = sum(m["lon"] for m in members) / len(members)
        addr = db.execute("SELECT addr FROM station WHERE id=? AND addr IS NOT NULL",
                          (members[0]["id"],)).fetchone()
        pid = db.execute(
            "INSERT INTO place (name, brand, addr, lat, lon, sources) VALUES (?,?,?,?,?,?)",
            (name, brand, addr[0] if addr else None, lat, lon,
             ",".join(sorted({m["source"] for m in members})))).lastrowid
        for m in members:
            lm, d, bm = link.get(m["id"], ("single", None, None))
            db.execute("UPDATE station SET place_id=?, link_method=?, link_dist_m=?,"
                       " brand_match=? WHERE id=?", (pid, lm, d, bm, m["id"]))
    db.commit()

    multi = db.execute("SELECT COUNT(*) FROM place WHERE sources LIKE '%,%'").fetchone()[0]
    log("linked %d records into %d places (%d multi-source, %d rejected on brand)"
        % (len(rows), len(groups), multi, rejected))


def main():
    args = set(sys.argv[1:])
    db = sqlite3.connect(DB_PATH)
    db.execute("PRAGMA synchronous=NORMAL")
    if "--link-only" not in args:
        import_polls(db, rebuild="--rebuild" in args)
    else:
        db.executescript(SCHEMA)
    link_places(db)
    for label, q in (("places", "SELECT COUNT(*) FROM place"),
                     ("stations", "SELECT COUNT(*) FROM station"),
                     ("fuel runs", "SELECT COUNT(*) FROM fuel_state"),
                     ("station runs", "SELECT COUNT(*) FROM station_state"),
                     ("events", "SELECT COUNT(*) FROM fuel_event")):
        log("%-13s %d" % (label, db.execute(q).fetchone()[0]))
    db.close()


if __name__ == "__main__":
    main()
