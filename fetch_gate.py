#!/usr/bin/env python3
"""
Macro indicator data pipeline.

Fetches all 10 indicator series from primary public sources and writes
data/gate_state.json in the format the dashboard auto-loads:

  {
    "version": "pipeline-1.0",
    "generated_at": "2026-10-29T14:30:00Z",
    "readings": { "<id>": [ {"date": "YYYY-MM-DD", "value": float}, ... ] },
    "meta":     { "<id>": {"asof": "YYYY-MM-DD", "stale_days": int, "cadence": "daily|weekly", "source": str} },
    "failed_series": [ "<id>", ... ]
  }

Design rules
- Never fail the run because one source is down: a failed series keeps the
  readings from the previous gate_state.json and is listed in failed_series,
  and its staleness chip goes red on the dashboard.
- Every series has a primary source and, where one exists, an independent
  fallback. FRED is the primary for four series; if FRED_API_KEY is set the
  FRED API is used (immune to the edge bot-mitigation that drops plain
  fredgraph.csv requests from some networks), else fredgraph.csv is tried.
- Thresholds live in the dashboard, not here. This script only moves data.

Sources
  hy_oas   FRED BAMLH0A0HYM2                      (no independent fallback — ICE BofA licenses only via FRED)
  move     Yahoo ^MOVE
  nfci     FRED NFCI            -> Chicago Fed CSV (often lags FRED by weeks; used only if FRED fails)
  dgs30    FRED DGS30           -> Fed H.15 data download (RIFLGFCY30_N.B)
  claims   FRED IC4WSA                            (no clean machine-readable DOL fallback)
  aaii     aaii.com/sentimentsurvey/sent_results (HTML table; bull - bear)
  vix_term Yahoo ^VIX9D / ^VIX
  vvix     Yahoo ^VVIX
  usdjpy   Yahoo JPY=X
  acm_tp   NY Fed ACMTermPremium.csv (an .xls; sheet "ACM Daily", column ACMTP10)
"""
import csv, datetime as dt, io, json, os, re, sys, time, urllib.request, urllib.parse

OUT = os.environ.get("GATE_OUT", "data/gate_state.json")
FRED_KEY = os.environ.get("FRED_API_KEY", "").strip()
START = (dt.date.today() - dt.timedelta(days=480)).isoformat()   # ~16 months: covers the 52-week claims window
UA = "Mozilla/5.0"   # short UA: Yahoo's limiter 429s the long Chrome string from CI networks

def get(url, timeout=25, binary=False):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    return data if binary else data.decode("utf-8", "replace")

def clip(rows):
    rows = [r for r in rows if r["date"] >= START]
    rows.sort(key=lambda r: r["date"])
    # de-duplicate on date (keep last)
    out = {}
    for r in rows: out[r["date"]] = r
    return list(out.values())

# ---------- FRED ----------
def fred(series):
    if FRED_KEY:
        url = ("https://api.stlouisfed.org/fred/series/observations?series_id=%s&api_key=%s"
               "&file_type=json&observation_start=%s" % (series, FRED_KEY, START))
        js = json.loads(get(url))
        rows = [{"date": o["date"], "value": float(o["value"])} for o in js["observations"] if o["value"] not in (".", "")]
        return clip(rows), "FRED API"
    txt = get("https://fred.stlouisfed.org/graph/fredgraph.csv?id=%s" % series)
    rows = []
    for line in txt.splitlines()[1:]:
        p = line.strip().split(",")
        if len(p) == 2 and p[1] not in (".", ""):
            rows.append({"date": p[0], "value": float(p[1])})
    if not rows: raise RuntimeError("fredgraph.csv returned no rows")
    return clip(rows), "FRED fredgraph.csv"

# ---------- Yahoo ----------
def yahoo(ticker):
    last = None
    for host in ("query1", "query2"):
        url = "https://%s.finance.yahoo.com/v8/finance/chart/%s?range=2y&interval=1d" % (host, urllib.parse.quote(ticker))
        try:
            js = json.loads(get(url))["chart"]["result"][0]
            ts = js["timestamp"]; cl = js["indicators"]["quote"][0]["close"]
            rows = []
            for t, v in zip(ts, cl):
                if v is None: continue
                d = dt.datetime.fromtimestamp(t, dt.timezone.utc).date().isoformat()
                rows.append({"date": d, "value": round(float(v), 4)})
            if rows: return clip(rows)
        except Exception as e:
            last = e; time.sleep(3)
    raise RuntimeError("Yahoo %s: %s" % (ticker, last))

# ---------- CBOE (primary for the three vol indices; direct from the exchange) ----------
def cboe(index):
    txt = get("https://cdn.cboe.com/api/global/us_indices/daily_prices/%s_History.csv" % index)
    rows = []
    rdr = csv.DictReader(io.StringIO(txt))
    cols = [c.strip() for c in (rdr.fieldnames or [])]
    dcol = next((c for c in cols if c.upper() == "DATE"), None)
    vcol = next((c for c in cols if c.upper() == "CLOSE"), None) or next((c for c in cols if c.upper() == index.upper()), None) \
           or (cols[-1] if len(cols) >= 2 else None)   # VVIX_History.csv is DATE,VVIX — no CLOSE column
    if not dcol or not vcol: raise RuntimeError("CBOE %s: unexpected columns %s" % (index, cols))
    for r in rdr:
        d = (r.get(dcol) or "").strip(); c = (r.get(vcol) or "").strip()
        if not d or not c: continue
        try:
            if "/" in d: m, dd, y = d.split("/"); d = "%s-%02d-%02d" % (y, int(m), int(dd))
            rows.append({"date": d, "value": round(float(c), 4)})
        except ValueError: continue
    return clip(rows)

# ---------- Fallbacks ----------
def h15_dgs30():
    url = ("https://www.federalreserve.gov/datadownload/Output.aspx?rel=H15&series=bf17364827e38702b42a58cf8eaa3f78"
           "&lastobs=400&from=&to=&filetype=csv&label=include&layout=seriescolumn")
    txt = get(url)
    rdr = list(csv.reader(io.StringIO(txt)))
    hdr_i = next(i for i, r in enumerate(rdr) if r and r[0] == "Time Period")
    col = rdr[hdr_i].index("RIFLGFCY30_N.B")
    rows = []
    for r in rdr[hdr_i + 1:]:
        if len(r) > col and re.match(r"\d{4}-\d\d-\d\d", r[0]) and r[col] not in ("ND", "", "NC"):
            rows.append({"date": r[0], "value": float(r[col])})
    return clip(rows)

def chicagofed_nfci():
    txt = get("https://www.chicagofed.org/~/media/publications/nfci/nfci-data-series-csv.csv?la=en")
    rows = []
    for r in csv.DictReader(io.StringIO(txt)):
        m, d, y = r["Friday_of_Week"].split("/")
        rows.append({"date": "%s-%s-%s" % (y, m, d), "value": float(r["NFCI"])})
    return clip(rows)

def aaii_parse(html):
    txt = re.sub(r"<[^>]+>", " ", html)
    txt = re.sub(r"\s+", " ", txt)
    # rows look like: "Oct 21 36.9% 17.3% 45.8%"
    pat = re.compile(r"\b([A-Z][a-z]{2}) (\d{1,2}) (\d{1,2}\.\d)% (\d{1,2}\.\d)% (\d{1,2}\.\d)%")
    mon = {m: i for i, m in enumerate(["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"], 1)}
    today = dt.date.today(); rows = []
    year = today.year
    prev_m = None
    for m in pat.finditer(txt):
        mm = mon[m.group(1)]
        if prev_m is not None and mm > prev_m: year -= 1      # table is newest-first; month wrap => previous year
        prev_m = mm
        d = dt.date(year, mm, int(m.group(2)))
        if d > today: d = dt.date(year - 1, mm, int(m.group(2)))
        rows.append({"date": d.isoformat(), "value": round(float(m.group(3)) - float(m.group(5)), 1)})
    if not rows: raise RuntimeError("AAII table not parsed")
    return clip(rows)

def aaii():
    return aaii_parse(get("https://www.aaii.com/sentimentsurvey/sent_results"))

def aaii_browser():
    """aaii.com blocks plain requests from datacenter IPs; a real headless Chromium usually passes.
    Requires `pip install playwright && playwright install --with-deps chromium` (done in the workflow)."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        pg = b.new_page(user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36")
        pg.goto("https://www.aaii.com/sentimentsurvey/sent_results", wait_until="domcontentloaded", timeout=60000)
        pg.wait_for_timeout(4000)
        html = pg.content(); b.close()
    return aaii_parse(html)

def acm():
    import xlrd  # pip install xlrd
    raw = get("https://www.newyorkfed.org/medialibrary/media/research/data_indicators/ACMTermPremium.csv", timeout=90, binary=True)
    wb = xlrd.open_workbook(file_contents=raw)
    s = wb.sheet_by_name("ACM Daily"); hdr = s.row_values(0); ci = hdr.index("ACMTP10")
    rows = []
    for r in range(1, s.nrows):
        v = s.row_values(r)
        if v[ci] in ("", None): continue
        d = dt.datetime.strptime(v[0], "%d-%b-%Y").date().isoformat()
        rows.append({"date": d, "value": round(float(v[ci]), 4)})
    return clip(rows)

# ---------- Orchestration ----------
SERIES = [
    # id, cadence, [(label, fn), ...] in priority order
    ("hy_oas",   "daily",  [("FRED BAMLH0A0HYM2", lambda: fred("BAMLH0A0HYM2"))]),
    ("move",     "daily",  [("Yahoo ^MOVE", lambda: (yahoo("^MOVE"), "Yahoo ^MOVE"))]),
    ("nfci",     "weekly", [("FRED NFCI", lambda: fred("NFCI")), ("Chicago Fed CSV", lambda: (chicagofed_nfci(), "Chicago Fed CSV"))]),
    ("dgs30",    "daily",  [("FRED DGS30", lambda: fred("DGS30")), ("Fed H.15", lambda: (h15_dgs30(), "Fed H.15"))]),
    ("claims",   "weekly", [("FRED IC4WSA", lambda: fred("IC4WSA"))]),
    ("aaii",     "weekly", [("AAII", lambda: (aaii(), "AAII sent_results")), ("AAII (headless browser)", lambda: (aaii_browser(), "AAII sent_results (browser)"))]),
    ("vvix",     "daily",  [("CBOE VVIX", lambda: (cboe("VVIX"), "CBOE VVIX_History")), ("Yahoo ^VVIX", lambda: (yahoo("^VVIX"), "Yahoo ^VVIX"))]),
    ("usdjpy",   "daily",  [("Yahoo JPY=X", lambda: (yahoo("JPY=X"), "Yahoo JPY=X"))]),
    ("acm_tp",   "daily",  [("NY Fed ACM", lambda: (acm(), "NY Fed ACMTermPremium"))]),
]

def _ratio(v, v9):
    v = {r["date"]: r["value"] for r in v}; v9 = {r["date"]: r["value"] for r in v9}
    return [{"date": d, "value": round(v9[d] / v[d], 4)} for d in sorted(set(v) & set(v9)) if v[d]]

def vix_term_cboe():  return _ratio(cboe("VIX"), cboe("VIX9D")), "CBOE VIX9D/VIX"
def vix_term_yahoo(): return _ratio(yahoo("^VIX"), yahoo("^VIX9D")), "Yahoo ^VIX9D/^VIX"

SERIES.insert(6, ("vix_term", "daily", [("CBOE VIX9D/VIX", vix_term_cboe), ("Yahoo VIX9D/VIX", vix_term_yahoo)]))

def main():
    prev = {}
    if os.path.exists(OUT):
        try: prev = json.load(open(OUT))
        except Exception: prev = {}
    readings = dict(prev.get("readings", {}))
    meta = dict(prev.get("meta", {}))
    failed = []
    today = dt.date.today()
    for sid, cadence, sources in SERIES:
        ok = False
        held_asof = (meta.get(sid) or {}).get("asof", "")
        for label, fn in sources:
            try:
                rows, src = fn()
                if not rows: raise RuntimeError("empty")
                asof = rows[-1]["date"]
                # A source may only replace held data if it is at least as fresh. Prevents a lagging
                # fallback (e.g. Chicago Fed's CSV, months behind FRED) from overwriting good data.
                if asof < held_asof:
                    print("SKIP %-9s %-28s asof %s is older than held %s" % (sid, label, asof, held_asof)); continue
                readings[sid] = rows
                meta[sid] = {"asof": asof, "stale_days": (today - dt.date.fromisoformat(asof)).days,
                             "cadence": cadence, "source": src, "n": len(rows)}
                print("OK   %-9s %-28s %s (%d rows)" % (sid, src, asof, len(rows)))
                ok = True; break
            except Exception as e:
                print("FAIL %-9s %-28s %s" % (sid, label, str(e)[:120]))
        if not ok:
            failed.append(sid)
            if sid in meta:
                meta[sid]["stale_days"] = (today - dt.date.fromisoformat(meta[sid]["asof"])).days
                if "carried forward" not in meta[sid].get("source", ""):
                    meta[sid]["source"] = meta[sid].get("source", "") + " (carried forward — fetch failed)"
    state = {"version": "pipeline-1.0", "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
             "readings": readings, "meta": meta, "failed_series": failed}
    os.makedirs(os.path.dirname(OUT) or ".", exist_ok=True)
    json.dump(state, open(OUT, "w"), separators=(",", ":"))
    print("wrote %s — %d series ok, %d failed: %s" % (OUT, len(SERIES) - len(failed), len(failed), ", ".join(failed) or "none"))
    inject_html(state)
    # Exit 0 even with failures: the dashboard surfaces staleness. Exit 1 only if NOTHING could be fetched.
    sys.exit(1 if len(failed) == len(SERIES) else 0)

def inject_html(state):
    """Self-contained mode (no web server needed): rewrite the dashboard's baked data block with the fresh
    readings so a file:// open shows live data. Set GATE_HTML_TEMPLATE and GATE_HTML_OUT."""
    tpl = os.environ.get("GATE_HTML_TEMPLATE", "").strip(); out = os.environ.get("GATE_HTML_OUT", "").strip()
    if not tpl or not out: return
    html = open(tpl, encoding="utf-8").read()
    m = re.search(r"const BAKED_READINGS_V15 = (\{.*?\});", html, re.S)
    if not m: print("inject: BAKED_READINGS_V15 block not found in template"); return
    compact = {k: [[r["date"], r["value"]] for r in v] for k, v in state["readings"].items()}
    html = html[:m.start(1)] + json.dumps(compact, separators=(",", ":")) + html[m.end(1):]
    stamp = "pipeline %s @ %s%s" % (state["version"], state["generated_at"],
                                    (" · FAILED: " + ", ".join(state["failed_series"])) if state["failed_series"] else "")
    html = html.replace("state.bakedVersion = 'v1.5 / 2026-10-28';", "state.bakedVersion = %s;" % json.dumps(stamp))
    # force the fresh bake to replace whatever localStorage holds, once per generated_at
    html = html.replace("loadBaked(false);\nrender();",
        "try { if (localStorage.getItem('gate_injected_gen') !== %s) { state.readings = {}; localStorage.setItem('gate_injected_gen', %s); } } catch (e) {}\nloadBaked(false);\nrender();"
        % (json.dumps(state["generated_at"]), json.dumps(state["generated_at"])))
    open(out, "w", encoding="utf-8").write(html)
    print("wrote %s (self-contained dashboard, %s)" % (out, stamp))

if __name__ == "__main__":
    main()
