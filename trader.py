import os
import json
import logging
import sqlite3
from datetime import datetime, timedelta
import pytz
import requests as http_requests

log = logging.getLogger(__name__)

IBKR_BASE         = os.environ.get("IBKR_BASE_URL", "https://localhost:5000/v1/api")
IBKR_ACCOUNT      = os.environ.get("IBKR_ACCOUNT_ID", "")
MAX_POSITIONS     = int(os.environ.get("MAX_POSITIONS", "10"))
MAX_STOP_LOSS_USD = float(os.environ.get("MAX_STOP_LOSS_USD", "50"))
STOP_LOSS_PCT     = float(os.environ.get("STOP_LOSS_PCT", "0.05"))
TRAIL_TRIGGER_PCT = float(os.environ.get("TRAIL_TRIGGER_PCT", "0.10"))
POLYGON_API_KEY   = os.environ.get("POLYGON_API_KEY", "")
PAPER_TRADING     = os.environ.get("PAPER_TRADING", "true").lower() == "true"
TRADE_DB          = os.environ.get("DB_PATH", "/tmp/digest.db")
ET                = pytz.timezone("America/New_York")
MAX_SECTOR_POS    = 3
TIME_STOP_DAYS    = 15
SCALE_OUT_PCT     = 0.10
EARNINGS_BUF_DAYS = 14

SECTOR_ETFS = {
    "Technology":"XLK","Energy":"XLE","Health Care":"XLV","Financials":"XLF",
    "Industrials":"XLI","Materials":"XLB","Consumer":"XLY","Staples":"XLP",
}

SP500_WATCHLIST = [
    ("AAPL","Technology"),("MSFT","Technology"),("NVDA","Technology"),
    ("AVGO","Technology"),("ADBE","Technology"),("CRM","Technology"),
    ("ORCL","Technology"),("AMD","Technology"),("QCOM","Technology"),
    ("TXN","Technology"),("INTU","Technology"),("ADI","Technology"),
    ("XOM","Energy"),("CVX","Energy"),("COP","Energy"),("EOG","Energy"),
    ("SLB","Energy"),("MPC","Energy"),("PSX","Energy"),("VLO","Energy"),
    ("UNH","Health Care"),("LLY","Health Care"),("JNJ","Health Care"),
    ("MRK","Health Care"),("ABBV","Health Care"),("TMO","Health Care"),
    ("ABT","Health Care"),("ISRG","Health Care"),("SYK","Health Care"),
    ("GILD","Health Care"),("VRTX","Health Care"),("REGN","Health Care"),
    ("JPM","Financials"),("BAC","Financials"),("GS","Financials"),
    ("MS","Financials"),("BLK","Financials"),("SCHW","Financials"),
    ("AXP","Financials"),("CB","Financials"),("PNC","Financials"),
    ("HON","Industrials"),("CAT","Industrials"),("RTX","Industrials"),
    ("UPS","Industrials"),("LMT","Industrials"),("GE","Industrials"),
    ("EMR","Industrials"),("ETN","Industrials"),("DE","Industrials"),
    ("AMZN","Consumer"),("TSLA","Consumer"),("HD","Consumer"),
    ("MCD","Consumer"),("NKE","Consumer"),("LOW","Consumer"),
    ("PG","Staples"),("KO","Staples"),("PEP","Staples"),
    ("WMT","Staples"),("COST","Staples"),("PM","Staples"),
    ("LIN","Materials"),("FCX","Materials"),("APD","Materials"),
    ("SHW","Materials"),("NEM","Materials"),
]

def poly_get(path, params={}):
    if not POLYGON_API_KEY:
        return None
    try:
        p = dict(params)
        p["apiKey"] = POLYGON_API_KEY
        r = http_requests.get(f"https://api.polygon.io{path}", params=p, timeout=15)
        return r.json() if r.status_code == 200 else None
    except Exception as e:
        log.warning(f"Polygon {path}: {e}")
        return None

def get_bars(ticker, days=200):
    end   = datetime.now(ET).strftime("%Y-%m-%d")
    start = (datetime.now(ET) - timedelta(days=days+60)).strftime("%Y-%m-%d")
    data  = poly_get(f"/v2/aggs/ticker/{ticker}/range/1/day/{start}/{end}",
                     {"adjusted":"true","sort":"asc","limit":500})
    return data.get("results", []) if data else []

def get_current_price(ticker):
    data = poly_get(f"/v2/last/trade/{ticker}")
    if data:
        p = data.get("results", {}).get("p")
        if p:
            return p
    bars = get_bars(ticker, days=5)
    return bars[-1]["c"] if bars else None

def sma(bars, period, field="c"):
    vals = [b[field] for b in bars if field in b]
    if len(vals) < period:
        return None
    return sum(vals[-period:]) / period

def atr(bars, period=14):
    if len(bars) < period + 1:
        return None
    trs = []
    for i in range(1, len(bars)):
        trs.append(max(bars[i]["h"]-bars[i]["l"],
                       abs(bars[i]["h"]-bars[i-1]["c"]),
                       abs(bars[i]["l"]-bars[i-1]["c"])))
    return sum(trs[-period:]) / period

def relative_strength(bars, spy_bars, period=63):
    if len(bars) < period or len(spy_bars) < period:
        return None
    sr = (bars[-1]["c"] - bars[-period]["c"]) / bars[-period]["c"]
    mr = (spy_bars[-1]["c"] - spy_bars[-period]["c"]) / spy_bars[-period]["c"]
    return sr - mr

def market_is_bullish():
    bars = get_bars("SPY", days=210)
    if not bars or len(bars) < 200:
        return True
    ma200 = sma(bars, 200)
    price = bars[-1]["c"]
    bull  = price > ma200
    log.info(f"Market: SPY={price:.2f} MA200={ma200:.2f} {'BULL' if bull else 'BEAR'}")
    return bull

def sector_is_bullish(sector):
    etf = SECTOR_ETFS.get(sector)
    if not etf:
        return True
    bars = get_bars(etf, days=60)
    if not bars or len(bars) < 50:
        return True
    return bars[-1]["c"] > sma(bars, 50)

def earnings_approaching(ticker):
    try:
        data = poly_get(f"/vX/reference/financials",
                        {"ticker": ticker, "limit": 4, "sort": "filing_date"})
        if not data:
            return False
        today = datetime.now(ET).date()
        for r in data.get("results", []):
            d = r.get("start_date", "")
            if d:
                try:
                    edate = datetime.strptime(d[:10], "%Y-%m-%d").date()
                    if 0 <= (edate - today).days <= EARNINGS_BUF_DAYS:
                        return True
                except Exception:
                    pass
    except Exception:
        pass
    return False

def check_entry_signal(ticker, sector, spy_bars):
    bars = get_bars(ticker, days=200)
    if not bars or len(bars) < 160:
        return None
    close = bars[-1]["c"]
    ma20  = sma(bars, 20)
    ma50  = sma(bars, 50)
    ma150 = sma(bars, 150)
    if not all([ma20, ma50, ma150]):
        return None
    if close <= ma50 or close <= ma150:
        return None
    vols = [b["v"] for b in bars if "v" in b]
    if len(vols) < 30:
        return None
    vol_10 = sum(vols[-10:]) / 10
    vol_30 = sum(vols[-30:]) / 30
    if vol_10 < vol_30 * 1.20:
        return None
    highs_20 = [b["h"] for b in bars[-20:]]
    lows_20  = [b["l"] for b in bars[-20:]]
    high_20  = max(highs_20)
    low_20   = min(lows_20)
    range_20 = high_20 - low_20
    if close > high_20 * 1.03:
        return None
    if close < (low_20 + range_20 * 0.5):
        return None
    if range_20 / low_20 > 0.20:
        return None
    rs = relative_strength(bars, spy_bars, period=63)
    if rs is not None and rs < 0:
        return None
    if not sector_is_bullish(sector):
        return None
    if earnings_approaching(ticker):
        log.info(f"  Skip {ticker} — earnings approaching")
        return None
    return {"ticker":ticker,"sector":sector,"price":close,"ma20":ma20,
            "ma50":ma50,"ma150":ma150,"vol_10":vol_10,"vol_30":vol_30,
            "high_20":high_20,"low_20":low_20,"atr":atr(bars,14),"rs":rs}

def ibkr_post(path, data):
    try:
        r = http_requests.post(f"{IBKR_BASE}{path}", json=data, verify=False, timeout=10)
        return r.json() if r.status_code in (200,201) else None
    except Exception as e:
        log.warning(f"IBKR POST {path}: {e}")
        return None

def ibkr_get(path):
    try:
        r = http_requests.get(f"{IBKR_BASE}{path}", verify=False, timeout=10)
        return r.json() if r.status_code == 200 else None
    except Exception as e:
        log.warning(f"IBKR GET {path}: {e}")
        return None

def get_conid(ticker):
    data = ibkr_get(f"/iserver/secdef/search?symbol={ticker}&secType=STK&exchange=SMART")
    return data[0].get("conid") if data and len(data) > 0 else None

def place_order(ticker, qty, side="BUY", order_type="MKT", aux_price=None):
    conid = get_conid(ticker)
    if not conid:
        log.warning(f"No conid for {ticker}")
        return None
    order = {"acctId":IBKR_ACCOUNT,"conid":conid,"orderType":order_type,
             "side":side,"quantity":qty,"tif":"DAY" if order_type=="MKT" else "GTC"}
    if aux_price:
        order["auxPrice"] = round(aux_price, 2)
    prefix = "[PAPER] " if PAPER_TRADING else ""
    log.info(f"{prefix}{side} {qty}x {ticker} {order_type}" + (f" @{aux_price:.2f}" if aux_price else ""))
    return ibkr_post(f"/iserver/account/{IBKR_ACCOUNT}/orders", {"orders": [order]})

def init_trade_tables():
    conn = sqlite3.connect(TRADE_DB)
    conn.execute("""CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker TEXT, sector TEXT, entry_price REAL, qty INTEGER,
        stop_price REAL, trail_active INTEGER DEFAULT 0,
        scaled_out INTEGER DEFAULT 0, peak_price REAL, ma20 REAL,
        opened_at TEXT DEFAULT (datetime('now')), closed_at TEXT,
        close_price REAL, pnl REAL, close_reason TEXT,
        status TEXT DEFAULT 'open')""")
    conn.commit()
    conn.close()

def get_open_trades():
    conn = sqlite3.connect(TRADE_DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM trades WHERE status='open'").fetchall()
    conn.close()
    return [dict(r) for r in rows]

def save_trade(ticker, sector, entry_price, qty, stop_price, ma20):
    conn = sqlite3.connect(TRADE_DB)
    conn.execute("INSERT INTO trades (ticker,sector,entry_price,qty,stop_price,peak_price,ma20) VALUES (?,?,?,?,?,?,?)",
                 (ticker, sector, entry_price, qty, stop_price, entry_price, ma20))
    conn.commit()
    conn.close()

def update_trade(trade_id, **kwargs):
    conn = sqlite3.connect(TRADE_DB)
    sets = ", ".join(f"{k}=?" for k in kwargs)
    conn.execute(f"UPDATE trades SET {sets} WHERE id=?", list(kwargs.values()) + [trade_id])
    conn.commit()
    conn.close()

def close_trade(trade_id, close_price, reason=""):
    conn = sqlite3.connect(TRADE_DB)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
    if row:
        pnl = (close_price - row["entry_price"]) * row["qty"]
        conn.execute("UPDATE trades SET status='closed',closed_at=datetime('now'),close_price=?,pnl=?,close_reason=? WHERE id=?",
                     (close_price, round(pnl,2), reason, trade_id))
    conn.commit()
    conn.close()

def get_trade_summary():
    init_trade_tables()
    conn = sqlite3.connect(TRADE_DB)
    conn.row_factory = sqlite3.Row
    open_t   = [dict(r) for r in conn.execute("SELECT * FROM trades WHERE status='open'").fetchall()]
    closed_t = [dict(r) for r in conn.execute("SELECT * FROM trades WHERE status='closed' ORDER BY closed_at DESC LIMIT 30").fetchall()]
    conn.close()
    total_pnl = sum(t["pnl"] or 0 for t in closed_t)
    wins      = sum(1 for t in closed_t if (t["pnl"] or 0) > 0)
    losses    = sum(1 for t in closed_t if (t["pnl"] or 0) <= 0)
    return {"open_trades":open_t,"closed_trades":closed_t,"total_pnl":round(total_pnl,2),
            "wins":wins,"losses":losses,
            "win_rate":round(wins/(wins+losses)*100,1) if (wins+losses)>0 else 0}

def run_daily_scan():
    log.info("=== Daily scan starting ===")
    init_trade_tables()
    now_et = datetime.now(ET)
    if now_et.weekday() >= 5:
        log.info("Weekend — skipping")
        return

    bullish = market_is_bullish()
    spy_bars = get_bars("SPY", days=200)
    open_trades  = get_open_trades()
    tickers_held = {t["ticker"] for t in open_trades}

    for trade in open_trades:
        ticker = trade["ticker"]
        price  = get_current_price(ticker)
        if not price:
            continue
        peak = max(trade["peak_price"] or price, price)
        update_trade(trade["id"], peak_price=peak)
        bars = get_bars(ticker, days=30)
        ma20 = sma(bars, 20) if bars else trade["ma20"]

        if ma20 and price < ma20:
            log.info(f"EXIT {ticker} @ {price:.2f} below 20MA")
            place_order(ticker, trade["qty"], side="SELL")
            close_trade(trade["id"], price, "20MA exit")
            continue

        if now_et.weekday() == 4:
            pct = (price - trade["entry_price"]) / trade["entry_price"]
            if pct < 0:
                log.info(f"EXIT {ticker} Friday loss trim")
                place_order(ticker, trade["qty"], side="SELL")
                close_trade(trade["id"], price, "Friday trim")
                continue

        opened    = datetime.strptime(trade["opened_at"][:10], "%Y-%m-%d").date()
        days_held = (now_et.date() - opened).days
        profit    = (price - trade["entry_price"]) / trade["entry_price"]

        if days_held >= TIME_STOP_DAYS and profit < 0.05:
            log.info(f"EXIT {ticker} time stop {days_held}d {profit*100:.1f}%")
            place_order(ticker, trade["qty"], side="SELL")
            close_trade(trade["id"], price, "Time stop")
            continue

        if profit >= SCALE_OUT_PCT and not trade["scaled_out"]:
            half = max(1, trade["qty"] // 2)
            log.info(f"SCALE OUT {ticker} selling {half} @ {profit*100:.1f}%")
            place_order(ticker, half, side="SELL")
            update_trade(trade["id"], scaled_out=1, qty=trade["qty"]-half)

        if profit >= TRAIL_TRIGGER_PCT and not trade["trail_active"]:
            trail = peak * (1 - STOP_LOSS_PCT)
            log.info(f"TRAIL STOP {ticker} @ {trail:.2f}")
            update_trade(trade["id"], trail_active=1, stop_price=trail)
        elif trade["trail_active"]:
            new_stop = peak * (1 - STOP_LOSS_PCT)
            if new_stop > trade["stop_price"]:
                update_trade(trade["id"], stop_price=new_stop)

        if price <= trade["stop_price"]:
            log.info(f"STOP HIT {ticker} @ {price:.2f}")
            place_order(ticker, trade["qty"], side="SELL")
            close_trade(trade["id"], price, "Stop loss")

    if not bullish:
        log.info("BEARISH — no new entries")
        return

    open_trades     = get_open_trades()
    slots           = MAX_POSITIONS - len(open_trades)
    if slots <= 0:
        return
    sector_counts   = {}
    for t in open_trades:
        s = t.get("sector","Unknown")
        sector_counts[s] = sector_counts.get(s,0) + 1

    log.info(f"Scanning {len(SP500_WATCHLIST)} stocks for {slots} slots...")
    candidates = []
    for ticker, sector in SP500_WATCHLIST:
        if ticker in tickers_held:
            continue
        if sector_counts.get(sector, 0) >= MAX_SECTOR_POS:
            continue
        sig = check_entry_signal(ticker, sector, spy_bars)
        if sig:
            candidates.append(sig)
            log.info(f"  CANDIDATE {ticker} ({sector}) RS={sig['rs']:.3f if sig['rs'] else 'N/A'}")

    candidates.sort(key=lambda x: x.get("rs") or 0, reverse=True)
    log.info(f"Entering top {min(len(candidates),slots)} of {len(candidates)} candidates")

    for sig in candidates[:slots]:
        ticker = sig["ticker"]
        price  = sig["price"]
        a      = sig.get("atr")
        if a:
            stop   = price - a * 2
        else:
            stop   = price * (1 - STOP_LOSS_PCT)
        stop_usd = price - stop
        qty      = max(1, int(MAX_STOP_LOSS_USD / stop_usd))
        log.info(f"ENTRY {ticker} qty={qty} stop=${stop:.2f} max_loss=${qty*stop_usd:.2f}")
        place_order(ticker, qty, side="BUY")
        place_order(ticker, qty, side="SELL", order_type="STP", aux_price=stop)
        save_trade(ticker, sig["sector"], price, qty, stop, sig["ma20"])
        sector_counts[sig["sector"]] = sector_counts.get(sig["sector"],0) + 1

    log.info("=== Scan complete ===")
