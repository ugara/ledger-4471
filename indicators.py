"""
Motore di analisi tecnica: recupera storico prezzi reali (CoinGecko per crypto,
Stooq per stock) e calcola indicatori con formule vere — RSI, SMA, EMA,
supporti/resistenze — invece di farli "indovinare" a un modello da uno screenshot.

Nessuna chiave API richiesta per nessuna delle due fonti dati.
"""

import requests
import statistics
import os
import json
from datetime import datetime, timezone, timedelta


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


def fetch_crypto_history_coinbase(ticker, days=365, start_date=None):
    """Coinbase Exchange (dati pubblici, nessuna chiave richiesta): candele
    giornaliere VERE. A differenza di Binance, Coinbase è un exchange
    regolamentato negli USA — non blocca il traffico dai datacenter
    statunitensi (dove girano i runner di GitHub Actions), quindi funziona
    dove Binance no (verificato: Binance dava errore 451 'restricted location').
    Copre bene gli asset principali, non i token più piccoli/nuovi.
    Se 'start_date' (stringa YYYY-MM-DD) è specificato, recupera SOLO le
    candele da quella data in poi (per gli aggiornamenti incrementali) —
    Coinbase limita comunque a 300 candele per richiesta, ma per un
    aggiornamento giornaliero il divario è sempre piccolo."""
    try:
        params = {"granularity": 86400}
        if start_date:
            params["start"] = f"{start_date}T00:00:00Z"
            params["end"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        resp = requests.get(
            f"https://api.exchange.coinbase.com/products/{ticker.upper()}-USD/candles",
            params=params,
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
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def fetch_crypto_history(ticker, name_hint="", days=365, start_date=None):
    """Ritorna candele OHLC reali. Prova Coinbase (candele giornaliere vere,
    exchange USA non soggetto al blocco geografico di Binance sui datacenter
    statunitensi); se il ticker non è listato lì, ripiega su CoinGecko
    (candele più larghe oltre i 30 giorni — limite della loro API gratuita).
    'start_date' (YYYY-MM-DD): se specificato, richiede solo le candele da
    quella data in poi (aggiornamento incrementale)."""
    coinbase_data = fetch_crypto_history_coinbase(ticker, days, start_date=start_date)
    if coinbase_data:
        log(f"{ticker}: storico giornaliero reale da Coinbase ({len(coinbase_data)} candele nuove)")
        return coinbase_data

    log(f"{ticker}: non trovato su Coinbase, ripiego su CoinGecko (candele più larghe)")
    coin_id = find_coingecko_id(ticker, name_hint)
    if not coin_id:
        log(f"Nessun id CoinGecko trovato per {ticker}")
        return []
    try:
        # CoinGecko non supporta un intervallo di date arbitrario sul piano
        # gratuito (solo 'days' predefiniti) — per un aggiornamento
        # incrementale chiediamo comunque una finestra ampia (90 giorni) che
        # coprirà sempre il divario di un aggiornamento manuale, il merge poi
        # scarta i duplicati già presenti in cache
        request_days = 90 if start_date else days
        resp = requests.get(
            f"https://api.coingecko.com/api/v3/coins/{coin_id}/ohlc",
            params={"vs_currency": "usd", "days": request_days},
            timeout=20,
        )
        resp.raise_for_status()
        candles = resp.json()  # [[timestamp, open, high, low, close], ...]
        return [(ts_to_date_str(int(c[0])), float(c[1]), float(c[2]), float(c[3]), float(c[4])) for c in candles]
    except Exception as e:
        log(f"Errore recupero OHLC CoinGecko per {ticker} ({coin_id}): {e}")
        return []


def fetch_stock_history(ticker, days=365, start_date=None):
    """Ritorna candele OHLC reali da Yahoo Finance (endpoint pubblico non
    ufficiale ma ampiamente usato, nessuna chiave richiesta). Sostituisce Stooq,
    che da marzo 2026 richiede una API key a richiesta (non più gratis diretto).
    'start_date' (YYYY-MM-DD): se specificato, usa period1/period2 per
    richiedere solo le candele da quella data in poi."""
    try:
        params = {"interval": "1d"}
        if start_date:
            start_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            params["period1"] = int(start_dt.timestamp())
            params["period2"] = int(datetime.now(timezone.utc).timestamp())
        else:
            params["range"] = "1y" if days > 180 else ("6mo" if days > 90 else "3mo")
        resp = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker.upper()}",
            params=params,
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
# CACHE STORICO SU DISCO — aggiornamento incrementale
# ============================================================
# Invece di riscaricare fino a un anno di candele a ogni esecuzione, teniamo
# uno storico che cresce nel tempo: al primo avvio per un asset scarichiamo il
# massimo disponibile, alle esecuzioni successive scarichiamo SOLO i giorni
# mancanti dall'ultimo salvataggio e li aggiungiamo. Il file cresce di giorno
# in giorno invece di essere ricostruito da zero — utile in particolare per il
# lungo termine (RSI/divergenze mensili), che con un solo anno di dati non ha
# abbastanza punti.

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")


def load_cached_history(ticker):
    """Legge lo storico salvato per un ticker, se esiste. Ritorna una lista
    di tuple (data, o, h, l, c) in ordine cronologico, o [] se non c'è cache."""
    path = os.path.join(DATA_DIR, f"{ticker.upper()}.json")
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return [tuple(row) for row in raw]
    except (json.JSONDecodeError, OSError) as e:
        log(f"Cache corrotta per {ticker}, la ignoro e riparto da zero: {e}")
        return []


def save_cached_history(ticker, history):
    """Salva lo storico (lista di tuple) su disco, in ordine cronologico."""
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, f"{ticker.upper()}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(history, f)


def merge_history(cached, fresh):
    """Unisce lo storico in cache con le candele appena scaricate, usando la
    data come chiave — se una data compare in entrambi, vince quella nuova
    (potrebbe essere una correzione, es. la candela di 'oggi' che si aggiorna
    durante la giornata). Ritorna la lista unita, ordinata cronologicamente."""
    by_date = {row[0]: row for row in cached}
    for row in fresh:
        by_date[row[0]] = row
    return [by_date[d] for d in sorted(by_date.keys())]


def get_history_incremental(ticker, asset_type, name_hint=""):
    """Punto d'ingresso principale per lo storico prezzi: usa la cache su
    disco e scarica solo i giorni mancanti, invece di riscaricare sempre
    tutto. Il primo avvio per un asset fa comunque un download pieno
    (nessuna cache ancora presente)."""
    cached = load_cached_history(ticker)

    if not cached:
        log(f"{ticker}: nessuna cache trovata, primo download completo...")
        fresh = (
            fetch_crypto_history(ticker, name_hint)
            if asset_type == "crypto"
            else fetch_stock_history(ticker)
        )
        if fresh:
            save_cached_history(ticker, fresh)
        return fresh

    last_cached_date = cached[-1][0]
    log(f"{ticker}: cache trovata ({len(cached)} candele, fino al {last_cached_date}) — scarico solo i giorni mancanti...")
    fresh = (
        fetch_crypto_history(ticker, name_hint, start_date=last_cached_date)
        if asset_type == "crypto"
        else fetch_stock_history(ticker, start_date=last_cached_date)
    )
    merged = merge_history(cached, fresh) if fresh else cached
    if len(merged) != len(cached):
        log(f"{ticker}: aggiunte {len(merged) - len(cached)} candele nuove, totale {len(merged)}.")
        save_cached_history(ticker, merged)
    else:
        log(f"{ticker}: nessuna candela nuova da aggiungere.")
    return merged


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


def support_resistance_window(closes, lookback):
    """Supporto/resistenza calcolati su una finestra specifica — va chiamata
    con lookback diversi per orizzonti diversi (non riusare lo stesso valore
    per breve, medio e lungo termine, altrimenti non sono più indicatori
    specifici per quell'orizzonte)."""
    window = closes[-lookback:] if len(closes) >= lookback else closes
    return {
        "support": round(min(window), 6),
        "resistance": round(max(window), 6),
        "lookback_days_used": len(window),
    }


def trend_short(closes):
    """Trend di brevissimo termine: SMA10 vs SMA30 sui dati giornalieri
    (finestra di poche settimane, coerente con un orizzonte 'breve termine')."""
    s10 = sma(closes, 10)
    s30 = sma(closes, 30)
    if s10 is None or s30 is None:
        return "indeterminato (storico insufficiente)"
    if s10 > s30 * 1.01:
        return "rialzista a breve (SMA10 sopra SMA30)"
    if s10 < s30 * 0.99:
        return "ribassista a breve (SMA10 sotto SMA30)"
    return "laterale a breve (SMA10 e SMA30 vicine)"


def trend_medium(closes):
    """Trend di medio termine: SMA50 vs SMA200 sui dati giornalieri (finestra
    di alcuni mesi — è la lettura 'classica' di trend primario)."""
    s50 = sma(closes, 50)
    s200 = sma(closes, 200)
    if s50 is None or s200 is None:
        return "indeterminato (storico insufficiente per SMA200)"
    if s50 > s200 * 1.02:
        return "rialzista (SMA50 sopra SMA200)"
    if s50 < s200 * 0.98:
        return "ribassista (SMA50 sotto SMA200)"
    return "laterale/incerto (SMA50 e SMA200 vicine)"


def trend_long(monthly_closes):
    """Trend di lungo termine: calcolato sui dati RESAMPLED MENSILI, non su
    quelli giornalieri — altrimenti 'lungo termine' userebbe la stessa identica
    finestra del medio termine. Con pochi mesi di storico (la cache è ancora
    giovane) usa un confronto punto-a-punto più permissivo invece di richiedere
    SMA lunghe che richiederebbero anni di dati per essere calcolabili."""
    if len(monthly_closes) < 3:
        return "indeterminato (storico mensile ancora troppo corto)"
    if len(monthly_closes) >= 9:
        s3 = sma(monthly_closes, 3)
        s9 = sma(monthly_closes, 9)
        if s3 is not None and s9 is not None:
            if s3 > s9 * 1.03:
                return "rialzista sul lungo periodo (media 3 mesi sopra media 9 mesi)"
            if s3 < s9 * 0.97:
                return "ribassista sul lungo periodo (media 3 mesi sotto media 9 mesi)"
            return "laterale/incerto sul lungo periodo (medie 3 e 9 mesi vicine)"
    # storico mensile ancora troppo corto per SMA9: confronto diretto primo vs ultimo
    change_pct = (monthly_closes[-1] / monthly_closes[0] - 1) * 100
    direzione = "rialzista" if change_pct > 5 else "ribassista" if change_pct < -5 else "laterale"
    return f"{direzione} sul periodo disponibile ({len(monthly_closes)} mesi, {change_pct:+.1f}%) — storico ancora corto per una lettura più solida"


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
    history = get_history_incremental(ticker, asset_type, name_hint)

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
    weekly_dates = [c["date"] for c in weekly_candles]
    monthly_dates = [c["date"] for c in monthly_candles]

    # La divergenza RSI va calcolata SEPARATAMENTE per ogni orizzonte — una
    # divergenza vista sui dati giornalieri non è la stessa cosa di una vista
    # sui dati settimanali o mensili, e mostrarle come se fossero la stessa
    # cosa su tutti e tre gli orizzonti (come succedeva prima) è fuorviante.
    # Il mensile richiede uno storico lungo per avere abbastanza punti
    # (swing point rilevabili): con lo storico attuale (~250 giorni ≈ 8-9
    # candele mensili) è quasi sempre insufficiente — la funzione lo segnala
    # da sola tramite 'insufficient_data' invece di inventare un risultato.
    divergence_daily = detect_rsi_divergence(closes, dates, lookback=min(120, len(closes)))
    divergence_weekly = (
        detect_rsi_divergence(weekly_closes, weekly_dates, lookback=len(weekly_closes))
        if len(weekly_closes) >= 15 else {"bearish_divergence": False, "bullish_divergence": False, "insufficient_data": True}
    )
    divergence_monthly = (
        detect_rsi_divergence(monthly_closes, monthly_dates, lookback=len(monthly_closes))
        if len(monthly_closes) >= 15 else {"bearish_divergence": False, "bullish_divergence": False, "insufficient_data": True}
    )

    # un estratto reale (non tutta la serie, per non gonfiare troppo il costo)
    # degli ultimi prezzi, così l'interpretazione qualitativa ha basi concrete
    # invece di ragionare solo su numeri riassuntivi — SEPARATO per orizzonte:
    # il breve termine deve vedere prezzi giornalieri recenti, ma il medio e
    # il lungo termine devono vedere un campione che copra DAVVERO quella
    # scala di tempo (settimane/mesi), non lo stesso mese di dati giornalieri
    # riciclato per tutti e tre — altrimenti l'analisi di 6-12 mesi verrebbe
    # scritta senza aver mai visto un solo dato che copra 6-12 mesi.
    sample_short = list(zip(dates[-30:], [round(c, 6) for c in closes[-30:]]))
    sample_medium = list(zip(weekly_dates[-20:], [round(c, 6) for c in weekly_closes[-20:]]))
    sample_long = list(zip(monthly_dates[-18:], [round(c, 6) for c in monthly_closes[-18:]]))

    # Supporto/resistenza, trend: calcolati SEPARATAMENTE per ogni orizzonte,
    # su finestre e serie diverse — non un'unica finestra di 90 giorni
    # riusata per breve, medio e lungo termine (era il problema principale).
    sr_short = support_resistance_window(closes, lookback=30)
    sr_medium = support_resistance_window(closes, lookback=180)
    sr_long = support_resistance_window(closes, lookback=len(closes))  # tutto lo storico disponibile

    timeframe_data = {
        "short": {
            "trend": trend_short(closes),
            "rsi": rsi(closes, 14),
            "support": sr_short["support"], "resistance": sr_short["resistance"],
            "support_resistance_window_days": sr_short["lookback_days_used"],
            "price_sample": sample_short,
            "price_sample_granularity": "giornaliera",
            "rsi_divergence_bearish": divergence_daily["bearish_divergence"],
            "rsi_divergence_bullish": divergence_daily["bullish_divergence"],
        },
        "medium": {
            "trend": trend_medium(closes),
            "rsi": rsi(weekly_closes, 14) if len(weekly_closes) >= 15 else None,
            "support": sr_medium["support"], "resistance": sr_medium["resistance"],
            "support_resistance_window_days": sr_medium["lookback_days_used"],
            "price_sample": sample_medium,
            "price_sample_granularity": "settimanale",
            "rsi_divergence_bearish": divergence_weekly["bearish_divergence"],
            "rsi_divergence_bullish": divergence_weekly["bullish_divergence"],
        },
        "long": {
            "trend": trend_long(monthly_closes),
            "rsi": rsi(monthly_closes, 14) if len(monthly_closes) >= 15 else None,
            "support": sr_long["support"], "resistance": sr_long["resistance"],
            "support_resistance_window_days": sr_long["lookback_days_used"],
            "price_sample": sample_long,
            "price_sample_granularity": "mensile",
            "rsi_divergence_bearish": divergence_monthly["bearish_divergence"],
            "rsi_divergence_bullish": divergence_monthly["bullish_divergence"],
            "rsi_divergence_unavailable": divergence_monthly.get("insufficient_data", False),
        },
    }

    # Con la cache che cresce nel tempo, lo storico può diventare molto lungo
    # (anni di dati) — usiamo tutto per calcolare gli indicatori (SMA200,
    # RSI mensile e simili beneficiano di più storico), ma per quello che va
    # nel grafico e viene incorporato nell'HTML limitiamo a una finestra
    # recente, altrimenti il file cresce senza fine giorno dopo giorno.
    DISPLAY_WINDOW = 500  # circa un anno e mezzo di candele giornaliere
    sma50_full = sma_series(closes, 50)
    sma200_full = sma_series(closes, 200)

    return {
        "ticker": ticker,
        "sufficient_data": True,
        "closes": closes,
        "candles": candles[-DISPLAY_WINDOW:],
        "current_price": closes[-1],
        "sma50": sma(closes, 50),
        "sma200": sma(closes, 200),
        "sma50_series": sma50_full[-DISPLAY_WINDOW:],
        "sma200_series": sma200_full[-DISPLAY_WINDOW:],
        "ema12": ema(closes, 12),
        "ema26": ema(closes, 26),
        # Manteniamo anche questi tre a livello "piatto" per compatibilità con
        # la visualizzazione RSI sul grafico (badge RSI 14 in alto alla card),
        # che mostra un solo numero per volta in base alla scheda selezionata
        # — i dati che guidano l'ANALISI SCRITTA sono invece quelli dentro
        # "timeframes", genuinamente separati per orizzonte.
        "rsi_daily": timeframe_data["short"]["rsi"],
        "rsi_weekly": timeframe_data["medium"]["rsi"],
        "rsi_monthly": timeframe_data["long"]["rsi"],
        "rsi_divergence_bearish_short": timeframe_data["short"]["rsi_divergence_bearish"],
        "rsi_divergence_bullish_short": timeframe_data["short"]["rsi_divergence_bullish"],
        "rsi_divergence_bearish_medium": timeframe_data["medium"]["rsi_divergence_bearish"],
        "rsi_divergence_bullish_medium": timeframe_data["medium"]["rsi_divergence_bullish"],
        "rsi_divergence_bearish_long": timeframe_data["long"]["rsi_divergence_bearish"],
        "rsi_divergence_bullish_long": timeframe_data["long"]["rsi_divergence_bullish"],
        "rsi_divergence_long_unavailable": timeframe_data["long"]["rsi_divergence_unavailable"],
        "timeframes": timeframe_data,
        "total_history_days": len(closes),
        "total_history_months": len(monthly_closes),
        # support_recent/resistance_recent: usati per la riga "Supporto /
        # Resistenza" mostrata in fondo alla card — riflette l'orizzonte
        # "medio" (180 giorni), il più rappresentativo per una lettura
        # generale a colpo d'occhio
        "support_recent": sr_medium["support"],
        "resistance_recent": sr_medium["resistance"],
    }
