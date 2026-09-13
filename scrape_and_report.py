"""
Cryptonary Daily Report + Dashboard generator.

Login passwordless via email+OTP, letto automaticamente da Gmail (IMAP) — niente
più refresh token fragili. Legge le pick attive (crypto + stock), l'analisi
tecnica con dati storici reali, e genera/aggiorna docs/index.html.

Variabili d'ambiente richieste:
  CRYPTONARY_EMAIL          - l'email con cui accedi a Cryptonary
  GMAIL_APP_PASSWORD        - App Password Gmail (16 caratteri, generata da
                              myaccount.google.com/apppasswords) — usata sia per
                              leggere il codice OTP (IMAP) sia per inviare avvisi (SMTP)
  ANTHROPIC_API_KEY         - per le chiamate a Claude
  GH_TOKEN                  - personal access token con scope 'repo' per
                              aggiornare i secret e processare le Issue
  GH_REPO                   - "utente/nome-repo"
"""

import os
import json
import re
import sys
import time
import imaplib
import email as email_lib
from datetime import datetime, timezone

from playwright.sync_api import sync_playwright
import anthropic

BASE_URL = "https://cryptonary.com"

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "docs")
DASHBOARD_PATH = os.path.join(OUTPUT_DIR, "index.html")
TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "dashboard_v2_phase1.html")


def log(msg):
    print(f"[{datetime.now().isoformat()}] {msg}", flush=True)


# ============================================================
# LOGIN passwordless: email -> OTP letto da Gmail -> sessione autenticata
# ============================================================

def fetch_otp_from_gmail(sender_hint="cryptonary", timeout_sec=60, poll_every=5):
    """Si collega alla casella Gmail via IMAP e cerca il codice OTP più recente
    inviato da Cryptonary, aspettando che arrivi se necessario. Ritorna la
    stringa del codice (es. '482913') o None se non trovato in tempo."""
    gmail_user = os.environ["CRYPTONARY_EMAIL"]
    gmail_pass = os.environ["GMAIL_APP_PASSWORD"]

    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            imap = imaplib.IMAP4_SSL("imap.gmail.com")
            imap.login(gmail_user, gmail_pass)
            imap.select("INBOX")

            # Cerca le email più recenti dal mittente, non lette o già lette
            # (potremmo dover rileggere lo stesso codice se lo script fallisce e riparte)
            status, data = imap.search(None, f'(FROM "{sender_hint}")')
            if status == "OK" and data[0]:
                ids = data[0].split()
                latest_id = ids[-1]  # l'ultima email trovata
                status, msg_data = imap.fetch(latest_id, "(RFC822)")
                raw_email = msg_data[0][1]
                msg = email_lib.message_from_bytes(raw_email)

                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain":
                            body += part.get_payload(decode=True).decode("utf-8", errors="ignore")
                else:
                    body = msg.get_payload(decode=True).decode("utf-8", errors="ignore")

                # Il codice OTP è tipicamente una sequenza di 4-8 cifre isolata nel testo
                match = re.search(r"\b(\d{4,8})\b", body)
                if match:
                    imap.logout()
                    return match.group(1)

            imap.logout()
        except Exception as e:
            log(f"Errore lettura IMAP (riprovo): {e}")

        time.sleep(poll_every)

    return None


def login_with_otp(page):
    """Esegue il login passwordless completo: inserisce l'email, aspetta il
    passaggio OTP, legge il codice da Gmail, lo inserisce, conferma l'accesso."""
    email_addr = os.environ["CRYPTONARY_EMAIL"]

    page.goto(f"{BASE_URL}/auth", wait_until="networkidle")

    email_input = page.locator('input[type="email"], input[placeholder*="email" i]').first
    email_input.fill(email_addr)

    continue_btn = page.get_by_role("button", name=re.compile("continue with email", re.I)).first
    continue_btn.click()

    log("Email inviata, aspetto il codice OTP nella casella di posta...")
    page.wait_for_timeout(3000)  # margine perché l'email arrivi

    otp_code = fetch_otp_from_gmail(sender_hint="cryptonary")
    if not otp_code:
        raise RuntimeError("Codice OTP non trovato nella casella email entro il tempo limite.")

    log(f"Codice OTP recuperato (lunghezza {len(otp_code)}), lo inserisco...")

    # Il modulo OTP potrebbe avere una singola casella o più caselle per cifra:
    # proviamo prima una singola casella di testo, poi il fallback multi-casella.
    otp_single = page.locator('input[type="text"], input[inputmode="numeric"]').first
    if otp_single.count() > 0:
        try:
            otp_single.fill(otp_code)
        except Exception:
            _fill_otp_boxes(page, otp_code)
    else:
        _fill_otp_boxes(page, otp_code)

    page.wait_for_timeout(1000)
    verify_btn = page.get_by_role("button", name=re.compile("verify|conferma|continue", re.I)).first
    if verify_btn.count() > 0:
        verify_btn.click()

    page.wait_for_url(f"{BASE_URL}/home", timeout=20000)
    log(f"Login riuscito — pagina: {page.title()!r}")


def _fill_otp_boxes(page, otp_code):
    """Fallback per moduli OTP con una casella separata per ogni cifra."""
    boxes = page.locator('input[maxlength="1"]')
    count = boxes.count()
    for i, digit in enumerate(otp_code[:count]):
        boxes.nth(i).fill(digit)


def scrape_picks(page, url, debug_label=""):
    """Estrae le righe della tabella pick (crypto o stock) leggendo il testo
    visibile — robusto a piccoli cambi di stile, fragile a cambi di struttura."""
    page.goto(url, wait_until="networkidle")

    try:
        page.wait_for_function(
            """() => {
                const rows = document.querySelectorAll('table tbody tr');
                if (rows.length === 0) return false;
                return Array.from(rows).some(r => r.innerText.trim().length > 5);
            }""",
            timeout=30000,
        )
    except Exception:
        pass  # procediamo comunque: meglio dati parziali che bloccare tutto

    page.wait_for_timeout(3000)

    rows = page.evaluate(
        """
        () => {
            const rows = Array.from(document.querySelectorAll('table tbody tr'));
            return rows.map(row => {
                const cells = Array.from(row.querySelectorAll('td'));
                return cells.map(c => c.innerText.trim());
            }).filter(r => r.length > 0 && r.some(cell => cell.length > 0));
        }
        """
    )

    if not rows:
        snippet = page.evaluate("() => document.body.innerText.slice(0, 300)")
        log(f"  DEBUG {debug_label}: 0 righe, estratto pagina: {snippet!r}")

    return rows


def scrape_daily_feed_headlines(page, hours=48):
    """Legge i titoli/estratti del feed principale delle ultime ore."""
    page.goto(f"{BASE_URL}/home", wait_until="networkidle")
    page.wait_for_timeout(1500)
    items = page.evaluate(
        """
        () => {
            const cards = Array.from(document.querySelectorAll('article, [class*="feed"] > div'));
            return cards.slice(0, 30).map(c => c.innerText.trim()).filter(Boolean);
        }
        """
    )
    return items


def parse_picks_with_claude(crypto_rows, stock_rows):
    """Trasforma le righe grezze scrapate (testo libero dalle celle) in una lista
    pulita e strutturata {name, ticker, type, entry, target, current, risk, status}.
    Un'unica chiamata economica (nessuna ricerca web necessaria qui)."""
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    prompt = f"""Ricevi righe di tabella grezze (testo estratto da una pagina web) con le pick
crypto e stock di Cryptonary. Estrai per ognuna: nome asset, ticker (simbolo, es. BTC, ETH, SNDK),
tipo (crypto/stock), prezzo di entry, prezzo target (se presente, altrimenti null), prezzo attuale
indicato nella tabella, livello di rischio, stato (Active/Archived — includi solo le Active).

RIGHE CRYPTO:
{json.dumps(crypto_rows, ensure_ascii=False)}

RIGHE STOCK:
{json.dumps(stock_rows, ensure_ascii=False)}

Rispondi SOLO con un array JSON valido, nessun testo extra:
[{{"name": "...", "ticker": "...", "type": "crypto|stock", "entry": numero_o_null, "target": numero_o_null, "current": numero_o_null, "risk": "...", "status": "..."}}]"""

    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )
    text = resp.content[0].text.strip()
    text = re.sub(r"^```json\s*|\s*```$", "", text)
    match = re.search(r"\[.*\]", text, re.S)
    if not match:
        log("ATTENZIONE: nessuna lista di pick strutturata estratta.")
        return []
    try:
        parsed = json.loads(match.group(0))
        return [p for p in parsed if p.get("status", "Active").lower() == "active"]
    except json.JSONDecodeError as e:
        log(f"ATTENZIONE: JSON pick non valido ({e})")
        return []


def call_claude_for_asset_analysis(ticker, name, asset_type, indicators, cryptonary_view, risk_profile):
    """Analisi tecnica + macro completa per un singolo asset, con ricerca web
    reale abilitata (non solo conoscenza congelata) e istruzioni esplicite di
    indipendenza di giudizio."""
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    prompt = f"""Sei un analista tecnico indipendente, molto rigoroso, che scrive per un investitore italiano.

ASSET: {name} ({ticker}), tipo: {asset_type}

DATI TECNICI CALCOLATI (formule matematiche vere, non stime):
{json.dumps({k: v for k, v in indicators.items() if k not in ('closes', 'candles', 'sma50_series', 'sma200_series')}, indent=2, ensure_ascii=False)}

VIEW DI CRYPTONARY SU QUESTO ASSET:
{json.dumps(cryptonary_view, ensure_ascii=False, indent=2)}

PROFILO DI RISCHIO DELL'UTENTE: {risk_profile}

ISTRUZIONI SULLE FONTI (obbligatorio):
- Usa la ricerca web per il contesto macro e le notizie recenti — non affidarti solo alla tua conoscenza pregressa, che potrebbe essere superata dagli eventi
- Dai priorità a fonti primarie e di alta affidabilità: comunicati ufficiali di banche centrali (Fed, BCE), agenzie statistiche governative, agenzie di stampa di prima fascia (Reuters, Associated Press, Bloomberg). Evita blog, aggregatori, contenuti sponsorizzati o fonti anonime
- Se fonti diverse si contraddicono, dillo esplicitamente invece di scegliere quella che conferma una tesi

ISTRUZIONI SULL'INDIPENDENZA DI GIUDIZIO (obbligatorio):
- NON limitarti a ripetere il sentiment dominante di mercato o la narrativa di Cryptonary: valutali criticamente con i tuoi stessi dati
- Se il tuo parere diverge da quello di Cryptonary, dillo chiaramente e spiega perché, con il campo "diverge_da_cryptonary"
- Considera esplicitamente scenario a favore E scenario contrario prima di ogni conclusione
- Il conteggio delle onde di Elliott è intrinsecamente soggettivo: presentalo come "una lettura plausibile", mai come un fatto certo
- Distingui sempre fatti verificati (dati, prezzi, notizie con fonte) da tue interpretazioni
- Usa linguaggio calibrato all'incertezza ("i dati suggeriscono", "uno scenario plausibile è"), mai assoluto
- Per la probabilità richiesta sotto: è una TUA stima soggettiva basata sui dati raccolti, non un calcolo statistico rigoroso — trattala e presentala di conseguenza, senza fare finta che sia più precisa di quanto sia
- Fornisci l'analisi su TRE orizzonti temporali separati (breve, medio, lungo termine) — un trader professionista guarda sempre più timeframe insieme, perché possono divergere (es. "a breve meglio aspettare un pullback, ma sul lungo la tesi strutturale resta solida"). Non forzare le tre letture a essere concordi se i dati suggeriscono altrimenti

Rispondi SOLO con un oggetto JSON valido (nessun markdown, nessun testo fuori dal JSON):
{{
  "macro_context": "2-4 frasi sul contesto macro rilevante per questo asset, con le fonti citate a parole (es. 'secondo l'ultimo comunicato della Fed...')",
  "divergence_from_cryptonary": "stringa vuota se sei d'accordo con la view di Cryptonary, altrimenti spiega la divergenza e perché",
  "my_opinion": "sintesi complessiva che unisce le tre letture temporali, con pro e contro espliciti",
  "timeframes": {{
    "short": {{
      "focus_days": numero_giorni_es_14_30,
      "trend_narrative": "2-3 frasi sul trend a breve termine (giorni/settimane)",
      "pattern_analysis": "pattern/onde di Elliott rilevanti su questo orizzonte, con linguaggio di cautela",
      "recommendation": "buy" o "hold" o "sell",
      "confidence_pct": numero_0_100,
      "suggested_stop_loss": numero_o_null,
      "suggested_target": numero_o_null,
      "target_horizon": "es. '1-3 settimane'"
    }},
    "medium": {{
      "focus_days": numero_giorni_es_60_120,
      "trend_narrative": "...", "pattern_analysis": "...",
      "recommendation": "buy/hold/sell", "confidence_pct": numero_0_100,
      "suggested_stop_loss": numero_o_null, "suggested_target": numero_o_null,
      "target_horizon": "es. '1-3 mesi'"
    }},
    "long": {{
      "focus_days": numero_giorni_es_180_365,
      "trend_narrative": "...", "pattern_analysis": "...",
      "recommendation": "buy/hold/sell", "confidence_pct": numero_0_100,
      "suggested_stop_loss": numero_o_null, "suggested_target": numero_o_null,
      "target_horizon": "es. '6-12 mesi'"
    }}
  }}
}}"""

    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4000,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{"role": "user", "content": prompt}],
    )

    # Con il web search tool la risposta può contenere più blocchi (ricerche +
    # testo): uniamo tutti i blocchi di testo, ignorando le chiamate agli strumenti.
    text_parts = [block.text for block in resp.content if block.type == "text"]
    full_text = "\n".join(text_parts).strip()
    full_text = re.sub(r"^```json\s*|\s*```$", "", full_text)

    # Il JSON potrebbe non essere l'unica cosa nel testo se il modello ha aggiunto
    # commenti: estraiamo il primo blocco { ... } bilanciato.
    match = re.search(r"\{.*\}", full_text, re.S)
    if not match:
        log(f"ATTENZIONE: nessun JSON trovato nell'analisi di {ticker}")
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError as e:
        log(f"ATTENZIONE: JSON non valido nell'analisi di {ticker} ({e})")
        return None


def call_claude_for_analysis(crypto_rows, stock_rows, headlines, portfolio):
    """Riepilogo giornaliero leggero (usato per il ticker/tabella rapida e per
    il riepilogo generale) — non l'analisi approfondita per-asset."""
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    prompt = f"""Sei un analista finanziario che scrive per un investitore italiano.

STANDARD DI QUALITÀ (obbligatorio):
- Non dare per buono un dato senza averlo verificato; se qualcosa è ambiguo, dillo
- Sii critico verso le pick di Cryptonary: hanno un interesse a mostrarle positivamente
- Considera sempre pro E contro prima di un parere
- Linguaggio calibrato all'incertezza, mai assoluto
- Tolleranza al rischio dell'utente: PRUDENTE su stock, MODERATA su crypto

DATI PICK CRYPTO (righe tabella grezze):
{json.dumps(crypto_rows, ensure_ascii=False, indent=2)}

DATI PICK STOCK (righe tabella grezze):
{json.dumps(stock_rows, ensure_ascii=False, indent=2)}

TITOLI RECENTI DAL FEED:
{json.dumps(headlines, ensure_ascii=False, indent=2)}

PORTAFOGLIO REALE DELL'UTENTE:
{json.dumps(portfolio, ensure_ascii=False, indent=2)}

Rispondi SOLO con un oggetto JSON valido (nessun markdown, nessun testo fuori dal JSON) con questa struttura:
{{
  "picks_table_html": "una riga <tr> per ogni pick, con ESATTAMENTE 4 celle <td>, in questo ordine e formato, senza aggiungere altre celle o testo extra: <tr><td>NomeAsset <span class=\\"ticker\\">TICKER</span></td><td>$prezzo</td><td>valoreRSI o —</td><td><span class=\\"tag bull\\">Bullish</span></td></tr> — usa classe 'tag bull' per giudizio positivo, 'tag bear' per negativo, 'tag neutral' per neutro; il testo dentro lo span del giudizio deve essere UNA PAROLA (Bullish/Bearish/Neutrale), non una frase",
  "portfolio_briefing_html": "<p>...</p> un paragrafo per ogni posizione utente + sintesi finale (qui sì, testo completo e argomentato)",
  "updated_prices": {{"TICKER": prezzo_numero, ...}}
}}"""

    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
    )
    text = resp.content[0].text
    text = re.sub(r"^```json\s*|\s*```$", "", text.strip())
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        log(f"ATTENZIONE: risposta di Claude non è JSON valido ({e}). Uso un fallback vuoto.")
        return {
            "picks_table_html": '<tr><td colspan="4">Dati non disponibili in questo aggiornamento.</td></tr>',
            "portfolio_briefing_html": "<p>Analisi non disponibile in questo aggiornamento a causa di un errore tecnico. Riprova al prossimo run.</p>",
            "updated_prices": {},
        }


def load_seed_array(marker_name):
    """Legge un array SEED_* dall'ultima dashboard pubblicata, così i dati
    sopravvivono da un run all'altro anche lato server (nessun DB esterno)."""
    if not os.path.exists(DASHBOARD_PATH):
        return []
    with open(DASHBOARD_PATH, "r", encoding="utf-8") as f:
        content = f.read()
    m = re.search(rf"__{marker_name}__\s*\*/\s*(\[.*?\])\s*/\*\s*__{marker_name}_END__", content, re.S)
    if not m:
        return []
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return []


def load_portfolio_from_previous_dashboard():
    return load_seed_array("SEED_ACTIVE")


def load_closed_from_previous_dashboard():
    return load_seed_array("SEED_CLOSED")


def process_pending_issues(active, closed):
    """Legge le GitHub Issue aperte con etichetta 'portfolio' (create dal bottone
    Aggiungi/Vendi/Elimina sulla dashboard), applica la modifica al portafoglio,
    e le richiude con un commento di confirmazione."""
    gh_token = os.environ.get("GH_TOKEN")
    gh_repo = os.environ.get("GH_REPO")
    if not gh_token or not gh_repo:
        log("GH_TOKEN/GH_REPO non impostati: salto l'elaborazione delle issue.")
        return active, closed

    import requests

    headers = {"Authorization": f"Bearer {gh_token}", "Accept": "application/vnd.github+json"}
    resp = requests.get(
        f"https://api.github.com/repos/{gh_repo}/issues",
        headers=headers,
        params={"state": "open", "labels": "portfolio", "per_page": 100},
    )
    resp.raise_for_status()
    issues = resp.json()
    log(f"Trovate {len(issues)} issue 'portfolio' da elaborare.")

    for issue in issues:
        body = issue.get("body", "") or ""
        match = re.search(r"```json\s*(\{.*?\})\s*```", body, re.S)
        if not match:
            log(f"  Issue #{issue['number']}: nessun blocco JSON valido, la salto.")
            continue
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError:
            log(f"  Issue #{issue['number']}: JSON non valido, la salto.")
            continue

        action = payload.get("action")
        ticker = (payload.get("ticker") or "").upper()
        comment = ""

        if action == "add":
            active.append({
                "name": payload.get("name", ticker),
                "ticker": ticker,
                "type": payload.get("type", "crypto"),
                "qty": payload.get("qty"),
                "entry": payload.get("entry"),
                "current": None,
            })
            comment = f"Posizione {ticker} aggiunta al portafoglio."

        elif action == "sell":
            idx = next((i for i, p in enumerate(active) if p.get("ticker") == ticker), None)
            if idx is not None:
                pos = active.pop(idx)
                pos["sellPrice"] = payload.get("sellPrice")
                pos["sellDate"] = payload.get("sellDate")
                closed.insert(0, pos)
                comment = f"Posizione {ticker} venduta e archiviata."
            else:
                comment = f"Non ho trovato una posizione attiva {ticker} da vendere."

        elif action == "remove":
            idx = next((i for i, p in enumerate(active) if p.get("ticker") == ticker), None)
            if idx is not None:
                active.pop(idx)
                comment = f"Posizione {ticker} rimossa."
            else:
                comment = f"Non ho trovato una posizione attiva {ticker} da rimuovere."
        else:
            comment = f"Azione '{action}' non riconosciuta, nessuna modifica applicata."

        requests.post(
            f"https://api.github.com/repos/{gh_repo}/issues/{issue['number']}/comments",
            headers=headers, json={"body": comment},
        )
        requests.patch(
            f"https://api.github.com/repos/{gh_repo}/issues/{issue['number']}",
            headers=headers, json={"state": "closed"},
        )
        log(f"  Issue #{issue['number']} elaborata: {comment}")

    return active, closed


def render_dashboard(picks_data, charts_data, deep_updated, portfolio_advice_html, portfolio, closed):
    """Genera index.html a partire dal template a schede, sostituendo tutti i
    segnaposto con i dati veri di questa esecuzione."""
    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        html = f.read()

    now_str = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    html = html.replace(
        '<!-- LAST_UPDATED -->—',
        f'<!-- LAST_UPDATED -->{now_str}',
    )

    html = re.sub(
        r"<!-- DASHBOARD_ADVICE_START -->.*?<!-- DASHBOARD_ADVICE_END -->",
        f'<!-- DASHBOARD_ADVICE_START -->\n{portfolio_advice_html}\n<!-- DASHBOARD_ADVICE_END -->',
        html, flags=re.S,
    )

    for p in portfolio:
        ticker = p.get("ticker", "").upper()
        match = next((pd for pd in picks_data if pd.get("ticker", "").upper() == ticker), None)
        if match:
            p["current"] = match.get("price")

    def replace_js_array(html, marker, value):
        pattern = rf"__{marker}__\s*\*/\s*\[.*?\]\s*/\*\s*__{marker}_END__"
        replacement = f"__{marker}__ */ {json.dumps(value, ensure_ascii=False)} /* __{marker}_END__"
        new_html, n = re.subn(pattern, replacement, html, flags=re.S)
        if n == 0:
            log(f"ATTENZIONE: segnaposto __{marker}__ non trovato nel template!")
        return new_html

    html = replace_js_array(html, "SEED_ACTIVE", portfolio)
    html = replace_js_array(html, "SEED_CLOSED", closed)
    html = replace_js_array(html, "PICKS_DATA", picks_data)
    html = replace_js_array(html, "CHARTS_DATA", charts_data)

    html = re.sub(
        r'__DEEP_UPDATED__\s*\*/\s*"[^"]*"\s*/\*\s*__DEEP_UPDATED_END__',
        f'__DEEP_UPDATED__ */ "{deep_updated}" /* __DEEP_UPDATED_END__',
        html,
    )

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(DASHBOARD_PATH, "w", encoding="utf-8") as f:
        f.write(html)
    log(f"Dashboard scritta in {DASHBOARD_PATH}")


def build_portfolio_advice_html(portfolio, picks_data, charts_data):
    """Costruisce il parere 'Cosa farei io al posto tuo' per il portafoglio reale,
    riusando l'analisi già fatta per ogni asset quando disponibile."""
    if not portfolio:
        return '<div class="advice-card"><div class="advice-body"><p>Aggiungi almeno una posizione per ricevere un parere personalizzato.</p></div></div>'

    cards = []
    for p in portfolio:
        ticker = p.get("ticker", "").upper()
        chart_match = next((c for c in charts_data if c.get("ticker", "").upper() == ticker), None)
        if chart_match and chart_match.get("analysis"):
            tf = chart_match["analysis"].get("timeframes", {}).get("medium", {})
            text = tf.get("trend_narrative", "") + " " + chart_match["analysis"].get("my_opinion", "")
        else:
            text = "Analisi dettagliata non ancora disponibile per questo asset (verrà generata al prossimo aggiornamento approfondito)."
        cards.append(
            f'<div class="advice-card"><div class="advice-head"><span class="name">{p.get("name")} '
            f'({ticker})</span></div><div class="advice-body"><p>{text}</p></div></div>'
        )
    return "\n".join(cards)


def compute_chg_pct(candles):
    """Variazione percentuale tra l'ultima chiusura e quella precedente
    (usata per la striscia prezzi in alto)."""
    if not candles or len(candles) < 2:
        return 0
    prev_close = candles[-2]["c"]
    last_close = candles[-1]["c"]
    if not prev_close:
        return 0
    return round((last_close - prev_close) / prev_close * 100, 2)


def main():
    import indicators

    utc_hour = datetime.now(timezone.utc).hour
    deep_mode = utc_hour in (7, 19)
    log(f"Ora UTC: {utc_hour} — modalità: {'APPROFONDITA' if deep_mode else 'rapida'}")

    portfolio = load_portfolio_from_previous_dashboard()
    closed = load_closed_from_previous_dashboard()

    log("Elaborazione issue 'portfolio' in sospeso...")
    portfolio, closed = process_pending_issues(portfolio, closed)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        log("Login (email + OTP da Gmail)...")
        login_with_otp(page)

        log("Lettura pick crypto...")
        crypto_rows = scrape_picks(page, f"{BASE_URL}/tools/assets-picks", "crypto")
        if not crypto_rows:
            log("  0 righe, riprovo una volta...")
            page.wait_for_timeout(3000)
            crypto_rows = scrape_picks(page, f"{BASE_URL}/tools/assets-picks", "crypto-retry")
        log(f"  {len(crypto_rows)} righe trovate")

        log("Lettura pick stock...")
        stock_rows = scrape_picks(page, f"{BASE_URL}/tools/stock-picks", "stock")
        if not stock_rows:
            log("  0 righe, riprovo una volta...")
            page.wait_for_timeout(3000)
            stock_rows = scrape_picks(page, f"{BASE_URL}/tools/stock-picks", "stock-retry")
        log(f"  {len(stock_rows)} righe trovate")

        browser.close()

    log("Estrazione struttura pick (Claude)...")
    structured_picks = parse_picks_with_claude(crypto_rows, stock_rows)
    log(f"  {len(structured_picks)} pick attive strutturate")

    # ---- Sempre: aggiorna prezzi per il ticker in alto ----
    picks_data = [
        {"name": p["name"], "ticker": p["ticker"], "price": p.get("current") or 0, "chg": 0}
        for p in structured_picks
    ]

    charts_data = []
    deep_updated = load_previous_deep_updated()

    if deep_mode:
        log("Modalità approfondita: calcolo indicatori + analisi per ogni asset...")
        risk_profile = "PRUDENTE sulle stock picks, MODERATA sulle crypto picks"

        for p in structured_picks:
            log(f"  Analisi {p['ticker']}...")
            ind_data = indicators.analyze_asset(p["ticker"], p["type"], p["name"])

            if not ind_data.get("sufficient_data"):
                charts_data.append({
                    "name": p["name"], "ticker": p["ticker"], "currentPrice": p.get("current"),
                    "sufficientData": False,
                })
                continue

            claude_analysis = call_claude_for_asset_analysis(
                p["ticker"], p["name"], p["type"], ind_data, p, risk_profile
            )

            charts_data.append({
                "name": p["name"], "ticker": p["ticker"],
                "currentPrice": ind_data["current_price"],
                "sufficientData": True,
                "dates": [c["date"] for c in ind_data["candles"]],
                "candles": [{"o": c["o"], "h": c["h"], "l": c["l"], "c": c["c"]} for c in ind_data["candles"]],
                "sma50Series": ind_data["sma50_series"],
                "sma200Series": ind_data["sma200_series"],
                "rsiDaily": ind_data["rsi_daily"],
                "rsiWeekly": ind_data["rsi_weekly"],
                "rsiMonthly": ind_data["rsi_monthly"],
                "supportRecent": ind_data["support_recent"],
                "resistanceRecent": ind_data["resistance_recent"],
                "analysis": claude_analysis,
            })

            # aggiorna anche il prezzo e la variazione % nel ticker
            for pd in picks_data:
                if pd["ticker"] == p["ticker"]:
                    pd["price"] = ind_data["current_price"]
                    pd["chg"] = compute_chg_pct(ind_data["candles"])

        deep_updated = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    else:
        log("Modalità rapida: salto l'analisi approfondita, riuso l'ultima disponibile.")
        charts_data = load_previous_charts_data()
        # aggiorna comunque i prezzi noti nella tabella pick grezza, se presenti,
        # e recupera la variazione % dalle candele già calcolate nell'ultimo run approfondito
        for pd in picks_data:
            match = next((p for p in structured_picks if p["ticker"] == pd["ticker"]), None)
            if match and match.get("current"):
                pd["price"] = match["current"]
            chart_match = next((c for c in charts_data if c.get("ticker") == pd["ticker"]), None)
            if chart_match and chart_match.get("candles"):
                pd["chg"] = compute_chg_pct(chart_match["candles"])

    portfolio_advice_html = build_portfolio_advice_html(portfolio, picks_data, charts_data)

    log("Generazione dashboard...")
    render_dashboard(picks_data, charts_data, deep_updated, portfolio_advice_html, portfolio, closed)

    log("Fatto.")


def load_previous_deep_updated():
    if not os.path.exists(DASHBOARD_PATH):
        return "—"
    with open(DASHBOARD_PATH, "r", encoding="utf-8") as f:
        content = f.read()
    m = re.search(r'__DEEP_UPDATED__\s*\*/\s*"([^"]*)"', content)
    return m.group(1) if m else "—"


def load_previous_charts_data():
    return load_seed_array("CHARTS_DATA")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"ERRORE: {e}")
        sys.exit(1)
