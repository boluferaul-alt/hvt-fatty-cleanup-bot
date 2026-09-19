"""Download the four blocked counties' official bulk delinquent-tax files.

Brazoria, Bell, Taylor and Cameron all publish their delinquent roll for free
on the county/CAD site, which is why those counties came back BLOCKED in
tax_watch even though the data was public the whole time. This pulls the files
into state/bulk/ so bulk_rolls.py can serve balances from them.

Politeness matters here: bellcad.org and taylor-cad.org sit behind a WAF that
answered a bare HEAD burst with 429. So: GET (never HEAD), a real Referer from
the page that links the file, one file at a time, and exponential backoff on
429/503. These are small counties' web servers — do not hammer them.

Brazoria is the odd one: its roll lives on a public OneDrive share, and the
anonymous api.onedrive.com/shares endpoint 401s. Resolving the 1drv.ms
redirect and asking for the content directly is what actually works.
"""
from __future__ import annotations

import os
import sys
import time

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
DEST = os.path.join(HERE, "state", "bulk")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# county -> (filename, url, referer)
# CAMERON is a live lookup since 2026-09-19 (cameron_live.py) and BRAZORIA
# since 2026-09-06, so neither roll is fetched any more. TAYLOR's newest file
# is discovered by date each run (taylor_newest), not pinned here.
SOURCES = {
    "BELL": (
        "BellCAD_Delinquent_Roll_Condensed_20260717.xlsx",
        "https://bellcad.org/wp-content/uploads/2019/06/BellCAD_Delinquent_Roll_Condensed_20260717.xlsx",
        "https://bellcad.org/data-portal/",
    ),
}

BRAZORIA_SHARE = ("https://1drv.ms/x/c/c5b1985dca72f232/"
                  "IQALb5drpwCETI730d2prW5DAfgL-YxAvVPQbGfc5c1Mjc4?e=WmY3TE")


def _session(referer: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": referer,
    })
    return s


def fetch(label: str, filename: str, url: str, referer: str,
          tries: int = 4) -> bool:
    out = os.path.join(DEST, filename)
    if os.path.exists(out) and os.path.getsize(out) > 1024:
        print(f"  {label:18} already have {filename} "
              f"({os.path.getsize(out)/1048576:.2f} MB)")
        return True
    s = _session(referer)
    delay = 5
    for attempt in range(1, tries + 1):
        try:
            r = s.get(url, timeout=180, stream=True)
        except requests.RequestException as e:
            print(f"  {label:18} attempt {attempt}: {type(e).__name__}")
            time.sleep(delay); delay *= 2
            continue
        if r.status_code in (429, 503):
            wait = int(r.headers.get("Retry-After") or delay)
            print(f"  {label:18} attempt {attempt}: {r.status_code} rate-limited, "
                  f"waiting {wait}s")
            r.close()
            time.sleep(wait); delay *= 2
            continue
        if r.status_code != 200:
            print(f"  {label:18} HTTP {r.status_code} — giving up")
            r.close()
            return False
        ctype = r.headers.get("content-type", "")
        if "text/html" in ctype:
            print(f"  {label:18} got HTML not a file (blocked?) — giving up")
            r.close()
            return False
        tmp = out + ".part"
        n = 0
        with open(tmp, "wb") as fh:
            for chunk in r.iter_content(65536):
                fh.write(chunk); n += len(chunk)
        r.close()
        os.replace(tmp, out)
        print(f"  {label:18} OK  {filename}  {n/1048576:.2f} MB")
        return True
    print(f"  {label:18} FAILED after {tries} attempts")
    return False


def fetch_brazoria() -> bool:
    """Resolve the 1drv.ms share to a direct content URL and pull the xlsx."""
    out = os.path.join(DEST, "TaxRoll_Brazoria_Excel.xlsx")
    if os.path.exists(out) and os.path.getsize(out) > 1024:
        print(f"  {'BRAZORIA':18} already have the roll "
              f"({os.path.getsize(out)/1048576:.2f} MB)")
        return True
    s = _session("https://www.brazoriacountytx.gov/")
    try:
        r = s.get(BRAZORIA_SHARE, timeout=120, allow_redirects=True)
    except requests.RequestException as e:
        print(f"  {'BRAZORIA':18} redirect resolve failed: {type(e).__name__}")
        return False
    final = r.url
    print(f"  {'BRAZORIA':18} resolved -> {final[:95]}")
    # Try the usual OneDrive direct-content variants, in order.
    cands = []
    if "download=1" not in final:
        cands.append(final + ("&" if "?" in final else "?") + "download=1")
    cands.append(final.replace("redir?", "download?"))
    cands.append(final)
    for u in cands:
        try:
            rr = s.get(u, timeout=180, stream=True)
        except requests.RequestException:
            continue
        ctype = rr.headers.get("content-type", "")
        if rr.status_code == 200 and "html" not in ctype:
            tmp = out + ".part"; n = 0
            with open(tmp, "wb") as fh:
                for chunk in rr.iter_content(65536):
                    fh.write(chunk); n += len(chunk)
            rr.close()
            os.replace(tmp, out)
            print(f"  {'BRAZORIA':18} OK  TaxRoll_Brazoria_Excel.xlsx  {n/1048576:.2f} MB")
            return True
        rr.close()
    print(f"  {'BRAZORIA':18} could not get a file body — needs a browser download")
    return False


def taylor_newest(max_days: int = 21) -> bool:
    """Taylor posts TaylorCAD_CollData_Delinquent_<DDMonYY>.zip roughly weekly
    under /wp-content/uploads/<YYYY>/<MM>/. The listing page 429s scripts, but
    the file URL is predictable, so walk back day by day to the newest one."""
    from datetime import date, timedelta
    ref = "https://taylor-cad.org/data-downloads/"
    d = date.today()
    # 9/19 on Render the WAF 429'd every probe and this walk ate 37 minutes
    # before the tax check could start. Cap it; a stale roll beats a late run.
    deadline = time.time() + 6 * 60
    for _ in range(max_days):
        if time.time() > deadline:
            print(f"  {'TAYLOR':18} gave up after 6 min of rate-limits")
            return False
        fn = f"TaylorCAD_CollData_Delinquent_{d.strftime('%d%b%y')}.zip"
        if os.path.exists(os.path.join(DEST, fn)):
            print(f"  {'TAYLOR':18} already have newest {fn}")
            return True
        url = f"https://taylor-cad.org/wp-content/uploads/{d:%Y}/{d:%m}/{fn}"
        ok = False
        for wait in (0, 30, 90):       # the WAF answers bursts with 429
            time.sleep(wait)
            try:
                r = _session(ref).get(url, timeout=30, stream=True)
                code, ctype = r.status_code, r.headers.get("content-type", "")
                r.close()
            except requests.RequestException:
                code, ctype = 0, ""
            if code != 429:
                ok = code == 200 and "html" not in ctype
                break
        if ok:
            return fetch("TAYLOR", fn, url, ref)
        time.sleep(4)
        d -= timedelta(days=1)
    print(f"  {'TAYLOR':18} no file found in the last {max_days} days")
    return False


def main() -> int:
    os.makedirs(DEST, exist_ok=True)
    print(f"downloading official county bulk rolls -> {DEST}\n")
    ok = {}
    for label, (fn, url, ref) in SOURCES.items():
        ok[label] = fetch(label, fn, url, ref)
        time.sleep(4)          # be a good citizen between counties
    # BRAZORIA is a live lookup now; its roll is no longer needed.
    ok["TAYLOR"] = taylor_newest()
    print("\nsummary:")
    for k, v in ok.items():
        print(f"  {k:18} {'OK' if v else 'FAILED'}")
    return 0 if all(ok.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
