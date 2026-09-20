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


def fetch_crypto_history_coinbase(ticker, days=365):
    """Coinbase Exchange (dati pubblici, nessuna chiave richiesta): candele
    giornaliere VERE. A differenza di Binance, Coinbase è un exchange
    regolamentato negli USA — non blocca il traffico dai datacenter
    statunitensi (dove girano i runner di GitHub Actions), quindi funziona
    dove Binance no (verificato: Binance dava errore 451 'restricted location').
    Copre bene gli asset principali, non i token più piccoli/nuovi."""
    try:
        resp = requests.get(
            f"https://api.exchange.coinbase.com/products/{ticker.upper()}-USD/candles",
            params={"granularity": 86400},  # 86400 secondi = candele giornaliere
            headers={"User-Agent": "cryptonary-ledger/1.0"},
            timeout=15,
        )
        if resp.status_code != 200:
            log(f"  DEBUG Coinbase per {ticker}: status {resp.status_code}, corpo: {resp.text[:200]}")
            return []
        raw = resp.json()
        if not isinstance(raw, list) or not raw:
            log(f"  DEBUG Coinbase per {ticker}: risposta senza dati validi: {str(raw)[:200]}")
            return []
        # formato Coinbase: [time, low, high, open, close, volume] — ordine
        # diverso da Binance/CoinGecko, e più recente per primo (va invertito)
        rows = [
            (ts_to_date_str(int(c[0]) * 1000), float(c[3]), float(c[2]), float(c[1]), float(c[4]))
            for c in raw
        ]
        rows.reverse()
        return rows
    except Exception as e:
        log(f"Coinbase non disponibile per {ticker}: {e}")
        return []


def ts_to_date_str(ts_ms):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def fetch_crypto_history(ticker, name_hint="", days=365):
    """Ritorna candele OHLC reali. Prova Coinbase (candele giornaliere vere,
    exchange USA non soggetto al blocco geografico di Binance sui datacenter
    statunitensi); se il ticker non è listato lì, ripiega su CoinGecko
    (candele più larghe oltre i 30 giorni — limite della loro API gratuita)."""
    coinbase_data = fetch_crypto_history_coinbase(ticker, days)
    if coinbase_data:
        log(f"{ticker}: storico giornaliero reale da Coinbase ({len(coinbase_data)} candele)")
        return coinbase_data

    log(f"{ticker}: non trovato su Coinbase, ripiego su CoinGecko (candele più larghe)")
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
    """Ritorna candele OHLC reali da Yahoo Finance (endpoint pubblico non
    ufficiale ma ampiamente usato, nessuna chiave richiesta). Sostituisce Stooq,
    che da marzo 2026 richiede una API key a richiesta (non più gratis diretto)."""
    try:
        range_param = "1y" if days > 180 else ("6mo" if days > 90 else "3mo")
        resp = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker.upper()}",
            params={"range": range_param, "interval": "1d"},
            headers={"User-Agent": "Mozilla/5.0 (compatible; cryptonary-ledger/1.0)"},
            timeout=20,
        )
        if resp.status_code != 200:
            log(f"Yahoo Finance per {ticker}: status {resp.status_code}, corpo: {resp.text[:200]}")
            return []
        data = resp.json()
        result_list = data.get("chart", {}).get("result")
        if not result_list:
            log(f"Yahoo Finance: nessun risultato per {ticker} — risposta: {str(data)[:200]}")
            return []

        result = result_list[0]
        timestamps = result.get("timestamp", [])
        quote = result.get("indicators", {}).get("quote", [{}])[0]
        opens, highs = quote.get("open", []), quote.get("high", [])
        lows, closes = quote.get("low", []), quote.get("close", [])

        rows = []
        for i, ts in enumerate(timestamps):
            if i >= len(opens) or None in (opens[i], highs[i], lows[i], closes[i]):
                continue
            rows.append((ts_to_date_str(ts * 1000), opens[i], highs[i], lows[i], closes[i]))
        return rows[-days:]
    except Exception as e:
        log(f"Errore recupero storico Yahoo Finance per {ticker}: {e}")
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
    series = rsi_series(closes, period)
    return series[-1] if series and series[-1] is not None else None


def rsi_series(closes, period=14):
    """Serie completa di RSI (un valore per ogni giorno, non solo l'ultimo) —
    necessaria per rilevare le divergenze RSI/prezzo con la matematica vera,
    invece di far indovinare a un modello un pattern che non può vedere."""
    if len(closes) < period + 1:
        return [None] * len(closes)

    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]

    result = [None] * period  # i primi 'period' punti non hanno RSI calcolabile
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    result.append(_rsi_from_avgs(avg_gain, avg_loss))

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        result.append(_rsi_from_avgs(avg_gain, avg_loss))

    return result


def _rsi_from_avgs(avg_gain, avg_loss):
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 1)


def find_swing_points(values, window=5):
    """Trova gli indici dei massimi e minimi locali (punti più alti/bassi di
    tutti i punti entro 'window' posizioni prima e dopo, usando i punti
    disponibili anche vicino ai bordi della serie — un massimo/minimo negli
    ultimi giorni disponibili è esattamente il caso più interessante da
    rilevare, quindi non può essere escluso solo perché mancano dati futuri
    per 'confermarlo')."""
    highs, lows = [], []
    n = len(values)
    for i in range(n):
        if values[i] is None:
            continue
        lo = max(0, i - window)
        hi = min(n, i + window + 1)
        segment = [v for v in values[lo:hi] if v is not None]
        if not segment:
            continue
        if values[i] == max(segment):
            highs.append(i)
        if values[i] == min(segment):
            lows.append(i)
    return highs, lows


def detect_rsi_divergence(closes, dates, lookback=90, window=5, rsi_period=14):
    """Rileva divergenze RSI/prezzo con la matematica vera (non fatte
    'indovinare' a un modello): confronta gli ultimi due massimi/minimi di
    prezzo con l'RSI negli stessi punti."""
    if len(closes) < lookback:
        lookback = len(closes)

    recent_closes = closes[-lookback:]
    recent_dates = dates[-lookback:]
    rsi_vals = rsi_series(closes, rsi_period)[-lookback:]

    price_highs, price_lows = find_swing_points(recent_closes, window)

    result = {"bearish_divergence": None, "bullish_divergence": None}

    if len(price_highs) >= 2:
        i1, i2 = price_highs[-2], price_highs[-1]
        price_higher_high = recent_closes[i2] > recent_closes[i1]
        rsi_at_1, rsi_at_2 = rsi_vals[i1], rsi_vals[i2]
        if price_higher_high and rsi_at_1 is not None and rsi_at_2 is not None and rsi_at_2 < rsi_at_1:
            result["bearish_divergence"] = (
                f"Il prezzo ha fatto un nuovo massimo più alto ({recent_dates[i1]}: {recent_closes[i1]:.4g} → "
                f"{recent_dates[i2]}: {recent_closes[i2]:.4g}), ma l'RSI nello stesso periodo è sceso "
                f"({rsi_at_1} → {rsi_at_2}): momentum in indebolimento nonostante il nuovo massimo di prezzo."
            )

    if len(price_lows) >= 2:
        i1, i2 = price_lows[-2], price_lows[-1]
        price_lower_low = recent_closes[i2] < recent_closes[i1]
        rsi_at_1, rsi_at_2 = rsi_vals[i1], rsi_vals[i2]
        if price_lower_low and rsi_at_1 is not None and rsi_at_2 is not None and rsi_at_2 > rsi_at_1:
            result["bullish_divergence"] = (
                f"Il prezzo ha fatto un nuovo minimo più basso ({recent_dates[i1]}: {recent_closes[i1]:.4g} → "
                f"{recent_dates[i2]}: {recent_closes[i2]:.4g}), ma l'RSI nello stesso periodo è salito "
                f"({rsi_at_1} → {rsi_at_2}): momentum ribassista in indebolimento nonostante il nuovo minimo di prezzo."
            )

    return result


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

    dates = [c["date"] for c in candles]
    divergence = detect_rsi_divergence(closes, dates, lookback=min(120, len(closes)))

    # un estratto reale (non tutta la serie, per non gonfiare troppo il costo)
    # degli ultimi prezzi, così l'interpretazione qualitativa ha basi concrete
    # invece di ragionare solo su numeri riassuntivi
    sample_points = list(zip(dates[-30:], [round(c, 6) for c in closes[-30:]]))

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
        "rsi_divergence_bearish": divergence["bearish_divergence"],
        "rsi_divergence_bullish": divergence["bullish_divergence"],
        "recent_price_sample": sample_points,
        **support_resistance(closes),
    }
