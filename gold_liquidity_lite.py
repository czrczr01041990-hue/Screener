"""
GOLD liquidity scanner (Termux friendly, only Python needed)

Run:  python gold_liquidity_lite.py

IDEA: stop-losses pile up just above equal highs / previous-day high and just
below equal lows / previous-day low. Price often "sweeps" those levels (wicks
through them) and snaps back. This script:
  1. Finds liquidity pools: equal highs, equal lows, PDH, PDL
  2. Finds high-traded-volume price zones (volume profile: POC + top zones)
  3. Alerts when the last COMPLETED candle sweeps a pool and closes back
     inside, with a volume spike:
        high pool swept, close back below  -> SHORT
        low pool swept,  close back above  -> LONG

Data: Yahoo Finance GC=F (COMEX gold futures, USD, delayed, unofficial).
NOTE: MCX gold prices differ (INR, duties, USDINR). Use this for levels and
direction on COMEX; do not copy price levels onto MCX.
"""

import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

# ---------- SETTINGS ----------
SYMBOL = "GC=F"
INTERVAL = "15m"
SWING_K = 3               # swing = highest/lowest vs 3 candles each side
EQ_TOL_PCT = 0.04         # highs/lows within 0.04% count as "equal"
VOL_MULT = 1.2            # sweep candle volume >= 1.2x average of last 20
SL_BUFFER_PCT = 0.03      # SL beyond sweep wick by 0.03%
RR_T1 = 1.5
RR_T2 = 2.5
PROFILE_BINS = 40
HVN_NEAR_PCT = 0.10       # pool within 0.10% of a high-volume zone = confluence
REFRESH_SEC = 300

TELEGRAM_BOT_TOKEN = ""   # from @BotFather (leave empty to disable)
TELEGRAM_CHAT_ID = ""     # your chat id
# ------------------------------

IST = timezone(timedelta(hours=5, minutes=30))
HEADERS = {"User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36"}
MIN_MIN = int(INTERVAL.replace("m", ""))


def fetch():
    """Completed candles: (datetime, o, h, l, c, v)."""
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
           + urllib.parse.quote(SYMBOL) + f"?interval={INTERVAL}&range=5d")
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as r:
        js = json.loads(r.read().decode())
    res = js["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    now = datetime.now(IST)
    out = []
    for i, ts in enumerate(res.get("timestamp") or []):
        o, h, l, c, v = (q["open"][i], q["high"][i], q["low"][i],
                         q["close"][i], q["volume"][i])
        if None in (o, h, l, c):
            continue
        dt = datetime.fromtimestamp(ts, IST)
        if now < dt + timedelta(minutes=MIN_MIN):
            continue                      # candle still forming
        out.append((dt, o, h, l, c, v or 0))
    return out


def swings(c, k=SWING_K):
    hs, ls = [], []
    for i in range(k, len(c) - k):
        nb = c[i - k:i] + c[i + 1:i + k + 1]
        if c[i][2] > max(x[2] for x in nb):
            hs.append(i)
        if c[i][3] < min(x[3] for x in nb):
            ls.append(i)
    return hs, ls


def cluster(c, idxs, key, tol):
    pts = sorted((c[i][key], i) for i in idxs)
    groups = []
    for p, i in pts:
        if groups and abs(p - groups[-1][-1][0]) <= tol:
            groups[-1].append((p, i))
        else:
            groups.append([(p, i)])
    return groups


def find_pools(c):
    """Unswept liquidity pools as of the candle BEFORE the last one."""
    price = c[-1][4]
    tol = price * EQ_TOL_PCT / 100
    hs, ls = swings(c)
    pools = []

    for g in cluster(c, hs, 2, tol):
        if len(g) >= 2:
            lvl = max(p for p, _ in g)
            last_i = max(i for _, i in g)
            if all(c[j][2] <= lvl for j in range(last_i + 1, len(c) - 1)):
                pools.append({"side": "high", "level": lvl,
                              "label": f"Equal highs x{len(g)}"})
    for g in cluster(c, ls, 3, tol):
        if len(g) >= 2:
            lvl = min(p for p, _ in g)
            last_i = max(i for _, i in g)
            if all(c[j][3] >= lvl for j in range(last_i + 1, len(c) - 1)):
                pools.append({"side": "low", "level": lvl,
                              "label": f"Equal lows x{len(g)}"})

    today = c[-1][0].date()
    prev = [i for i, x in enumerate(c) if x[0].date() < today]
    if prev:
        pday = c[prev[-1]][0].date()
        pidx = [i for i in prev if c[i][0].date() == pday]
        pdh = max(c[i][2] for i in pidx)
        pdl = min(c[i][3] for i in pidx)
        last_i = pidx[-1]
        if all(c[j][2] <= pdh for j in range(last_i + 1, len(c) - 1)):
            pools.append({"side": "high", "level": pdh, "label": "PDH"})
        if all(c[j][3] >= pdl for j in range(last_i + 1, len(c) - 1)):
            pools.append({"side": "low", "level": pdl, "label": "PDL"})
    return pools


def volume_profile(c):
    lo = min(x[3] for x in c)
    hi = max(x[2] for x in c)
    if hi <= lo:
        return None, []
    w = (hi - lo) / PROFILE_BINS
    bins = [0.0] * PROFILE_BINS
    for x in c:
        tp = (x[2] + x[3] + x[4]) / 3
        b = min(int((tp - lo) / w), PROFILE_BINS - 1)
        bins[b] += x[5]
    order = sorted(range(PROFILE_BINS), key=lambda b: bins[b], reverse=True)
    centers = [lo + (b + 0.5) * w for b in order[:3]]
    return centers[0], centers      # POC, top-3 high-volume zones


def analyze(c):
    if len(c) < 40:
        return None, None
    pools = find_pools(c)
    poc, hvns = volume_profile(c)
    last = c[-1]
    o, h, l, cl = last[1], last[2], last[3], last[4]

    hist = [x[5] for x in c[-21:-1]]
    avg = sum(hist) / len(hist) if hist else 0
    vol_ratio = last[5] / avg if avg else 0

    info = {"pools": pools, "poc": poc, "hvns": hvns, "price": cl}

    cands = []
    for p in pools:
        if p["side"] == "high" and h > p["level"] and cl < p["level"]:
            cands.append(("SHORT", p))
        if p["side"] == "low" and l < p["level"] and cl > p["level"]:
            cands.append(("LONG", p))
    if not cands or vol_ratio < VOL_MULT:
        return None, info

    signal, pool = max(cands, key=lambda x: abs(x[1]["level"] - cl))
    entry = cl
    buf = entry * SL_BUFFER_PCT / 100
    if signal == "SHORT":
        sl = h + buf
        risk = sl - entry
        t1 = entry - RR_T1 * risk
        below = [p["level"] for p in pools if p["side"] == "low" and p["level"] < t1]
        t2 = max(below) if below else entry - RR_T2 * risk
    else:
        sl = l - buf
        risk = entry - sl
        t1 = entry + RR_T1 * risk
        above = [p["level"] for p in pools if p["side"] == "high" and p["level"] > t1]
        t2 = min(above) if above else entry + RR_T2 * risk
    if risk <= 0:
        return None, info

    near = any(abs(pool["level"] - z) <= entry * HVN_NEAR_PCT / 100 for z in hvns)
    return {
        "Signal": signal, "Pool": f"{pool['label']} @ {pool['level']:.1f}",
        "Entry": round(entry, 1), "SL": round(sl, 1),
        "T1": round(t1, 1), "T2": round(t2, 1),
        "Risk": round(risk, 1), "Vol x": round(vol_ratio, 1),
        "HVN zone": "Yes" if near else "No",
        "Time": last[0].strftime("%d-%b %H:%M"),
    }, info


alerted = set()


def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": text}).encode()
    try:
        urllib.request.urlopen(url, data, timeout=15).read()
    except Exception as e:
        print("Telegram error:", e)


def alert(r):
    key = (r["Time"], r["Signal"])
    if key in alerted:
        return
    alerted.add(key)
    emoji = "🟢" if r["Signal"] == "LONG" else "🔴"
    send_telegram(
        f"{emoji} GOLD {r['Signal']} liquidity sweep ({r['Time']})\n"
        f"Swept: {r['Pool']}\n"
        f"Entry: {r['Entry']}\n"
        f"SL: {r['SL']}  (risk {r['Risk']})\n"
        f"T1: {r['T1']}\n"
        f"T2: {r['T2']}\n"
        f"Vol: {r['Vol x']}x | High-volume zone: {r['HVN zone']}"
    )


def show_map(info):
    if not info:
        return
    p = info["price"]
    up = sorted((x for x in info["pools"] if x["level"] > p), key=lambda x: x["level"])[:3]
    dn = sorted((x for x in info["pools"] if x["level"] < p),
                key=lambda x: x["level"], reverse=True)[:3]
    print(f"Price {p:.1f} | POC {info['poc']:.1f}" if info["poc"] else f"Price {p:.1f}")
    for x in reversed(up):
        print(f"  above: {x['level']:.1f}  {x['label']}")
    for x in dn:
        print(f"  below: {x['level']:.1f}  {x['label']}")


def scan():
    c = fetch()
    r, info = analyze(c)
    show_map(info)
    if r:
        print(f"{r['Signal']} {r['Pool']}  E {r['Entry']}  SL {r['SL']}  "
              f"T1 {r['T1']}  T2 {r['T2']}  Vol {r['Vol x']}x")
        alert(r)
    else:
        print("No liquidity sweep right now.")


if __name__ == "__main__":
    while True:
        print("\n" + "=" * 60)
        print("Scan @", datetime.now(IST).strftime("%d-%b %H:%M:%S"))
        try:
            scan()
        except Exception as e:
            print("Error:", e)
        time.sleep(REFRESH_SEC)
