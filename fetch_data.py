#!/usr/bin/env python3
"""
fetch_data.py
Auto-refresh data.json for the inflation dashboard by scraping the official
release pages for each market.

Design rule: NEVER overwrite a good number with a bad one.
Every scraped value is validated. If a source fails, returns nonsense, or
can't be parsed, the script keeps last month's value and flags that source
as "stale" so the dashboard can show it. A broken parser degrades visibly,
it does not poison the report.

Run:  python fetch_data.py
Deps: requests, beautifulsoup4, pdfplumber  (installed by the workflow)
"""

import json, re, sys, datetime, traceback
import requests
from bs4 import BeautifulSoup

DATA_FILE = "data.json"
HEADERS = {"User-Agent": "inflation-monitor-bot/1.0 (+github actions)"}
TIMEOUT = 30

# --- helpers ---------------------------------------------------------------

def get(url):
    """Fetch a URL's text, or return None on any failure."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        return r.text
    except Exception as e:
        print(f"  ! fetch failed: {url}  ({e})")
        return None

def valid_pct(x):
    """A plausible monthly/annual inflation percentage."""
    try:
        return -50.0 <= float(x) <= 100.0
    except (TypeError, ValueError):
        return False

def signed(x):
    """Format 0.5 -> '+0.5', -0.1 -> '-0.1' to match the dashboard style."""
    f = float(x)
    return ("+" if f >= 0 else "") + f"{f:.1f}"

def find_pct(text, pattern):
    """Run a regex with one capture group over text, return the number or None."""
    if not text:
        return None
    m = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
    if not m:
        return None
    val = m.group(1).replace(",", ".")
    return val if valid_pct(val) else None

# --- per-source parsers ----------------------------------------------------
# Each returns a dict of {field_name: number_string} for the fields it can
# supply. Return {} on total failure. THESE REGEXES ARE WHAT YOU TUNE IN
# STEP 4: run the script, see which come back None, and adjust the pattern to
# match the wording the release actually uses this month.

def parse_uk_cpi():
    html = get("https://www.ons.gov.uk/economy/inflationandpriceindices/bulletins/consumerpriceinflation/latest")
    text = BeautifulSoup(html, "html.parser").get_text(" ") if html else ""
    out = {}
    yoy = find_pct(text, r"CPI[^.]{0,40}rose by ([\d.]+)%? in the 12 months")
    if yoy: out["headlineCpiYoY"] = signed(yoy)
    core = find_pct(text, r"excluding energy, food, alcohol and tobacco[^.]{0,60}?([\d.]+)%")
    if core: out["coreCpiYoY"] = signed(core)
    return out

def parse_uk_ppi():
    html = get("https://www.ons.gov.uk/economy/inflationandpriceindices/bulletins/producerpriceinflation/latest")
    text = BeautifulSoup(html, "html.parser").get_text(" ") if html else ""
    out = {}
    yoy = find_pct(text, r"output \(factory gate\) prices rose by ([\d.]+)%? in the year")
    if yoy: out["ppiYoY"] = signed(yoy)
    mom = find_pct(text, r"output prices rose by ([\d.]+)%? in [A-Z][a-z]+ \d{4}")
    if mom: out["ppiMoM"] = signed(mom)
    return out

def parse_us_cpi():
    html = get("https://www.bls.gov/news.release/cpi.nr0.htm")
    text = BeautifulSoup(html, "html.parser").get_text(" ") if html else ""
    out = {}
    mom = find_pct(text, r"increased ([\d.]+) percent[^.]{0,30}seasonally adjusted")
    if mom: out["headlineCpiMoM"] = signed(mom)
    yoy = find_pct(text, r"all items index increased ([\d.]+) percent[^.]{0,30}12 months")
    if yoy: out["headlineCpiYoY"] = signed(yoy)
    coremom = find_pct(text, r"less food and energy[^.]{0,60}?rose ([\d.]+) percent")
    if coremom: out["coreCpiMoM"] = signed(coremom)
    coreyoy = find_pct(text, r"less food and energy index[^.]{0,60}?([\d.]+) percent[^.]{0,20}12 months")
    if coreyoy: out["coreCpiYoY"] = signed(coreyoy)
    return out

def parse_us_ppi():
    html = get("https://www.bls.gov/news.release/ppi.nr0.htm")
    text = BeautifulSoup(html, "html.parser").get_text(" ") if html else ""
    out = {}
    mom = find_pct(text, r"final demand[^.]{0,30}rose ([\d.]+) percent in")
    if mom: out["ppiMoM"] = signed(mom)
    yoy = find_pct(text, r"final demand[^.]{0,40}?([\d.]+) percent for the 12 months")
    if yoy: out["ppiYoY"] = signed(yoy)
    return out

def parse_eurostat_hicp():
    """Eurostat API (JSON-stat) is far more stable than the dated release page.
    prc_hicp_manr = HICP annual rate. coicop CP00 = all items, TOT_X_NRG_FOOD = core."""
    out = {}
    base = "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/prc_hicp_manr"
    try:
        j = requests.get(base, params={"format":"JSON","geo":"EA","coicop":"CP00","lang":"EN"},
                         headers=HEADERS, timeout=TIMEOUT).json()
        vals = j["value"]
        latest = vals[str(max(int(k) for k in vals))]   # most recent period
        if valid_pct(latest): out["headlineCpiYoY"] = signed(latest)
    except Exception as e:
        print(f"  ! eurostat HICP failed ({e})")
    return out

def parse_boj_cgpi():
    """BoJ publishes the CGPI as a dated PDF: cgpiYYMM.pdf for the reference month.
    No API. This is the most fragile source."""
    out = {}
    try:
        import pdfplumber, io
        now = datetime.date.today()
        # reference month is usually the prior month
        ref = (now.replace(day=1) - datetime.timedelta(days=1))
        fname = f"cgpi{ref.strftime('%y%m')}.pdf"
        url = f"https://www.boj.or.jp/en/statistics/pi/cgpi_release/{fname}"
        raw = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        raw.raise_for_status()
        with pdfplumber.open(io.BytesIO(raw.content)) as pdf:
            text = pdf.pages[0].extract_text() or ""
        yoy = find_pct(text, r"([\-\d.]+)\s*%?\s*(?:from the previous year|year-on-year)")
        if yoy: out["ppiYoY"] = signed(yoy)
    except Exception as e:
        print(f"  ! BoJ CGPI failed ({e})")
    return out

# Map each market code -> list of parser functions that feed it.
MARKET_PARSERS = {
    "UK": [parse_uk_cpi, parse_uk_ppi],
    "EA": [parse_eurostat_hicp],
    "US": [parse_us_cpi, parse_us_ppi],
    "JP": [parse_boj_cgpi],
}

# --- main ------------------------------------------------------------------

def main():
    with open(DATA_FILE, encoding="utf-8") as f:
        data = json.load(f)

    today = datetime.date.today().isoformat()
    updated, fell_back = [], []

    for market in data["markets"]:
        code = market.get("code")
        scraped = {}
        for fn in MARKET_PARSERS.get(code, []):
            try:
                scraped.update(fn())
            except Exception:
                print(f"  ! parser crashed for {code}:\n{traceback.format_exc()}")
        for field, old in list(market.items()):
            if field in scraped and scraped[field] != old:
                market[field] = scraped[field]
                updated.append(f"{code}.{field}: {old} -> {scraped[field]}")
        # any expected field this source should provide but didn't = stale
        for field in scraped:
            pass
        for fn in MARKET_PARSERS.get(code, []):
            pass
        missing = [f for f in ("headlineCpiYoY","ppiYoY") if f in market and market[f] != "n/a" and f not in scraped]
        if MARKET_PARSERS.get(code) and missing:
            fell_back.append(f"{code}: kept old {', '.join(missing)}")

    data.setdefault("fetchStatus", {})
    data["fetchStatus"] = {"ranAt": today, "updated": updated, "fellBack": fell_back}
    if updated:
        data["meta"]["lastRefreshed"] = today

    # write only if still valid JSON
    out = json.dumps(data, indent=2, ensure_ascii=False)
    json.loads(out)  # raises if somehow malformed
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        f.write(out + "\n")

    print("\n=== refresh summary ===")
    print(f"updated ({len(updated)}):")
    for u in updated: print("  +", u)
    print(f"fell back / stale ({len(fell_back)}):")
    for s in fell_back: print("  ~", s)
    if not updated:
        print("No fields changed. Either nothing new published, or parsers need tuning (step 4).")

if __name__ == "__main__":
    main()
