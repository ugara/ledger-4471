"""
Motore di analisi tecnica: recupera storico prezzi reali (CoinGecko per crypto,
Stooq per stock) e calcola indicatori con formule vere — RSI, SMA, EMA,
supporti/resistenze — invece di farli "indovinare" a un modello da uno screenshot.

Nessuna chiave API richiesta per nessuna delle due fonti dati.
"""

import requests
import statistics


def log(msg):
    print(f"[indicators] {msg}", flush=True)


# ============================================================
# RECUPERO STORICO PREZZI
# ============================================================

def find_coingecko_id(ticker, name_hint=""):
    """Cerca l'id CoinGecko corrispondente a un ticker (es. BTC -> bitcoin).
    Usa l'endpoint di ricerca pubblico, nessuna chiave richiesta."""
    try:
        resp = requests.get(
            "https://api.coingecko.com/api/v3/search",
            params={"query": name_hint or ticker},
            timeout=15,
        )
        resp.raise_for_status()
        coins = resp.json().get("coins", [])
        if not coins:
            return None
        # preferisci un match esatto sul simbolo
        exact = [c for c in coins if c.get("symbol", "").upper() == ticker.upper()]
        best = exact[0] if exact else coins[0]
        return best.get("id")
    except Exception as e:
        log(f"Errore ricerca CoinGecko per {ticker}: {e}")
        return None


def fetch_crypto_history_binance(ticker, days=365):
    """Prova prima Binance: candele giornaliere VERE (OHLC reale) su un anno
    intero, dati di mercato pubblici senza bisogno di chiave API. Copre bene i
    principali asset ma non i token più piccoli/nuovi (fallback su CoinGecko)."""
    try:
        resp = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": f"{ticker.upper()}USDT", "interval": "1d", "limit": min(days, 1000)},
            timeout=15,
        )
        if resp.status_code != 200:
            return []
        raw = resp.json()
        if not isinstance(raw, list) or not raw:
            return []
        return [
            (ts_to_date_str(int(c[0])), float(c[1]), float(c[2]), float(c[3]), float(c[4]))
            for c in raw
        ]
    except Exception as e:
        log(f"Binance non disponibile per {ticker}: {e}")
        return []


def ts_to_date_str(ts_ms):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def fetch_crypto_history(ticker, name_hint="", days=365):
    """Ritorna candele OHLC reali. Prova prima Binance (candele giornaliere vere);
    se il ticker non è listato lì, ripiega su CoinGecko (candele più larghe oltre
    i 30 giorni — limite della loro API gratuita, non nostro)."""
    binance_data = fetch_crypto_history_binance(ticker, days)
    if binance_data:
        log(f"{ticker}: storico giornaliero reale da Binance ({len(binance_data)} candele)")
        return binance_data

    log(f"{ticker}: non trovato su Binance, ripiego su CoinGecko (candele più larghe)")
    coin_id = find_coingecko_id(ticker, name_hint)
    if not coin_id:
        log(f"Nessun id CoinGecko trovato per {ticker}")
        return []
    try:
        resp = requests.get(
            f"https://api.coingecko.com/api/v3/coins/{coin_id}/ohlc",
            params={"vs_currency": "usd", "days": days},
            timeout=20,
        )
        resp.raise_for_status()
        candles = resp.json()  # [[timestamp, open, high, low, close], ...]
        return [(ts_to_date_str(int(c[0])), float(c[1]), float(c[2]), float(c[3]), float(c[4])) for c in candles]
    except Exception as e:
        log(f"Errore recupero OHLC CoinGecko per {ticker} ({coin_id}): {e}")
        return []


def fetch_stock_history(ticker, days=365):
    """Ritorna una lista di (data, open, high, low, close) da Stooq (CSV pubblico)."""
    try:
        resp = requests.get(
            f"https://stooq.com/q/d/l/",
            params={"s": f"{ticker.lower()}.us", "i": "d"},
            timeout=20,
        )
        resp.raise_for_status()
        lines = resp.text.strip().split("\n")
        if len(lines) < 2 or "Date" not in lines[0]:
            log(f"Stooq non ha dati validi per {ticker}: {lines[0] if lines else 'vuoto'}")
            return []
        rows = []
        for line in lines[1:]:
            parts = line.split(",")
            if len(parts) < 5:
                continue
            date, o, h, l, c = parts[0], parts[1], parts[2], parts[3], parts[4]
            try:
                rows.append((date, float(o), float(h), float(l), float(c)))
            except ValueError:
                continue
        return rows[-days:]
    except Exception as e:
        log(f"Errore recupero storico Stooq per {ticker}: {e}")
        return []


# ============================================================
# INDICATORI (formule standard, calcolate sui prezzi di chiusura)
# ============================================================

def sma(closes, period):
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period


def ema_series(closes, period):
    """Ritorna l'intera serie EMA (serve per l'RSI e per il grafico)."""
    if len(closes) < period:
        return []
    k = 2 / (period + 1)
    ema_vals = [sum(closes[:period]) / period]  # SMA iniziale come seed
    for price in closes[period:]:
        ema_vals.append(price * k + ema_vals[-1] * (1 - k))
    return ema_vals


def sma_series(closes, period):
    """Serie completa di SMA (per sovrapporla al grafico), con None dove non
    c'è ancora storico sufficiente — Chart.js interpreta None come 'nessun punto'."""
    result = []
    for i in range(len(closes)):
        if i + 1 < period:
            result.append(None)
        else:
            result.append(sum(closes[i + 1 - period:i + 1]) / period)
    return result


def ema(closes, period):
    series = ema_series(closes, period)
    return series[-1] if series else None


def rsi(closes, period=14):
    """RSI standard (media mobile semplice di Wilder sui guadagni/perdite)."""
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 1)


def support_resistance(closes, lookback=90):
    """Stima semplice ma robusta: minimo/massimo del periodo recente come
    supporto/resistenza principali, più il range a lungo termine per contesto."""
    recent = closes[-lookback:] if len(closes) >= lookback else closes
    long_term = closes if len(closes) >= lookback else closes

    return {
        "support_recent": round(min(recent), 6),
        "resistance_recent": round(max(recent), 6),
        "support_long": round(min(long_term), 6),
        "resistance_long": round(max(long_term), 6),
    }


def trend_direction(closes):
    """Lettura semplice e onesta del trend primario: confronta SMA50 vs SMA200."""
    s50 = sma(closes, 50)
    s200 = sma(closes, 200)
    if s50 is None or s200 is None:
        return "indeterminato (storico insufficiente per SMA200)"
    if s50 > s200 * 1.02:
        return "rialzista (SMA50 sopra SMA200)"
    if s50 < s200 * 0.98:
        return "ribassista (SMA50 sotto SMA200)"
    return "laterale/incerto (SMA50 e SMA200 vicine)"


# ============================================================
# FUNZIONE PRINCIPALE: analisi completa di un asset
# ============================================================

def resample_ohlc(candles, period):
    """Aggrega candele giornaliere {date, o, h, l, c} in candele settimanali
    ('W') o mensili ('M'). Necessario per un RSI davvero multi-timeframe,
    come farebbe un trader guardando grafici a diversa granularità."""
    from datetime import datetime
    groups = {}
    order = []
    for c in candles:
        d = datetime.strptime(c["date"], "%Y-%m-%d")
        if period == "W":
            key = d.strftime("%Y-W%W")
        else:  # 'M'
            key = d.strftime("%Y-%m")
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(c)

    resampled = []
    for key in order:
        bucket = groups[key]
        resampled.append({
            "date": bucket[0]["date"],
            "o": bucket[0]["o"],
            "h": max(x["h"] for x in bucket),
            "l": min(x["l"] for x in bucket),
            "c": bucket[-1]["c"],
        })
    return resampled


def analyze_asset(ticker, asset_type, name_hint=""):
    """Ritorna un dizionario con tutti gli indicatori calcolati per un asset,
    pronto da passare a Claude per l'interpretazione qualitativa. Include anche
    le candele OHLC per il grafico a candele, e un RSI calcolato separatamente
    su base giornaliera/settimanale/mensile (multi-timeframe reale)."""
    if asset_type == "crypto":
        history = fetch_crypto_history(ticker, name_hint)  # [(date_str, o, h, l, c), ...]
    else:
        history = fetch_stock_history(ticker)  # [(date_str, o, h, l, c), ...]

    candles = [{"date": d, "o": o, "h": h, "l": l, "c": c} for d, o, h, l, c in history]
    closes = [c["c"] for c in candles]

    if len(closes) < 30:
        log(f"Storico insufficiente per {ticker} ({len(closes)} punti) — analisi limitata.")
        return {
            "ticker": ticker,
            "sufficient_data": False,
            "closes": closes,
            "candles": candles,
        }

    weekly_candles = resample_ohlc(candles, "W")
    monthly_candles = resample_ohlc(candles, "M")
    weekly_closes = [c["c"] for c in weekly_candles]
    monthly_closes = [c["c"] for c in monthly_candles]

    return {
        "ticker": ticker,
        "sufficient_data": True,
        "closes": closes,
        "candles": candles,
        "current_price": closes[-1],
        "sma50": sma(closes, 50),
        "sma200": sma(closes, 200),
        "sma50_series": sma_series(closes, 50),
        "sma200_series": sma_series(closes, 200),
        "ema12": ema(closes, 12),
        "ema26": ema(closes, 26),
        "rsi_daily": rsi(closes, 14),
        "rsi_weekly": rsi(weekly_closes, 14) if len(weekly_closes) >= 15 else None,
        "rsi_monthly": rsi(monthly_closes, 14) if len(monthly_closes) >= 15 else None,
        "trend": trend_direction(closes),
        **support_resistance(closes),
    }
