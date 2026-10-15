import csv
import io
import logging
import os
import re
import sqlite3
import threading
import time
import zipfile

import requests
import yaml

from . import geo
from .metrics import geocode_requests

log = logging.getLogger(__name__)

STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia",
    "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}
STATE_BY_NAME = {name.lower(): code for code, name in STATES.items()}
AP_STATES = {
    "ala.": "AL", "ariz.": "AZ", "ark.": "AR", "calif.": "CA", "colo.": "CO", "conn.": "CT",
    "del.": "DE", "fla.": "FL", "ga.": "GA", "ill.": "IL", "ind.": "IN", "kan.": "KS",
    "ky.": "KY", "la.": "LA", "md.": "MD", "mass.": "MA", "mich.": "MI", "minn.": "MN",
    "miss.": "MS", "mo.": "MO", "mont.": "MT", "neb.": "NE", "nev.": "NV", "n.h.": "NH",
    "n.j.": "NJ", "n.m.": "NM", "n.y.": "NY", "n.c.": "NC", "n.d.": "ND", "okla.": "OK",
    "ore.": "OR", "pa.": "PA", "r.i.": "RI", "s.c.": "SC", "s.d.": "SD", "tenn.": "TN",
    "vt.": "VT", "va.": "VA", "wash.": "WA", "w.va.": "WV", "wis.": "WI", "wyo.": "WY",
}

NON_US_CUES = re.compile(
    r"\b(Ireland|Irish|Northern Ireland|England|Scotland|Wales|U\.K\.|UK|Britain|British Columbia|"
    r"Ontario|Quebec|Alberta|Manitoba|Saskatchewan|Nova Scotia|New Brunswick|Canada|Canadian|"
    r"Australia|New Zealand|OPP|RCMP)\b|\bCounty\s+(?:Louth|Tyrone|Cork|Kerry|Donegal|Mayo|Galway|"
    r"Antrim|Down|Armagh|Fermanagh|Londonderry|Derry|Dublin|Meath|Wexford|Kildare|Limerick|Clare)\b"
)

NON_US_OUTLETS = re.compile(r"^(BBC|CBC|CTV|ITV|ITVX|Sky News|RT[ÉE]|Global News|Irish \w+|The Irish \w+)\b")

PLACE_SUFFIX = re.compile(
    r"\s+(?:city and borough|unified government|consolidated government|metropolitan government|"
    r"metro government|urban county|city|town|village|borough|municipality|corporation|CDP|"
    r"comunidad|zona urbana|charter township|township|plantation)(?:\s+\(balance\))?$"
)
COUNTY_SUFFIX = re.compile(r"\s+(?:County|Parish|Borough|City and Borough|Census Area|Municipality|city|City)$")

LOCATION_WORDS = r"in|near|outside|of|between|and"
QUALIFIERS = {"north", "south", "east", "west", "northeast", "northwest", "southeast", "southwest",
              "central", "downtown", "greater", "suburban", "rural", "northern", "southern", "eastern",
              "western", "upper", "lower"}
CAP_WORD = r"(?:[A-Z][\w'’.\-]*|Mt\.|St\.|Ft\.)"
CONNECTORS = {"de", "del", "la", "le", "of"}
STOP_WORDS = {"on", "at", "after", "for", "with", "and", "to", "from", "by", "as", "near", "in", "into",
              "following", "during", "over", "under", "off", "along", "amid", "while", "between"}
PLACE_SUFFIX_WORDS = ("Twp.", "Twp", "Township", "Borough", "Village")
ROAD_SUFFIX_WORDS = {"road", "rd", "rd.", "street", "st", "st.", "avenue", "ave", "ave.", "boulevard", "blvd", "blvd.",
                     "drive", "dr", "dr.", "lane", "ln", "way", "highway", "hwy", "hwy.", "parkway", "pkwy",
                     "expressway", "freeway", "turnpike", "bridge", "pike", "trail", "circle", "court", "bypass"}


def norm(name):
    n = name.lower().replace("’", "'").strip()
    n = re.sub(r"\bst\.?\s+", "saint ", n)
    n = re.sub(r"\bmt\.?\s+", "mount ", n)
    n = re.sub(r"\bft\.?\s+", "fort ", n)
    n = n.replace(".", "")
    n = re.sub(r"'s$", "", n)
    return re.sub(r"\s+", " ", n).strip()


def _gazetteer_names(raw, kind):
    base = (COUNTY_SUFFIX if kind == "county" else PLACE_SUFFIX).sub("", raw)
    m = re.match(r"^(.*?)\s*\((.*?)\)\s*$", base)
    names = [m.group(1), m.group(2)] if m else [base]
    if kind == "place" and "County" in raw:
        names += [re.split(r"[-/]", n)[0] for n in names if re.search(r"[-/]", n)]
    return [norm(n) for n in names if n]


def build_gazetteer(files, db_path):
    """Load Census Gazetteer files (zip or pipe-delimited txt) into SQLite."""
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    tmp = db_path + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    con = sqlite3.connect(tmp)
    con.execute("CREATE TABLE places (name TEXT, display TEXT, state TEXT, kind TEXT, lat REAL, lon REAL, geoid TEXT)")
    count = 0
    for kind, path in files.items():
        for row in _read_gazetteer(path):
            if kind == "cousub" and row.get("FUNCSTAT") not in ("A", "B", "C"):
                continue
            if row["USPS"] not in STATES:
                continue
            lat, lon = float(row["INTPTLAT"]), float(row["INTPTLONG"])
            for n in set(_gazetteer_names(row["NAME"], kind)):
                con.execute("INSERT INTO places VALUES (?,?,?,?,?,?,?)",
                            (n, row["NAME"], row["USPS"], kind, lat, lon, row["GEOID"]))
                count += 1
    con.execute("CREATE INDEX ix_places_name ON places(name, kind)")
    con.commit()
    con.close()
    os.replace(tmp, db_path)
    return count


def _read_gazetteer(path):
    if path.endswith(".zip"):
        with zipfile.ZipFile(path) as z:
            member = next(n for n in z.namelist() if n.endswith(".txt"))
            text = z.read(member).decode("latin-1")
    else:
        with open(path, encoding="latin-1") as f:
            text = f.read()
    reader = csv.reader(io.StringIO(text), delimiter="|")
    header = [h.strip() for h in next(reader)]
    for row in reader:
        yield dict(zip(header, (c.strip() for c in row)))


class Gazetteer:
    def __init__(self, db_path):
        self.db_path = db_path
        self._local = threading.local()

    @property
    def available(self):
        return os.path.exists(self.db_path)

    def _con(self):
        con = getattr(self._local, "con", None)
        if con is None:
            con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, check_same_thread=False)
            self._local.con = con
        return con

    def lookup(self, name, kinds):
        key = norm(name)
        marks = ",".join("?" for _ in kinds)
        rows = self._con().execute(
            f"SELECT display, state, kind, lat, lon FROM places WHERE name = ? AND kind IN ({marks})",
            (key, *kinds),
        ).fetchall()
        return _dedupe([{"display": r[0], "state": r[1], "kind": r[2], "lat": r[3], "lon": r[4]} for r in rows])

    def nearest_state(self, lat, lon, max_km=40):
        rows = self._con().execute(
            "SELECT state, lat, lon FROM places WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?",
            (lat - 0.5, lat + 0.5, lon - 0.6, lon + 0.6),
        ).fetchall()
        best = None
        for state, plat, plon in rows:
            d = geo.haversine_km(lat, lon, plat, plon)
            if d <= max_km and (best is None or d < best[0]):
                best = (d, state)
        return best[1] if best else None


def _dedupe(matches, km=20):
    """Same name, same state, within km: one real place listed as both place and town."""
    out = []
    for m in sorted(matches, key=lambda m: {"place": 0, "cousub": 1, "county": 2}[m["kind"]]):
        if any(o["state"] == m["state"] and (o["kind"] == "county") == (m["kind"] == "county")
               and geo.haversine_km(o["lat"], o["lon"], m["lat"], m["lon"]) <= km for o in out):
            continue
        out.append(m)
    return out


def load_outlets(path):
    if not path or not os.path.exists(path):
        return {"markets": {}, "non_us": set()}
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    markets = {k.strip().lower(): [s.upper() for s in v] for k, v in (data.get("markets") or {}).items()}
    non_us = {k.strip().lower() for k in data.get("non_us") or []}
    return {"markets": markets, "non_us": non_us}


def split_outlet(headline):
    """Google News titles end with ' - <outlet>'."""
    title, sep, outlet = headline.rpartition(" - ")
    if sep and outlet and len(outlet) <= 80:
        return title.strip(), outlet.strip()
    return headline.strip(), None


STATE_NOT_FOLLOWED_BY = re.compile(
    r"\s+(City|Beach|County|Co\.|Parish|Twp\.?|Township|Avenue|Ave\.?|Street|St\.|Road|Rd\.?|Boulevard|"
    r"Blvd\.?|Drive|Dr\.|Lane|Way|Square|Falls|Rapids|Heights)\b"
)
LOCATION_BEFORE_STATE = re.compile(
    r"\b(?:in|across|near|of|outside|throughout)\s+(?:(?:north|south|east|west|central|northern|southern|"
    r"eastern|western|northeast|northwest|southeast|southwest|rural)\w*\s+)?$",
    re.I,
)


def _state_mentions(text):
    """Split state names in the text into location statements and other mentions."""
    strong, weak, taken = [], [], []
    for code, name in sorted(STATES.items(), key=lambda kv: -len(kv[1])):
        if code == "DC":
            continue
        for m in re.finditer(r"\b" + re.escape(name) + r"\b", text):
            if any(a <= m.start() < b for a, b in taken):
                continue
            if STATE_NOT_FOLLOWED_BY.match(text, m.end()):
                continue
            before = text[max(0, m.start() - 30):m.start()]
            if code == "WA" and not (re.search(r",\s*$", before) or text[m.end():m.end() + 6].lower() == " state"):
                continue
            taken.append((m.start(), m.end()))
            if LOCATION_BEFORE_STATE.search(before) or re.search(r",\s*$", before):
                strong.append(code)
            else:
                weak.append(code)
    for m in re.finditer(r",\s*(" + "|".join(re.escape(a) for a in AP_STATES) + r")", text, re.I):
        strong.append(AP_STATES[m.group(1).lower()])
    return strong, weak


def _place_mentions(text, title_case):
    mentions = []
    for m in re.finditer(r"\b(" + LOCATION_WORDS + r")\s+", text, re.I):
        prep = m.group(1).lower()
        if prep == "and" and not re.search(r"\bbetween\s+(?:\S+\s+){1,4}$", text[:m.start()], re.I):
            continue
        pos = m.end()
        tokens = list(re.finditer(r"\S+", text[pos:]))
        while tokens and tokens[0].group().lower().strip(",") in QUALIFIERS:
            tokens = tokens[1:]
        run = []
        for t in tokens:
            raw = t.group()
            bare = raw.strip(",;:!?\"“”()")
            if bare.lower() in STOP_WORDS or not (re.fullmatch(CAP_WORD, bare) or (run and bare.lower() in CONNECTORS)):
                break
            run.append((bare, pos + t.start() + raw.index(bare[0]) if bare else pos + t.start()))
            if raw != bare and not raw.endswith(".") or len(run) >= 5:
                break
        while run and run[-1][0].lower() in CONNECTORS:
            run.pop()
        if run:
            mentions.append({"words": [w for w, _ in run], "ends": [s + len(w) for w, s in run],
                             "start": run[0][1], "prep": prep})
    m = re.match(r"\s*((?:" + CAP_WORD + r"\s){0,3}" + CAP_WORD + r"),\s*([A-Z]{2})\s*[–—-]", text)
    if m and m.group(2) in STATES:
        words = m.group(1).split()
        mentions.insert(0, {"words": words, "ends": [m.end(1)] * len(words), "start": m.start(1), "prep": "dateline"})
    return mentions


def _county_mentions(text):
    found = []
    for m in re.finditer(r"((?:" + CAP_WORD + r"\s+){1,3})(County|county|Co\.|Parish|Borough)(?=\W|$)", text):
        words = m.group(1).split()
        found.append({"words": words, "start": m.start(1), "end": m.end()})
    return found


class Geocoder:
    def __init__(self, gazetteer, outlets=None, nominatim=None):
        self.gazetteer = gazetteer
        self.outlets = outlets or {"markets": {}, "non_us": set()}
        self.nominatim = nominatim

    def market(self, outlet):
        if not outlet:
            return []
        return self.outlets["markets"].get(outlet.strip().lower(), [])

    def is_non_us_outlet(self, outlet, domain=None):
        if outlet and (outlet.strip().lower() in self.outlets["non_us"] or NON_US_CUES.search(outlet)
                       or NON_US_OUTLETS.search(outlet)):
            return True
        if domain:
            host = re.sub(r"^https?://", "", domain).split("/")[0].lower()
            if re.search(r"\.(ca|uk|ie|au|nz)$", host):
                return True
        return False

    def resolve(self, title, outlet=None, domain=None):
        evidence = {"method": "gazetteer", "outlet": outlet, "outlet_market": self.market(outlet)}
        if self.is_non_us_outlet(outlet, domain) or NON_US_CUES.search(title):
            evidence["reason"] = "outside_coverage"
            return _unresolved(evidence)
        if not self.gazetteer or not self.gazetteer.available:
            evidence["reason"] = "gazetteer_unavailable"
            return _unresolved(evidence)

        words = [w for w in re.findall(r"[A-Za-z][\w']*", title) if len(w) > 3]
        title_case = bool(words) and sum(w[0].isupper() for w in words) / len(words) > 0.9
        strong, weak = _state_mentions(title)
        strong_states = set(strong)
        weak_states = set(weak) - strong_states
        market = set(evidence["outlet_market"])
        evidence["text_states"] = sorted(strong_states)

        candidates = []
        for c in _county_mentions(title):
            bound = _bound_state(title, c["end"])
            for size in range(len(c["words"]), 0, -1):
                name = " ".join(c["words"][-size:])
                if norm(name) in QUALIFIERS or norm(name) in STOP_WORDS:
                    continue
                result = self._match(name, ("county",), bound, strong_states, weak_states | market)
                if result:
                    result["position"] = c["start"]
                    candidates.append(result)
                    break

        for p in _place_mentions(title, title_case):
            words = p["words"]
            sizes = [len(words)] if title_case else range(len(words), 0, -1)
            for size in sizes:
                if size < len(words) and (words[size] in ("County", "Co.", "Parish")
                                          or words[size].lower() in ROAD_SUFFIX_WORDS):
                    continue
                chunk = words[:size]
                suffix_kind = None
                if chunk[-1] in PLACE_SUFFIX_WORDS:
                    suffix_kind = ("cousub", "place")
                    chunk = chunk[:-1]
                if not chunk:
                    continue
                if re.search(r"['’]s$", chunk[-1]):
                    chunk = chunk[:-1] + [re.sub(r"['’]s$", "", chunk[-1])]
                name = " ".join(chunk).rstrip(".,")
                key = norm(name)
                if key in QUALIFIERS or key.upper() in STATES or key in STATE_BY_NAME:
                    continue
                bound = _bound_state(title, p["ends"][size - 1])
                result = self._match(name, suffix_kind or ("place", "cousub"), bound, strong_states, weak_states | market)
                if result:
                    result["position"] = p["start"]
                    candidates.append(result)
                    break

        resolved = [c for c in candidates if c["confidence"] in ("geocoded_high", "geocoded_medium")]
        if resolved:
            rank = {"geocoded_high": 0, "geocoded_medium": 1}
            prec = {"city": 0, "county": 1}
            best = min(resolved, key=lambda c: (rank[c["confidence"]], prec[c["precision"]], c["position"]))
            evidence.update(best["evidence"])
            return {
                "confidence": best["confidence"], "precision": best["precision"],
                "place": best["place"], "region": best["region"],
                "lat": best["lat"], "lon": best["lon"], "evidence": evidence,
            }
        ambiguous = [c for c in candidates if c["confidence"] == "geocoded_low"]
        if ambiguous:
            evidence.update(ambiguous[0]["evidence"])
            return {"confidence": "geocoded_low", "precision": "none", "place": ambiguous[0]["place"],
                    "region": None, "lat": None, "lon": None, "evidence": evidence}
        if len(strong_states) == 1:
            evidence["reason"] = "state_only"
            out = _unresolved(evidence)
            out["precision"] = "state"
            out["region"] = next(iter(strong_states))
            return out
        evidence["reason"] = "no_place_in_text"
        return _unresolved(evidence)

    def _match(self, name, kinds, bound_state, strong_states, hint_states):
        matches = self.gazetteer.lookup(name, kinds)
        if not matches:
            return self._fallback(name, bound_state or (next(iter(strong_states)) if len(strong_states) == 1 else None))
        basis = None
        pool = matches
        if bound_state:
            pool, basis = [m for m in matches if m["state"] == bound_state], "state_next_to_place"
        elif strong_states:
            pool, basis = [m for m in matches if m["state"] in strong_states], "state_named_in_text"
        elif hint_states:
            pool, basis = [m for m in matches if m["state"] in hint_states], "outlet_market_or_state_mention"
        else:
            basis = "unique_name"
        geocode_requests.labels("gazetteer", "hit" if pool else "miss").inc()
        if not pool:
            return None
        evidence = {"text_place": name, "state_basis": basis, "matched": pool[0]["display"] if len(pool) == 1 else None,
                    "candidates": len(pool)}
        if len(pool) > 1:
            return {"confidence": "geocoded_low", "precision": "none", "place": name, "region": None,
                    "lat": None, "lon": None, "evidence": evidence}
        m = pool[0]
        confidence = "geocoded_high" if basis in ("state_next_to_place", "state_named_in_text") else "geocoded_medium"
        return {
            "confidence": confidence, "precision": "county" if m["kind"] == "county" else "city",
            "place": m["display"], "region": m["state"], "lat": m["lat"], "lon": m["lon"], "evidence": evidence,
        }

    def _fallback(self, name, state):
        if not self.nominatim or not state:
            return None
        hit = self.nominatim.city(name, state)
        if not hit:
            return None
        return {
            "confidence": "geocoded_medium", "precision": "city", "place": hit["display"], "region": state,
            "lat": hit["lat"], "lon": hit["lon"],
            "evidence": {"text_place": name, "state_basis": "fallback_geocoder", "matched": hit["display"],
                         "candidates": 1, "method": "nominatim"},
        }


def _bound_state(text, pos):
    """State written right after a place: 'Topanga, CA' or 'Mt. Laurel, New Jersey'."""
    tail = text[pos:pos + 32]
    m = re.match(r",\s*([A-Z]{2})\b", tail)
    if m and m.group(1) in STATES:
        return m.group(1)
    for code, name in sorted(STATES.items(), key=lambda kv: -len(kv[1])):
        if re.match(r",?\s*" + re.escape(name) + r"\b", tail) and not STATE_NOT_FOLLOWED_BY.match(tail, tail.find(name) + len(name)):
            return code
    for abbr, code in AP_STATES.items():
        if re.match(r",\s*" + re.escape(abbr), tail, re.I):
            return code
    return None


def _unresolved(evidence):
    return {"confidence": "unresolved", "precision": "none", "place": None, "region": None,
            "lat": None, "lon": None, "evidence": evidence}


class Nominatim:
    """Optional fallback for places missing from the gazetteer.

    Follows the public server's usage policy: one request per second at most,
    identifying User-Agent, results cached. Point base_url at a self-hosted
    instance for heavier use.
    """

    def __init__(self, base_url, user_agent, cache, min_interval=1.1, ttl_days=30, timeout=10):
        self.base_url = base_url.rstrip("/")
        self.cache = cache
        self.min_interval = min_interval
        self.ttl = ttl_days * 86400
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers["User-Agent"] = user_agent
        self._lock = threading.Lock()
        self._last = 0.0

    def city(self, name, state):
        key = f"city:{norm(name)}:{state}"
        cached = self.cache.get("nominatim", key, self.ttl)
        if cached is not None:
            geocode_requests.labels("nominatim", "cache_hit").inc()
            return cached or None
        with self._lock:
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                resp = self.session.get(
                    f"{self.base_url}/search",
                    params={"city": name, "state": STATES[state], "country": "us", "format": "jsonv2",
                            "addressdetails": 1, "limit": 1},
                    timeout=self.timeout,
                )
                resp.raise_for_status()
                rows = resp.json()
            except (requests.RequestException, ValueError) as exc:
                geocode_requests.labels("nominatim", "error").inc()
                log.warning("geocoder request failed", extra={"provider": "nominatim", "error": str(exc)})
                return None
        hit = {}
        if rows:
            addr = rows[0].get("address") or {}
            if addr.get("state", "").lower() == STATES[state].lower():
                hit = {"display": rows[0].get("display_name", name), "lat": float(rows[0]["lat"]),
                       "lon": float(rows[0]["lon"])}
        geocode_requests.labels("nominatim", "hit" if hit else "miss").inc()
        self.cache.put("nominatim", key, hit)
        return hit or None


class MemoryCache:
    def __init__(self):
        self.data = {}

    def get(self, provider, key, ttl):
        item = self.data.get((provider, key))
        if item and time.time() - item[0] < ttl:
            return item[1]
        return None

    def put(self, provider, key, value):
        self.data[(provider, key)] = (time.time(), value)


def location_from_result(result):
    loc = {k: result[k] for k in ("confidence", "precision", "place", "region")}
    loc["evidence"] = dict(result.get("evidence") or {})
    return loc
