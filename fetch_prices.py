#!/usr/bin/env python3
"""
UK fuel prices - fetcher for the nationwide page.

Run:  python3 fetch_prices.py
Needs only Python 3.8+ (no extra packages).

What it does
1. Downloads the UK government Fuel Finder CSV (every registered forecourt,
   including independents). If the official URL is blocked (it only answers
   UK connections), it falls back to a public hourly mirror of the same file.
2. Downloads the old CMA retailer JSON feeds that are still online
   (the CMA interim scheme closed on 1 May 2026; some retailers still publish).
   A retailer price is only used if that feed was updated in the last 48 hours
   and is newer than the Fuel Finder price for the same station.
3. Removes duplicates, keeps UK stations (England, Scotland, Wales,
   Northern Ireland), checks prices for obviously wrong values, then writes
   site/data.json - a compact file the page (site/index.html) loads.
"""
import csv, io, json, math, os, re, socket, statistics, sys, time
import urllib.request, urllib.error
from datetime import datetime, timezone, timedelta

try:
    from zoneinfo import ZoneInfo
    UK = ZoneInfo("Europe/London")
except Exception:  # very old Python: fall back to UTC labels
    UK = timezone.utc

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_JSON = os.path.join(HERE, "site", "data.json")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
TIMEOUT = 30

FUEL_FINDER = [
    ("Fuel Finder (gov.uk, official CSV)",
     "https://www.fuel-finder.service.gov.uk/internal/v1.0.2/csv/get-latest-fuel-prices-csv"),
    ("Fuel Finder (hourly public mirror, github.com/matthewgall/fuelfinder-archive)",
     "https://raw.githubusercontent.com/matthewgall/fuelfinder-archive/main/data.csv"),
]

LEGACY_FEEDS = [  # (retailer, url) - CMA interim scheme feeds
    ("Asda", "https://storelocator.asda.com/fuel_prices_data.json"),
    ("BP", "https://www.bp.com/en_gb/united-kingdom/home/fuelprices/fuel_prices_data.json"),
    ("Esso / Tesco Alliance", "https://fuelprices.esso.co.uk/latestdata.json"),
    ("Jet", "https://jetlocal.co.uk/fuel_prices_data.json"),
    ("Morrisons", "https://www.morrisons.com/fuel-prices/fuel.json"),
    ("Moto", "https://moto-way.com/fuel-price/fuel_prices.json"),
    ("Motor Fuel Group", "https://fuel.motorfuelgroup.com/fuel_prices_data.json"),
    ("Rontec", "https://www.rontec-servicestations.co.uk/fuel-prices/data/fuel_prices_data.json"),
    ("Sainsbury's", "https://api.sainsburys.co.uk/v1/exports/latest/fuel_prices_data.json"),
    ("SGN", "https://www.sgnretail.uk/files/data/SGN_daily_fuel_prices.json"),
    ("Shell", "https://www.shell.co.uk/fuel-prices-data.html"),
    ("Tesco", "https://www.tesco.com/fuel_prices/fuel_prices_data.json"),
    ("Applegreen", "https://applegreenstores.com/fuel-prices/data.json"),
    ("Karan Retail", "https://devapi.krlpos.com/integration/live_price/krl"),
]
LEGACY_MAX_AGE_H = 48

FUELS = ["E10", "E5", "B7", "SDV"]  # unleaded, super, diesel, premium diesel
FF_FUEL_COLS = {"E10": "E10", "E5": "E5", "B7": "B7S", "SDV": "B7P"}

REGION_AREAS = {
    "London": "E EC N NW SE SW W WC BR CR EN HA IG RM SM TW UB",
    "South East": "BN CT GU HP KT ME MK OX PO RG RH SL SO TN DA",
    "East of England": "AL CB CM CO IP LU NR PE SG SS WD",
    "South West": "BA BH BS DT EX GL PL SN SP TA TQ TR",
    "West Midlands": "B CV DY HR ST SY TF WR WS WV",
    "East Midlands": "DE LE LN NG NN",
    "Yorkshire and the Humber": "BD DN HD HG HU HX LS S WF YO",
    "North East": "DH DL NE SR TS",
    "North West": "BB BL CA CH CW FY L LA M OL PR SK WA WN",
    "Wales": "CF LD LL NP SA",
    "Scotland": "AB DD DG EH FK G HS IV KA KW KY ML PA PH TD ZE",
    "Northern Ireland": "BT",
}
AREA_REGION = {a: r for r, areas in REGION_AREAS.items() for a in areas.split()}
REGIONS = list(REGION_AREAS)
NATION_OF = {r: ("Wales" if r == "Wales" else "Scotland" if r == "Scotland"
                 else "Northern Ireland" if r == "Northern Ireland" else "England") for r in REGIONS}
WELSH_CH_DISTRICTS = {"CH5", "CH6", "CH7", "CH8"}
COUNTRY_REGION = {"WALES": "Wales", "W": "Wales", "SCOTLAND": "Scotland", "S": "Scotland",
                  "NORTHERN IRELAND": "Northern Ireland", "N": "Northern Ireland", "NI": "Northern Ireland"}

HARD_MIN, HARD_MAX = 100.0, 250.0   # p/litre outside this = rejected
REL_LOW, REL_HIGH = 0.12, 0.30      # >12% below / >30% above the regional median = rejected

BRAND_NAMES = {
    "ESSO": "Esso", "BP": "BP", "SHELL": "Shell", "TESCO": "Tesco", "MORRISONS": "Morrisons",
    "SAINSBURYS": "Sainsbury's", "SAINSBURY'S": "Sainsbury's", "TEXACO": "Texaco", "JET": "Jet",
    "GULF": "Gulf", "VALERO": "Valero", "EG ON THE MOVE": "EG On The Move", "MURCO": "Murco",
    "ASDA": "Asda", "ASDA EXPRESS": "Asda Express", "MAXOL": "Maxol", "WELCOME BREAK": "Welcome Break",
    "PACE": "Pace", "GO": "Go", "SOLO": "Solo", "CIRCLE K": "Circle K", "SPAR": "Spar",
    "THE CO-OPERATIVE": "Co-op", "CO-OP": "Co-op", "COOP": "Co-op", "COSTCO WHOLESALE": "Costco",
    "COSTCO": "Costco", "ESSAR": "Essar", "TOTAL": "Total", "TOTALENERGIES": "TotalEnergies",
    "MOTO": "Moto", "APPLEGREEN": "Applegreen", "HARVEST ENERGY": "Harvest Energy",
    "CERTAS ENERGY": "Certas Energy", "RONTEC": "Rontec", "SGN": "SGN", "MFG": "MFG",
}


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def http_get(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/json,text/csv,text/plain,*/*;q=0.8",
        "Accept-Language": "en-GB,en;q=0.9",
        "Referer": "https://www.gov.uk/guidance/access-fuel-price-data",
        "Cache-Control": "no-cache",
    })
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read()


def why(e):
    if isinstance(e, urllib.error.HTTPError):
        return "HTTP %s (blocked or removed)" % e.code
    if isinstance(e, (socket.timeout, TimeoutError)):
        return "timed out"
    if isinstance(e, urllib.error.URLError):
        return "could not connect (%s)" % getattr(e, "reason", e)
    if isinstance(e, json.JSONDecodeError):
        return "not valid JSON (feed page, not data)"
    return str(e)[:120]


# ---------- helpers ----------
def norm_pc(pc):
    pc = re.sub(r"[^A-Z0-9]", "", (pc or "").upper())
    if len(pc) >= 5:
        pc = pc[:-3] + " " + pc[-3:]
    return pc


def pc_area(pc):
    m = re.match(r"^([A-Z]{1,2})\d", pc or "")
    return m.group(1) if m else ""


def pc_district(pc):
    return (pc or "").split(" ")[0]


def haversine_m(a, b, c, d):
    r = 6371000.0
    p1, p2 = math.radians(a), math.radians(c)
    dp, dl = math.radians(c - a), math.radians(d - b)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def fnum(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def to_pence(v):
    """Return (pence, note). Converts pounds and tenths-of-a-penny."""
    x = fnum(v)
    if x is None or x <= 0:
        return None, None
    if 0.5 <= x < 5:          # given in pounds, e.g. 1.459
        return round(x * 100, 1), "converted from £"
    if 1000 <= x < 5000:      # tenths of a penny, e.g. 1459
        return round(x / 10, 1), "converted from tenths of a penny"
    return round(x, 1), None


def tidy(s):
    s = re.sub(r"\s+", " ", (s or "").strip())
    if not s:
        return ""
    if s.isupper() or s.islower():
        s = s.title()
        s = re.sub(r"\b(Bp|Pfs|Uk|Mfg|Sgn|Eg|Ii|Iii|Kfc|Mot)\b", lambda m: m.group(0).upper(), s)
        s = re.sub(r"'S\b", "'s", s)
    return s


KNOWN_PREFIXES = [  # first word(s) of a brand field -> brand
    ("ASDA EXPRESS", "Asda"), ("ASDA", "Asda"), ("BP", "BP"), ("ESSO", "Esso"), ("SHELL", "Shell"),
    ("TEXACO", "Texaco"), ("ESSAR", "Essar"), ("MURCO", "Murco"), ("GULF", "Gulf"), ("JET", "Jet"),
    ("VALERO", "Valero"), ("TESCO", "Tesco"), ("MORRISONS", "Morrisons"), ("SAINSBURY", "Sainsbury's"),
    ("COSTCO", "Costco"), ("TOTAL", "Total"), ("NISA", "Nisa"), ("GO ", "Go"), ("PACE", "Pace"),
    ("APPLEGREEN", "Applegreen"), ("EG ", "EG On The Move"), ("MAXOL", "Maxol"), ("CIRCLE K", "Circle K"),
    ("SPAR", "Spar"), ("CO-OP", "Co-op"), ("THE CO-OPERATIVE", "Co-op"), ("WELCOME BREAK", "Welcome Break"),
    ("MOTO", "Moto"), ("HARVEST", "Harvest Energy"), ("CERTAS", "Certas Energy"), ("SCOTTISH FUELS", "Scottish Fuels"),
    ("UK FUELS", "UK Fuels"),
]
NO_BRAND = {"", "NONE", "NO BRAND", "UNBRANDED", "N/A", "NA", "INDEPENDENT", "OTHER", "-"}
UK_BRAND_COUNTS = {}


def brand_name(b):
    key = re.sub(r"\s+", " ", (b or "").strip()).upper()
    if key in NO_BRAND:
        return "Independent"
    if key in BRAND_NAMES:
        return BRAND_NAMES[key]
    for pre, name in KNOWN_PREFIXES:
        if key == pre.strip() or key.startswith(pre if pre.endswith(" ") else pre + " ") \
                or key.startswith(pre + "-") or key.startswith(pre + "/"):
            return name
    if UK_BRAND_COUNTS.get(key, 0) >= 5 and not re.search(r"GARAGE|SERVICE|FILLING|STATION|MOTORS|\bLTD\b|LIMITED", key):
        return tidy(key)  # a real chain name used across the UK
    return "Independent"


def parse_ff_ts(s):
    # e.g. "Fri Oct 02 2026 15:38:14 GMT+0000 (Coordinated Universal Time)"
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%a %b %d %Y %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s[:24] if fmt.startswith("%a") else s[:19], fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def parse_legacy_ts(s):
    s = (s or "").strip()
    for fmt in ("%d/%m/%Y %H:%M:%S", "%d-%m-%Y %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M"):
        try:
            return datetime.strptime(s[:19], fmt).replace(tzinfo=UK).astimezone(timezone.utc)
        except ValueError:
            pass
    return None


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def uk_label(dt):
    if not dt:
        return "unknown"
    d = dt.astimezone(UK)
    return d.strftime("%a %d %b %Y, %H:%M ") + (d.tzname() or "UK")


# ---------- sources ----------
def load_fuel_finder(sources):
    for label, url in FUEL_FINDER:
        t0 = time.time()
        try:
            raw = http_get(url)
            text = raw.decode("utf-8-sig", errors="replace")
            rows = list(csv.DictReader(io.StringIO(text)))
            if len(rows) < 1000 or "forecourts.node_id" not in rows[0]:
                raise ValueError("unexpected file contents (%d rows)" % len(rows))
            latest = None
            for r in rows:
                for f in FF_FUEL_COLS.values():
                    t = parse_ff_ts(r.get("forecourts.price_submission_timestamp." + f))
                    if t and (latest is None or t > latest):
                        latest = t
            sources.append({"name": label, "url": url, "ok": True, "rows": len(rows),
                            "last_updated": iso(latest), "last_updated_uk": uk_label(latest),
                            "note": "newest price in file", "secs": round(time.time() - t0, 1)})
            log("OK   %s: %d forecourts, newest price %s" % (label, len(rows), uk_label(latest)))
            return rows, label
        except Exception as e:
            sources.append({"name": label, "url": url, "ok": False, "error": why(e)})
            log("FAIL %s: %s" % (label, why(e)))
    return [], None


def load_legacy(sources, now):
    feeds = []
    for retailer, url in LEGACY_FEEDS:
        name = "%s (old CMA retailer feed)" % retailer
        try:
            d = json.loads(http_get(url))
            st = d.get("stations") or []
            lu = parse_legacy_ts(d.get("last_updated"))
            age_h = (now - lu).total_seconds() / 3600 if lu else None
            fresh = age_h is not None and age_h <= LEGACY_MAX_AGE_H
            src = {"name": name, "url": url, "ok": True, "rows": len(st), "last_updated": iso(lu),
                   "last_updated_uk": uk_label(lu), "used": fresh,
                   "note": "used" if fresh else "stale - not used (last updated %s)" % uk_label(lu)}
            sources.append(src)
            log("%s %s: %d stations, updated %s" % ("OK  " if fresh else "OLD ", name, len(st), uk_label(lu)))
            if fresh:
                feeds.append((retailer, lu, st, src))
        except Exception as e:
            sources.append({"name": name, "url": url, "ok": False, "error": why(e)})
            log("FAIL %s: %s" % (name, why(e)))
    return feeds


# ---------- region ----------
def region_of(pc, lat, lon, country):
    """Return region name, or None if not a UK station."""
    c = (country or "").strip().upper()
    area = pc_area(pc)
    if area in ("GX", "JE", "GY", "IM"):
        return None
    if c in COUNTRY_REGION:
        return COUNTRY_REGION[c]
    if area in AREA_REGION:
        if area == "CH" and pc_district(pc) in WELSH_CH_DISTRICTS:
            return "Wales"
        return AREA_REGION[area]
    if lat is None or lon is None:
        return None
    if lat > 55.8 or (lat > 54.9 and lon < -2.6 and lat > 55.0):
        return "Scotland"
    if lon < -5.3 and 54 < lat < 55.4:
        return "Northern Ireland"
    return None  # can't place it reliably


class Grid:
    """Simple spatial index for 'is there a station within N metres'."""
    def __init__(self):
        self.cells = {}

    def key(self, lat, lon):
        return (int(math.floor(lat * 100)), int(math.floor(lon * 60)))

    def add(self, s):
        if s["lat"] is not None:
            self.cells.setdefault(self.key(s["lat"], s["lon"]), []).append(s)

    def near(self, lat, lon, metres):
        a, b = self.key(lat, lon)
        best = None
        for i in (-1, 0, 1):
            for j in (-1, 0, 1):
                for s in self.cells.get((a + i, b + j), []):
                    d = haversine_m(lat, lon, s["lat"], s["lon"])
                    if d < metres and (best is None or d < best[0]):
                        best = (d, s)
        return best[1] if best else None


def main():
    now = datetime.now(timezone.utc)
    sources = []
    ff_rows, ff_label = load_fuel_finder(sources)
    legacy = load_legacy(sources, now)

    for r in ff_rows:
        k = re.sub(r"\s+", " ", (r.get("forecourts.brand_name") or "").strip()).upper()
        UK_BRAND_COUNTS[k] = UK_BRAND_COUNTS.get(k, 0) + 1
    stations = []
    closed = unplaced = 0
    for r in ff_rows:
        g = lambda k: (r.get("forecourts." + k) or "").strip()
        pc = norm_pc(g("location.postcode"))
        lat, lon = fnum(g("location.latitude")), fnum(g("location.longitude"))
        if lat is not None and not (49.8 < lat < 61 and -8.7 < lon < 2):
            lat = lon = None
        region = region_of(pc, lat, lon, g("location.country"))
        if region is None:
            unplaced += 1
            continue
        if g("temporary_closure").lower() == "true" or g("permanent_closure").lower() == "true":
            closed += 1
            continue
        prices = {}
        for fuel, col in FF_FUEL_COLS.items():
            p, note = to_pence(g("fuel_price." + col))
            if p is None:
                continue
            t = parse_ff_ts(g("price_submission_timestamp." + col))
            prices[fuel] = {"p": p, "t": iso(t), "src": "Fuel Finder", "note": note}
        addr = ", ".join(x for x in [tidy(g("location.address_line_1")), tidy(g("location.address_line_2"))] if x)
        stations.append({
            "brand": brand_name(g("brand_name")), "name": tidy(g("trading_name")),
            "addr": addr, "pc": pc, "town": tidy(g("location.city")) or tidy(g("location.county")),
            "lat": lat, "lon": lon, "region": region, "prices": prices, "src": ["Fuel Finder"],
            "motorway": g("is_motorway_service_station").lower() == "true",
        })

    # ---- merge retailer feeds, de-duplicating by postcode / distance ----
    by_pc, grid = {}, Grid()
    for s in stations:
        by_pc.setdefault(s["pc"], []).append(s)
        grid.add(s)
    merged = added = updated = agree = compared = 0
    for retailer, lu, st_list, src in legacy:
        uk_count = 0
        for x in st_list:
            loc = x.get("location") or {}
            lat, lon = fnum(loc.get("latitude")), fnum(loc.get("longitude"))
            if lat is not None and not (49.8 < lat < 61 and -8.7 < lon < 2):
                lat = lon = None
            pc = norm_pc(x.get("postcode"))
            region = region_of(pc, lat, lon, "")
            if region is None:
                continue
            uk_count += 1
            match = None
            for s in by_pc.get(pc, []):
                if lat is None or s["lat"] is None or haversine_m(lat, lon, s["lat"], s["lon"]) < 1500:
                    match = s
                    break
            if match is None and lat is not None:
                match = grid.near(lat, lon, 120)
            lprices = {}
            for fuel, v in (x.get("prices") or {}).items():
                fuel = {"B7S": "B7", "B7P": "SDV", "SUPER": "E5"}.get(fuel.upper(), fuel.upper())
                if fuel in FUELS:
                    p, note = to_pence(v)
                    if p is not None:
                        lprices[fuel] = {"p": p, "t": iso(lu), "src": retailer + " feed", "note": note}
            if match:
                merged += 1
                if retailer + " feed" not in match["src"]:
                    match["src"].append(retailer + " feed")
                for fuel, lp in lprices.items():
                    cur = match["prices"].get(fuel)
                    if cur is None:
                        match["prices"][fuel] = lp
                        updated += 1
                        continue
                    if cur["src"] == "Fuel Finder":
                        compared += 1
                        if abs(cur["p"] - lp["p"]) < 0.05:
                            agree += 1
                    if (cur["t"] or "") < lp["t"] and abs(cur["p"] - lp["p"]) >= 0.05:
                        match["prices"][fuel] = dict(lp, alt="%s said %.1fp" % (cur["src"], cur["p"]))
                        updated += 1
            elif lprices:
                added += 1
                addr = tidy(x.get("address"))
                town = addr.split(",")[-1].strip() if "," in addr else ""
                s = {"brand": brand_name(x.get("brand")), "name": "%s %s" % (brand_name(x.get("brand")), town or pc),
                     "addr": addr, "pc": pc, "town": town, "lat": lat, "lon": lon, "region": region,
                     "prices": lprices, "src": [retailer + " feed"], "motorway": False}
                stations.append(s)
                by_pc.setdefault(pc, []).append(s)
                grid.add(s)
        src["uk_rows"] = uk_count

    # ---- sanity-check prices against the regional median for each fuel ----
    rejected = []
    medians = {}
    for reg in REGIONS:
        for fuel in FUELS:
            vals = [s["prices"][fuel]["p"] for s in stations if s["region"] == reg and fuel in s["prices"]
                    and HARD_MIN <= s["prices"][fuel]["p"] <= HARD_MAX]
            medians[(reg, fuel)] = statistics.median(vals) if len(vals) >= 5 else None
    for fuel in FUELS:
        vals = [s["prices"][fuel]["p"] for s in stations if fuel in s["prices"]
                and HARD_MIN <= s["prices"][fuel]["p"] <= HARD_MAX]
        medians[("UK", fuel)] = statistics.median(vals) if vals else None

    def reject(s, fuel, why_):
        rejected.append({"station": s["name"], "brand": s["brand"], "pc": s["pc"], "fuel": fuel,
                         "value": s["prices"][fuel]["p"], "src": s["prices"][fuel]["src"], "why": why_})
        del s["prices"][fuel]

    for s in stations:
        for fuel in list(s["prices"]):
            p = s["prices"][fuel]["p"]
            med = medians.get((s["region"], fuel)) or medians[("UK", fuel)]
            if not (HARD_MIN <= p <= HARD_MAX):
                reject(s, fuel, "outside %d-%dp" % (HARD_MIN, HARD_MAX))
            elif med and p < med * (1 - REL_LOW):
                reject(s, fuel, "more than %d%% below the %s median (%.1fp)" % (REL_LOW * 100, s["region"], med))
            elif med and p > med * (1 + REL_HIGH):
                reject(s, fuel, "more than %d%% above the %s median (%.1fp)" % (REL_HIGH * 100, s["region"], med))
        pr = s["prices"]
        for prem, std in (("E5", "E10"), ("SDV", "B7")):
            if prem in pr and std in pr and pr[prem]["p"] < pr[std]["p"] - 0.05:
                reject(s, prem, "premium cheaper than standard (%.1fp)" % pr[std]["p"])
    stations = [s for s in stations if s["prices"]]
    stations.sort(key=lambda s: (REGIONS.index(s["region"]), s["pc"]))

    # ---- compact output ----
    brands = sorted({s["brand"] for s in stations})
    src_names = sorted({v["src"] for s in stations for v in s["prices"].values()})
    bi = {b: i for i, b in enumerate(brands)}
    si = {n: i for i, n in enumerate(src_names)}
    ri = {r: i for i, r in enumerate(REGIONS)}
    rows = []
    for s in stations:
        row = [s["name"], s["addr"], s["town"], s["pc"],
               round(s["lat"], 5) if s["lat"] is not None else None,
               round(s["lon"], 5) if s["lon"] is not None else None,
               bi[s["brand"]], ri[s["region"]], 1 if s["motorway"] else 0]
        for fuel in FUELS:
            v = s["prices"].get(fuel)
            if v:
                t = int(datetime.strptime(v["t"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp() // 60) if v["t"] else 0
                row.append([int(round(v["p"] * 10)), t, si[v["src"]]])
            else:
                row.append(0)
        rows.append(row)
    newest = max((s["prices"][f]["t"] for s in stations for f in s["prices"] if s["prices"][f]["t"]), default=None)
    week_ago = iso(now - timedelta(days=7))
    nations = {}
    for s in stations:
        n = NATION_OF[s["region"]]
        nations[n] = nations.get(n, 0) + 1
    data = {
        "v": 1, "generated": iso(now), "generated_uk": uk_label(now),
        "newest_price_uk": uk_label(datetime.strptime(newest, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)) if newest else "unknown",
        "fuels": FUELS, "brands": brands, "regions": REGIONS, "src": src_names,
        "cols": ["name", "addr", "town", "pc", "lat", "lon", "brand", "region", "motorway"] + FUELS,
        "price_format": "[pence*10, unix_minutes, src_index] or 0",
        "sources": sources, "nations": nations,
        "counts": {"stations": len(stations), "brands": len(brands), "closed_excluded": closed,
                   "not_placed": unplaced, "retailer_matches": merged, "retailer_added": added,
                   "retailer_price_updates": updated, "cross_checked": compared, "cross_check_agree": agree,
                   "rejected_prices": len(rejected),
                   "stations_updated_last_7_days": sum(1 for s in stations if any((v["t"] or "") >= week_ago for v in s["prices"].values()))},
        "rejected": rejected[:300], "st": rows,
    }
    os.makedirs(os.path.dirname(OUT_JSON), exist_ok=True)
    tmp = OUT_JSON + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"), ensure_ascii=False)
    os.replace(tmp, OUT_JSON)
    log("Wrote %s: %d UK stations %s, %d brands, %d prices rejected, %.0f KB"
        % (OUT_JSON, len(stations), nations, len(brands), len(rejected), os.path.getsize(OUT_JSON) / 1024))
    if not stations:
        log("WARNING: no stations - every source failed")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
