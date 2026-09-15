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
from datetime import datetime, timezone, timedelta

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

def get_latest_email_uid(sender_hint="cryptonary"):
    """Ritorna l'UID dell'email più recente già presente in casella (prima di
    richiedere un nuovo codice), per poter poi riconoscere con certezza quale
    email è quella NUOVA e non riprendere per sbaglio un vecchio codice OTP."""
    gmail_user = os.environ["CRYPTONARY_EMAIL"]
    gmail_pass = os.environ["GMAIL_APP_PASSWORD"]
    try:
        imap = imaplib.IMAP4_SSL("imap.gmail.com")
        imap.login(gmail_user, gmail_pass)
        imap.select("INBOX")
        status, data = imap.search(None, f'(FROM "{sender_hint}")')
        imap.logout()
        if status == "OK" and data[0]:
            return int(data[0].split()[-1])
    except Exception as e:
        log(f"Impossibile leggere lo stato iniziale della casella: {e}")
    return 0


def fetch_otp_from_gmail(sender_hint="cryptonary", timeout_sec=60, poll_every=5, after_id=0):
    """Si collega alla casella Gmail via IMAP e cerca il codice OTP più recente
    inviato da Cryptonary, aspettando che arrivi se necessario. Se 'after_id' è
    specificato, ignora email con id pari o inferiore (evita di riprendere per
    sbaglio un codice vecchio già presente in casella). Ritorna la stringa del
    codice (es. '482913') o None se non trovato in tempo."""
    gmail_user = os.environ["CRYPTONARY_EMAIL"]
    gmail_pass = os.environ["GMAIL_APP_PASSWORD"]

    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            imap = imaplib.IMAP4_SSL("imap.gmail.com")
            imap.login(gmail_user, gmail_pass)
            imap.select("INBOX")

            status, data = imap.search(None, f'(FROM "{sender_hint}")')
            if status == "OK" and data[0]:
                ids = [int(i) for i in data[0].split()]
                new_ids = [i for i in ids if i > after_id]
                if new_ids:
                    latest_id = str(max(new_ids)).encode()
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

                    match = re.search(r"\b(\d{4,8})\b", body)
                    if match:
                        imap.logout()
                        return match.group(1)
                else:
                    log(f"  Nessuna email NUOVA ancora (trovate solo id <= {after_id}, aspetto...)")

            imap.logout()
        except Exception as e:
            log(f"Errore lettura IMAP (riprovo): {e}")

        time.sleep(poll_every)

    return None


def login_with_otp(page):
    """Esegue il login passwordless completo: inserisce l'email, aspetta il
    passaggio OTP, legge il codice da Gmail, lo inserisce, conferma l'accesso."""
    email_addr = os.environ["CRYPTONARY_EMAIL"]

    baseline_id = get_latest_email_uid(sender_hint="cryptonary")
    log(f"Stato casella prima della richiesta OTP: ultima email esistente id={baseline_id}")

    page.goto(f"{BASE_URL}/auth", wait_until="networkidle")

    email_input = page.locator('input[type="email"], input[placeholder*="email" i]').first
    email_input.fill(email_addr)

    continue_btn = page.get_by_role("button", name=re.compile("continue with email", re.I)).first
    continue_btn.click()

    log("Email inviata, aspetto il codice OTP nella casella di posta...")
    page.wait_for_timeout(3000)  # margine perché l'email arrivi

    otp_code = fetch_otp_from_gmail(sender_hint="cryptonary", after_id=baseline_id)
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

    try:
        page.wait_for_url(f"{BASE_URL}/home", timeout=20000)
    except Exception as e:
        # Diagnostica: se non arriva alla dashboard, capiamo perché prima di
        # arrenderci — pagina attuale, eventuali messaggi di errore visibili.
        log(f"DEBUG login fallito — url attuale: {page.url}, titolo: {page.title()!r}")
        page_text = page.evaluate("() => document.body.innerText.slice(0, 500)")
        log(f"DEBUG contenuto pagina al momento del timeout: {page_text!r}")
        raise
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
    """Legge i titoli/estratti del feed principale delle ultime ore. Selettore
    verificato navigando dal vivo il sito (classe che corrisponde esattamente
    al numero di post mostrati in "Feed N")."""
    page.goto(f"{BASE_URL}/home", wait_until="networkidle")
    page.wait_for_timeout(1500)
    items = page.evaluate(
        """
        () => {
            const cards = Array.from(document.querySelectorAll('.first\\\\:pt-0'));
            return cards.map(c => c.innerText.replace(/\\n/g, ' | ').trim()).filter(Boolean);
        }
        """
    )
    return items


def scrape_community_highlights(page):
    """Legge i messaggi recenti visibili nella sezione Community (canali di
    chat). Ogni riga canale è un tag <a> con questa classe specifica —
    verificato navigando dal vivo il sito."""
    try:
        page.goto(f"{BASE_URL}/home", wait_until="networkidle")
        page.wait_for_timeout(1000)
        community_tab = page.get_by_text("Community", exact=True).first
        community_tab.click()
        page.wait_for_timeout(2000)
    except Exception as e:
        log(f"Impossibile aprire la tab Community: {e}")
        return []

    items = page.evaluate(
        """
        () => {
            const rows = Array.from(document.querySelectorAll('a.flex.min-w-0.flex-1'));
            return rows.map(r => r.innerText.replace(/\\n/g, ' | ').trim()).filter(Boolean);
        }
        """
    )
    return items


def scrape_airdrops(page):
    """Legge la shortlist curata di airdrop di Cryptonary (già selezionati da
    loro come i più interessanti, non tutti i 30+ presenti in generale)."""
    try:
        page.goto(f"{BASE_URL}/airdrops/shortlist", wait_until="networkidle")
        page.wait_for_timeout(2000)
    except Exception as e:
        log(f"Impossibile aprire la pagina Airdrops: {e}")
        return ""

    rows = page.evaluate(
        """
        () => {
            const rows = Array.from(document.querySelectorAll('table tbody tr'));
            return rows.map(row => Array.from(row.querySelectorAll('td'))
                .map(td => td.innerText.trim()).filter(Boolean).join(' | '));
        }
        """
    )
    return "\n".join(rows)


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


def fetch_shared_macro_context():
    """Una singola ricerca web sul contesto macroeconomico generale, condivisa
    da TUTTE le pick nello stesso run — invece di farla ricercare da capo 11
    volte (una per asset), sprecando ricerche web quasi identiche e denaro."""
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    prompt = """Cerca sul web le notizie più recenti e rilevanti sul contesto macroeconomico
generale che potrebbe impattare sia i mercati crypto che quelli azionari: politica monetaria
di Fed/BCE, inflazione, dati economici recenti, eventi geopolitici rilevanti, sentiment
generale dei mercati.

ISTRUZIONI SULLE FONTI (obbligatorio):
- Dai priorità a fonti primarie e di alta affidabilità: comunicati ufficiali di banche centrali,
  agenzie statistiche governative, agenzie di stampa di prima fascia (Reuters, AP, Bloomberg)
- Evita blog, aggregatori, contenuti sponsorizzati
- Se fonti diverse si contraddicono, dillo esplicitamente

Scrivi una sintesi di 4-6 frasi in italiano, con linguaggio calibrato all'incertezza,
citando le fonti a parole (es. "secondo l'ultimo comunicato della Fed..."). Rispondi
SOLO con la sintesi in prosa, nessun JSON, nessun markdown."""

    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=800,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{"role": "user", "content": prompt}],
    )
    text_parts = [block.text for block in resp.content if block.type == "text"]
    return "\n".join(text_parts).strip()


def call_claude_for_asset_analysis(ticker, name, asset_type, indicators, cryptonary_view, risk_profile, shared_macro="", asset_history=None):
    """Analisi tecnica + macro completa per un singolo asset, con ricerca web
    reale abilitata (non solo conoscenza congelata), istruzioni esplicite di
    indipendenza di giudizio, e memoria delle analisi dei giorni precedenti
    per lo stesso asset (per un'analisi che si evolve nel tempo invece di
    ripartire sempre da zero)."""
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    asset_history = asset_history or []

    history_block = ""
    if asset_history:
        history_block = f"""
STORICO DELLE TUE ANALISI PRECEDENTI SU QUESTO ASSET (dalla più vecchia alla più recente):
{json.dumps(asset_history, ensure_ascii=False, indent=2)}

ISTRUZIONI SULLO STORICO (obbligatorio):
- Confronta la view attuale con quella dei giorni precedenti: la tesi si è rafforzata, indebolita, o è cambiata?
- Se avevi indicato uno scenario/target che si è nel frattempo avverato o smentito dal prezzo attuale, dillo esplicitamente
- Usa lo storico per un'analisi più approfondita e continuativa, non ripartire da zero come se fosse la prima volta
"""

    prompt = f"""Sei un analista tecnico indipendente, molto rigoroso, che scrive per un investitore italiano.

ASSET: {name} ({ticker}), tipo: {asset_type}

DATI TECNICI CALCOLATI (formule matematiche vere, non stime):
{json.dumps({k: v for k, v in indicators.items() if k not in ('closes', 'candles', 'sma50_series', 'sma200_series')}, indent=2, ensure_ascii=False)}

VIEW DI CRYPTONARY SU QUESTO ASSET:
{json.dumps(cryptonary_view, ensure_ascii=False, indent=2)}
{history_block}

PROFILO DI RISCHIO DELL'UTENTE: {risk_profile}

CONTESTO MACRO GENERALE (già ricercato una volta per tutte le pick di questo run,
NON cercarlo di nuovo — usalo così com'è come base per il campo "macro_context"):
{shared_macro or "Non disponibile in questo run."}

ISTRUZIONI SULLA RICERCA WEB (obbligatorio):
- Usa la ricerca web SOLO per notizie recenti specifiche su {name} ({ticker}) — il contesto
  macro generale ti è già stato fornito sopra, non serve ricercarlo di nuovo
- Dai priorità a fonti primarie e di alta affidabilità: comunicati ufficiali, agenzie di stampa
  di prima fascia (Reuters, Associated Press, Bloomberg). Evita blog, aggregatori, fonti anonime
- Se non trovi notizie specifiche recenti sull'asset, va benissimo dirlo esplicitamente
  invece di forzare una ricerca inutile

ISTRUZIONI SUI DATI CALCOLATI (obbligatorio):
- I campi "rsi_divergence_bearish" e "rsi_divergence_bullish" nei dati tecnici sopra sono
  calcolati con la matematica vera (non una tua stima): se non sono null, significa che è
  stata rilevata davvero una divergenza tra prezzo e RSI — usali come base concreta per il
  campo "pattern_analysis", citandoli esplicitamente. Se sono null, NON inventare una
  divergenza che non è stata rilevata
- Il campo "recent_price_sample" contiene gli ultimi prezzi di chiusura reali: usali per
  ancorare i tuoi commenti su pattern/struttura recente a dati veri, non solo ai numeri
  riassuntivi (RSI/SMA)

ISTRUZIONI SULL'INDIPENDENZA DI GIUDIZIO (obbligatorio):
- NON limitarti a ripetere il sentiment dominante di mercato o la narrativa di Cryptonary: valutali criticamente con i tuoi stessi dati
- Se il tuo parere diverge da quello di Cryptonary, dillo chiaramente e spiega perché, con il campo "divergence_from_cryptonary"
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


def call_claude_for_daily_updates(feed_headlines, community_highlights, airdrops_text, previous_updates_summary=""):
    """Digest giornaliero: feed editoriale + punti salienti della community +
    airdrop interessanti, con parere critico e confronto con ieri."""
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    prompt = f"""Sei un analista indipendente che prepara un riepilogo giornaliero per un investitore italiano.

STANDARD DI QUALITÀ (obbligatorio):
- Non dare per buono un dato senza averlo verificato; se qualcosa è ambiguo, dillo
- Sii critico verso le view di Cryptonary: hanno un interesse a mostrarle positivamente
- Linguaggio calibrato all'incertezza, mai assoluto
- Se il testo grezzo qui sotto è rumoroso o poco chiaro, fai del tuo meglio ma segnalalo

POST DEL FEED EDITORIALE (ultime ore, testo grezzo estratto dalla pagina):
{json.dumps(feed_headlines, ensure_ascii=False, indent=2)}

MESSAGGI DALLA COMMUNITY (testo grezzo, potrebbe contenere rumore/chiacchiere non rilevanti):
{json.dumps(community_highlights, ensure_ascii=False, indent=2)}

PAGINA AIRDROP (testo grezzo):
{airdrops_text[:3000]}

RIEPILOGO DI IERI (per confronto, se disponibile):
{previous_updates_summary or "Non disponibile."}

ISTRUZIONI:
- Dal feed editoriale: riassumi le notizie/post più rilevanti, in italiano, con eventuale giudizio Bullish/Bearish/Neutrale di Cryptonary se presente
- Dalla community: estrai SOLO ciò che è genuinamente rilevante (informazioni su asset da utenti esperti/staff, warning su problemi/bug, sentiment ricorrente su un asset condiviso da più persone) — ignora small talk e battute; se non trovi nulla di rilevante, dillo onestamente
- Dagli airdrop: segnala solo quelli che sembrano genuinamente interessanti (non tutti quelli presenti in pagina), con una breve motivazione
- Se hai il riepilogo di ieri, segnala esplicitamente cosa è cambiato di significativo
- Aggiungi un tuo breve parere personale dove hai elementi sufficienti per darlo

Rispondi SOLO con un oggetto JSON valido (nessun markdown, nessun testo fuori dal JSON):
{{
  "feed_html": "HTML con un <div class='update-item'> per ogni notizia rilevante, contenente titolo (<h4>), 2-3 frasi di riassunto (<p>), ed eventuale <span class='tag bull/bear/neutral'>Giudizio</span>",
  "community_html": "HTML con un <div class='update-item'> per ogni punto rilevante trovato in community, o un unico <p>Nessun punto rilevante nella community oggi.</p> se non c'è nulla",
  "airdrops_html": "HTML con un <div class='update-item'> per ogni airdrop interessante segnalato, con nome e breve motivazione, o <p>Nessun airdrop di particolare interesse oggi.</p> se non ce ne sono",
  "comparison_note": "1-2 frasi su cosa è cambiato rispetto a ieri, o stringa vuota se non disponibile un confronto",
  "summary_for_tomorrow": "3-4 frasi di sintesi complessiva della giornata, da riusare domani come 'riepilogo di ieri' per il confronto"
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
        log(f"ATTENZIONE: risposta di Claude (updates) non è JSON valido ({e}). Uso un fallback vuoto.")
        return {
            "feed_html": "<p>Dati non disponibili in questo aggiornamento a causa di un errore tecnico.</p>",
            "community_html": "<p>Non disponibile.</p>",
            "airdrops_html": "<p>Non disponibile.</p>",
            "comparison_note": "",
            "summary_for_tomorrow": "",
        }


def call_claude_for_period_report(period_label, log_entries, portfolio, risk_profile):
    """Sintetizza un report settimanale o mensile a partire dal 'diario' dei
    riepiloghi giornalieri accumulati (non rifà uno scraping apposta: il sito
    mostra solo l'attualità, non uno storico navigabile)."""
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    prompt = f"""Sei un analista indipendente che scrive un report {period_label} per un investitore italiano.

STANDARD DI QUALITÀ (obbligatorio):
- Non dare per buono un dato senza averlo verificato; se qualcosa è ambiguo, dillo
- Sii critico verso le view di Cryptonary: hanno un interesse a mostrarle positivamente
- Linguaggio calibrato all'incertezza, mai assoluto
- Considera sempre pro e contro prima di una conclusione

DIARIO DEI RIEPILOGHI GIORNALIERI ACCUMULATI IN QUESTO PERIODO ({len(log_entries)} giorni disponibili):
{json.dumps(log_entries, ensure_ascii=False, indent=2)}

PORTAFOGLIO REALE DELL'UTENTE:
{json.dumps(portfolio, ensure_ascii=False, indent=2)}

PROFILO DI RISCHIO: {risk_profile}

ISTRUZIONI:
- Sintetizza i temi ricorrenti e i cambiamenti più significativi emersi nei giorni sopra, non ripetere semplicemente ogni giorno in sequenza
- Se il diario copre meno giorni del periodo completo, dillo esplicitamente all'inizio (es. "Dati disponibili solo per N giorni su un periodo {period_label}")
- Commenta l'evoluzione del portafoglio reale dell'utente in questo periodo, se ci sono elementi sufficienti

Rispondi SOLO con un oggetto JSON valido (nessun markdown, nessun testo fuori dal JSON):
{{
  "report_html": "HTML con sezioni <h4> e paragrafi <p> per: sintesi del periodo, temi ricorrenti, evoluzione del portafoglio, punti di attenzione per il periodo successivo"
}}"""

    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=3000,
        messages=[{"role": "user", "content": prompt}],
    )
    text = resp.content[0].text
    text = re.sub(r"^```json\s*|\s*```$", "", text.strip())
    try:
        return json.loads(text)["report_html"]
    except (json.JSONDecodeError, KeyError) as e:
        log(f"ATTENZIONE: risposta di Claude (report {period_label}) non valida ({e}).")
        return f"<p>Report {period_label} non disponibile a causa di un errore tecnico. Riprova al prossimo run.</p>"


def load_html_block(marker_name, default=""):
    """Legge un blocco HTML delimitato da commenti <!-- MARKER_START/END -->
    dall'ultima dashboard pubblicata (per la modalità 'rapida' che riusa
    l'ultimo aggiornamento invece di rigenerarlo)."""
    if not os.path.exists(DASHBOARD_PATH):
        return default
    with open(DASHBOARD_PATH, "r", encoding="utf-8") as f:
        content = f.read()
    m = re.search(rf"<!-- {marker_name}_START -->(.*?)<!-- {marker_name}_END -->", content, re.S)
    return m.group(1).strip() if m else default


def load_previous_updates_updated():
    if not os.path.exists(DASHBOARD_PATH):
        return "—"
    with open(DASHBOARD_PATH, "r", encoding="utf-8") as f:
        content = f.read()
    m = re.search(r'__UPDATES_UPDATED__\s*\*/\s*"([^"]*)"', content)
    return m.group(1) if m else "—"


def load_previous_updates_summary():
    if not os.path.exists(DASHBOARD_PATH):
        return ""
    with open(DASHBOARD_PATH, "r", encoding="utf-8") as f:
        content = f.read()
    m = re.search(r'__UPDATES_SUMMARY__\s*\*/\s*(.*?)\s*/\*\s*__UPDATES_SUMMARY_END__', content, re.S)
    if not m:
        return ""
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return ""


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


def load_processed_issue_ids():
    return load_seed_array("PROCESSED_ISSUES")


def load_already_alerted():
    return load_seed_array("ALREADY_ALERTED")


def process_pending_issues(active, closed, processed_ids):
    """Legge le GitHub Issue aperte con etichetta 'portfolio' (create dal bottone
    Aggiungi/Vendi/Elimina sulla dashboard), applica la modifica al portafoglio,
    e le richiude con un commento di conferma.

    'processed_ids' è la NOSTRA lista persistente di issue già elaborate — non ci
    affidiamo solo allo stato 'closed' su GitHub (che potrebbe fallire ad
    aggiornarsi), così un'issue non viene mai rielaborata due volte per errore,
    ma un secondo acquisto REALE dello stesso ticker (issue diversa) viene sempre
    sommato correttamente alla posizione esistente invece di essere rifiutato."""
    gh_token = os.environ.get("GH_TOKEN")
    gh_repo = os.environ.get("GH_REPO")
    if not gh_token or not gh_repo:
        log("GH_TOKEN/GH_REPO non impostati: salto l'elaborazione delle issue.")
        return active, closed, processed_ids

    import requests

    headers = {"Authorization": f"Bearer {gh_token}", "Accept": "application/vnd.github+json"}
    resp = requests.get(
        f"https://api.github.com/repos/{gh_repo}/issues",
        headers=headers,
        params={"state": "open", "labels": "portfolio", "per_page": 100},
    )
    resp.raise_for_status()
    issues = resp.json()
    log(f"Trovate {len(issues)} issue 'portfolio' aperte su GitHub.")

    for issue in issues:
        if issue["number"] in processed_ids:
            log(f"  Issue #{issue['number']}: già elaborata in precedenza (anche se "
                f"risultava ancora aperta su GitHub), la salto e provo solo a richiuderla.")
            requests.patch(
                f"https://api.github.com/repos/{gh_repo}/issues/{issue['number']}",
                headers=headers, json={"state": "closed"},
            )
            continue

        body = issue.get("body", "") or ""
        match = re.search(r"```json\s*(\{.*?\})\s*```", body, re.S)
        if not match:
            log(f"  Issue #{issue['number']}: nessun blocco JSON valido, la salto (non segnata come elaborata).")
            continue
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError:
            log(f"  Issue #{issue['number']}: JSON non valido, la salto (non segnata come elaborata).")
            continue

        action = payload.get("action")
        ticker = (payload.get("ticker") or "").upper()
        comment = ""

        if action == "add":
            existing = next((p for p in active if p.get("ticker") == ticker), None)
            new_qty = payload.get("qty") or 0
            new_entry = payload.get("entry") or 0
            if existing and existing.get("qty") and existing.get("entry"):
                old_qty = existing["qty"]
                old_entry = existing["entry"]
                total_qty = old_qty + new_qty
                # prezzo medio di carico ponderato per quantità (pratica standard)
                weighted_entry = (old_qty * old_entry + new_qty * new_entry) / total_qty if total_qty else new_entry
                existing["qty"] = total_qty
                existing["entry"] = round(weighted_entry, 8)
                comment = (f"Posizione {ticker} aggiornata: sommate {new_qty} unità a quelle già "
                           f"presenti ({old_qty}). Nuovo totale: {total_qty}, prezzo medio di carico: {weighted_entry:.4f}.")
            else:
                active.append({
                    "name": payload.get("name", ticker),
                    "ticker": ticker,
                    "type": payload.get("type", "crypto"),
                    "qty": new_qty,
                    "entry": new_entry,
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

        # Segniamo l'issue come elaborata SUBITO, nella nostra lista persistente —
        # da qui in avanti non verrà più ri-applicata, indipendentemente da cosa
        # succede con le chiamate a GitHub qui sotto (che sono solo "cosmetiche":
        # commento + chiusura visibile, non la fonte di verità sull'idempotenza).
        processed_ids.append(issue["number"])

        comment_resp = requests.post(
            f"https://api.github.com/repos/{gh_repo}/issues/{issue['number']}/comments",
            headers=headers, json={"body": comment},
        )
        if not comment_resp.ok:
            log(f"  ATTENZIONE: commento su issue #{issue['number']} fallito "
                f"({comment_resp.status_code}): {comment_resp.text[:200]}")

        close_resp = requests.patch(
            f"https://api.github.com/repos/{gh_repo}/issues/{issue['number']}",
            headers=headers, json={"state": "closed"},
        )
        if not close_resp.ok:
            log(f"  NOTA: chiusura issue #{issue['number']} fallita su GitHub "
                f"({close_resp.status_code}) — non è un problema, è già segnata come "
                f"elaborata internamente e non verrà ripetuta.")
        else:
            log(f"  Issue #{issue['number']} elaborata e chiusa: {comment}")

    return active, closed, processed_ids

    return active, closed


def render_dashboard(picks_data, charts_data, deep_updated, portfolio_advice_html, portfolio, closed, processed_ids,
                      feed_html, community_html, airdrops_html, comparison_note, updates_updated, updates_summary_raw,
                      daily_log, weekly_html, weekly_updated, monthly_html, monthly_updated, analysis_history,
                      already_alerted):
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

    def replace_html_block(html, marker, content_html):
        pattern = rf"<!-- {marker}_START -->.*?<!-- {marker}_END -->"
        replacement = f'<!-- {marker}_START -->\n{content_html}\n<!-- {marker}_END -->'
        new_html, n = re.subn(pattern, replacement, html, flags=re.S)
        if n == 0:
            log(f"ATTENZIONE: segnaposto HTML {marker} non trovato nel template!")
        return new_html

    html = replace_html_block(html, "UPDATES_FEED", feed_html)
    html = replace_html_block(html, "UPDATES_COMMUNITY", community_html)
    html = replace_html_block(html, "UPDATES_AIRDROPS", airdrops_html)
    html = replace_html_block(html, "WEEKLY_REPORT", weekly_html)
    html = replace_html_block(html, "MONTHLY_REPORT", monthly_html)

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
    html = replace_js_array(html, "PROCESSED_ISSUES", processed_ids)
    html = replace_js_array(html, "ALREADY_ALERTED", already_alerted)

    # ANALYSIS_HISTORY è un oggetto {ticker: [...]}, non un array: usiamo lo
    # stesso pattern di replace_js_array ma con { } invece di [ ]
    pattern_hist = r"__ANALYSIS_HISTORY__\s*\*/\s*\{.*?\}\s*/\*\s*__ANALYSIS_HISTORY_END__"
    replacement_hist = f"__ANALYSIS_HISTORY__ */ {json.dumps(analysis_history, ensure_ascii=False)} /* __ANALYSIS_HISTORY_END__"
    html, n_hist = re.subn(pattern_hist, replacement_hist, html, flags=re.S)
    if n_hist == 0:
        log("ATTENZIONE: segnaposto __ANALYSIS_HISTORY__ non trovato nel template!")

    def replace_js_string(html, marker, value):
        pattern = rf"__{marker}__\s*\*/\s*.*?\s*/\*\s*__{marker}_END__"
        replacement = f"__{marker}__ */ {json.dumps(value, ensure_ascii=False)} /* __{marker}_END__"
        new_html, n = re.subn(pattern, replacement, html, count=1, flags=re.S)
        if n == 0:
            log(f"ATTENZIONE: segnaposto stringa __{marker}__ non trovato nel template!")
        return new_html

    html = replace_js_string(html, "DEEP_UPDATED", deep_updated)
    html = replace_js_string(html, "UPDATES_UPDATED", updates_updated)
    html = replace_js_string(html, "UPDATES_COMPARISON", comparison_note)
    html = replace_js_string(html, "UPDATES_SUMMARY", updates_summary_raw)
    html = replace_js_array(html, "DAILY_LOG", daily_log)
    html = replace_js_string(html, "WEEKLY_UPDATED", weekly_updated)
    html = replace_js_string(html, "MONTHLY_UPDATED", monthly_updated)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(DASHBOARD_PATH, "w", encoding="utf-8") as f:
        f.write(html)
    log(f"Dashboard scritta in {DASHBOARD_PATH}")


def check_and_consume_deep_analysis_request():
    """Controlla se c'è una richiesta manuale di analisi approfondita (Issue
    con etichetta 'analisi-richiesta', aperta dal pulsante sulla dashboard).
    Se trovata, la consuma (chiude) e ritorna True — l'analisi approfondita
    (costosa: ricerca web per ogni asset) NON scatta più da sola a orario
    fisso, solo quando esplicitamente richiesta così, per tenere sotto
    controllo il consumo di token."""
    gh_token = os.environ.get("GH_TOKEN")
    gh_repo = os.environ.get("GH_REPO")
    if not gh_token or not gh_repo:
        return False

    import requests
    headers = {"Authorization": f"Bearer {gh_token}", "Accept": "application/vnd.github+json"}
    resp = requests.get(
        f"https://api.github.com/repos/{gh_repo}/issues",
        headers=headers,
        params={"state": "open", "labels": "analisi-richiesta", "per_page": 10},
    )
    resp.raise_for_status()
    issues = resp.json()

    if not issues:
        return False

    log(f"Trovate {len(issues)} richieste di analisi approfondita — la eseguo questa volta.")
    for issue in issues:
        requests.post(
            f"https://api.github.com/repos/{gh_repo}/issues/{issue['number']}/comments",
            headers=headers,
            json={"body": "Richiesta accolta: analisi approfondita in corso in questa esecuzione."},
        )
        requests.patch(
            f"https://api.github.com/repos/{gh_repo}/issues/{issue['number']}",
            headers=headers, json={"state": "closed"},
        )
    return True


def check_portfolio_alerts(portfolio, charts_data, already_alerted):
    """Controlla le posizioni reali dell'utente per condizioni degne di un
    avviso (RSI estremo, prezzo oltre stop-loss/target, nuova divergenza da
    Cryptonary). Evita di ripetere lo stesso avviso più volte nello stesso
    giorno usando 'already_alerted' (lista di chiavi già notificate oggi)."""
    today_str = datetime.now(timezone.utc).strftime("%d/%m/%Y")
    # pulizia: teniamo solo le chiavi di oggi/ieri, così gli avvisi si "resettano" ogni giorno
    already_alerted = [k for k in already_alerted if today_str in k or
                        (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%d/%m/%Y") in k]

    new_alerts = []
    for pos in portfolio:
        ticker = pos.get("ticker", "").upper()
        chart = next((c for c in charts_data if c.get("ticker") == ticker), None)
        if not chart or not chart.get("sufficientData"):
            continue

        rsi = chart.get("rsiDaily")
        price = chart.get("currentPrice")
        medium = (chart.get("analysis") or {}).get("timeframes", {}).get("medium", {})
        stop_loss = medium.get("suggested_stop_loss")
        target = medium.get("suggested_target")
        divergence = (chart.get("analysis") or {}).get("divergence_from_cryptonary", "")

        candidates = []
        if rsi is not None and rsi >= 80:
            candidates.append(("rsi_high", f"RSI a {rsi} — ipercomprato estremo"))
        if rsi is not None and rsi <= 20:
            candidates.append(("rsi_low", f"RSI a {rsi} — ipervenduto estremo"))
        if price is not None and stop_loss is not None and price <= stop_loss:
            candidates.append(("stop_loss", f"Prezzo (${price:,.2f}) ha raggiunto/superato lo stop-loss suggerito (${stop_loss:,.2f})"))
        if price is not None and target is not None and price >= target:
            candidates.append(("target", f"Prezzo (${price:,.2f}) ha raggiunto/superato il target suggerito (${target:,.2f})"))
        if divergence:
            candidates.append(("divergence", f"Il mio parere diverge da quello di Cryptonary: {divergence[:150]}"))

        for key, detail in candidates:
            alert_key = f"{ticker}:{key}:{today_str}"
            if alert_key in already_alerted:
                continue
            new_alerts.append({"ticker": ticker, "name": pos.get("name", ticker), "detail": detail})
            already_alerted.append(alert_key)

    return new_alerts, already_alerted


def send_alert_email(alerts):
    """Invia un'unica email con tutti gli avvisi del run, usando l'App Password
    Gmail già configurata (funziona per SMTP oltre che per IMAP)."""
    import smtplib
    from email.mime.text import MIMEText

    to_addr = os.environ["CRYPTONARY_EMAIL"]
    gmail_pass = os.environ["GMAIL_APP_PASSWORD"]

    lines = [f"- {a['name']} ({a['ticker']}): {a['detail']}" for a in alerts]
    body = (
        "Avvisi dal tuo Cryptonary Ledger:\n\n" + "\n".join(lines) +
        "\n\nQuesto è un avviso automatico generato da IA, non consulenza finanziaria. "
        "Controlla la dashboard per i dettagli completi."
    )
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = f"⚠️ Cryptonary Ledger — {len(alerts)} avviso/i importante/i"
    msg["From"] = to_addr
    msg["To"] = to_addr

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(to_addr, gmail_pass)
            server.send_message(msg)
        log(f"Email di avviso inviata ({len(alerts)} avvisi).")
    except Exception as e:
        log(f"ERRORE invio email di avviso: {e}")


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


def should_run_deep_mode(deep_updated_str, threshold_hours=10):
    """Decide se fare l'analisi approfondita in base a QUANTO TEMPO è passato
    dall'ultima volta, non controllando l'ora esatta — le esecuzioni schedulate
    di GitHub Actions possono slittare anche di ore (osservato: fino a 4h30 di
    ritardo), quindi un controllo sull'ora esatta rischierebbe di saltare
    l'analisi approfondita per un giorno intero se lo slot 'giusto' slitta
    fuori dalla finestra attesa. Soglia di 10 ore: con 2 run al giorno,
    lasciamo un margine di 2 ore di tolleranza sul ritardo prima di forzare
    comunque una nuova analisi approfondita."""
    if not deep_updated_str or deep_updated_str == "—":
        return True
    try:
        last_dt = datetime.strptime(deep_updated_str, "%d/%m/%Y %H:%M UTC").replace(tzinfo=timezone.utc)
    except ValueError:
        log(f"ATTENZIONE: formato data inatteso in DEEP_UPDATED ({deep_updated_str!r}), forzo modalità approfondita per sicurezza.")
        return True
    hours_since = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600
    return hours_since >= threshold_hours


def main():
    import indicators

    utc_hour = datetime.now(timezone.utc).hour
    deep_updated = load_previous_deep_updated()
    # Una sola volta al giorno (non più 2), per contenere il consumo di token:
    # soglia di 20 ore così si allinea naturalmente sempre sullo stesso slot
    # giornaliero (13:00 UTC = 15:00 ora italiana estiva), tollerando eventuali
    # ritardi di GitHub Actions senza mai saltare un giorno intero
    # L'analisi approfondita (costosa: ricerca web per ogni asset) NON scatta
    # più da sola a orario fisso — solo quando richiesta esplicitamente dal
    # pulsante sulla dashboard, per tenere sotto controllo i costi
    deep_mode = check_and_consume_deep_analysis_request()
    updates_updated = load_previous_updates_updated()
    updates_mode = should_run_deep_mode(updates_updated, threshold_hours=20)
    log(f"Ora UTC: {utc_hour} — ultima analisi approfondita: {deep_updated} — "
        f"modalità: {'APPROFONDITA' if deep_mode else 'rapida'} — "
        f"aggiornamenti: {'da rigenerare' if updates_mode else 'riuso ultimo'}")

    portfolio = load_portfolio_from_previous_dashboard()
    closed = load_closed_from_previous_dashboard()
    processed_ids = load_processed_issue_ids()

    log("Elaborazione issue 'portfolio' in sospeso...")
    portfolio, closed, processed_ids = process_pending_issues(portfolio, closed, processed_ids)

    risk_profile = "PRUDENTE sulle stock picks, MODERATA sulle crypto picks"

    feed_headlines, community_highlights, airdrops_text = [], [], ""

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

        if updates_mode:
            log("Lettura feed per gli Aggiornamenti...")
            feed_headlines = scrape_daily_feed_headlines(page)
            log(f"  {len(feed_headlines)} post trovati")

            log("Lettura Community...")
            community_highlights = scrape_community_highlights(page)
            log(f"  {len(community_highlights)} righe canale trovate")

            log("Lettura Airdrop...")
            airdrops_text = scrape_airdrops(page)
            log(f"  {len(airdrops_text)} caratteri letti dalla pagina Airdrop")

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

    if deep_mode:
        log("Modalità approfondita: calcolo indicatori + analisi per ogni asset...")

        log("  Ricerca del contesto macro condiviso (una sola volta per tutto il run)...")
        shared_macro = fetch_shared_macro_context()
        log(f"  Contesto macro ottenuto ({len(shared_macro)} caratteri).")

        analysis_history = load_analysis_history()
        today_str = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")

        for p in structured_picks:
            log(f"  Analisi {p['ticker']}...")
            ind_data = indicators.analyze_asset(p["ticker"], p["type"], p["name"])

            if not ind_data.get("sufficient_data"):
                charts_data.append({
                    "name": p["name"], "ticker": p["ticker"], "currentPrice": p.get("current"),
                    "sufficientData": False,
                })
                continue

            asset_hist = analysis_history.get(p["ticker"], [])[-7:]  # ultimi ~3-4 giorni (2 run/giorno)

            claude_analysis = call_claude_for_asset_analysis(
                p["ticker"], p["name"], p["type"], ind_data, p, risk_profile, shared_macro, asset_hist
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
                "rsiDivergenceBearish": ind_data.get("rsi_divergence_bearish"),
                "rsiDivergenceBullish": ind_data.get("rsi_divergence_bullish"),
                "supportRecent": ind_data["support_recent"],
                "resistanceRecent": ind_data["resistance_recent"],
                "analysis": claude_analysis,
            })

            # aggiungiamo una voce compatta allo storico di QUESTO asset, per
            # le prossime esecuzioni (non l'intera analisi: solo l'essenziale,
            # per non far crescere troppo il prompt nel tempo)
            medium_tf = (claude_analysis or {}).get("timeframes", {}).get("medium", {})
            history_entry = {
                "data": today_str,
                "prezzo": ind_data["current_price"],
                "raccomandazione_medio_termine": medium_tf.get("recommendation"),
                "probabilita_stimata": medium_tf.get("confidence_pct"),
                "sintesi": (claude_analysis or {}).get("my_opinion", "")[:300],
            }
            ticker_hist = analysis_history.get(p["ticker"], [])
            ticker_hist.append(history_entry)
            analysis_history[p["ticker"]] = ticker_hist[-14:]  # tiene ~una settimana (2 run/giorno)

            # aggiorna anche il prezzo e la variazione % nel ticker
            for pd in picks_data:
                if pd["ticker"] == p["ticker"]:
                    pd["price"] = ind_data["current_price"]
                    pd["chg"] = compute_chg_pct(ind_data["candles"])

        deep_updated = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    else:
        log("Modalità rapida: salto l'analisi approfondita, riuso l'ultima disponibile.")
        charts_data = load_previous_charts_data()
        analysis_history = load_analysis_history()  # invariato, solo riletto per passarlo a render_dashboard
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

    # ---- Aggiornamenti (feed + community + airdrop) ----
    if updates_mode:
        log("Generazione digest Aggiornamenti (Claude)...")
        previous_summary = load_previous_updates_summary()
        updates_result = call_claude_for_daily_updates(
            feed_headlines, community_highlights, airdrops_text, previous_summary
        )
        feed_html = updates_result["feed_html"]
        community_html = updates_result["community_html"]
        airdrops_html = updates_result["airdrops_html"]
        comparison_note = updates_result["comparison_note"]
        updates_summary_raw = updates_result["summary_for_tomorrow"]
        updates_updated = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    else:
        log("Aggiornamenti: riuso l'ultimo digest disponibile (non ancora ora di rigenerarlo).")
        feed_html = load_html_block("UPDATES_FEED", "<p>Non ancora disponibile.</p>")
        community_html = load_html_block("UPDATES_COMMUNITY", "<p>Non ancora disponibile.</p>")
        airdrops_html = load_html_block("UPDATES_AIRDROPS", "<p>Non ancora disponibile.</p>")
        comparison_note = ""  # non rigenerato in modalità rapida, resta vuoto (nessun 'rispetto a ieri' nuovo)
        updates_summary_raw = load_previous_updates_summary()
        # updates_updated resta quello caricato all'inizio di main() (invariato)

    # ---- Diario giornaliero (per i report settimanale/mensile) ----
    daily_log = load_daily_log()
    if updates_mode and updates_summary_raw:
        today_str = datetime.now(timezone.utc).strftime("%d/%m/%Y")
        # evita di aggiungere due volte lo stesso giorno se il run gira più volte nello stesso giorno
        daily_log = [e for e in daily_log if e.get("date") != today_str]
        daily_log.append({"date": today_str, "summary": updates_summary_raw})
        daily_log = daily_log[-30:]  # tiene al massimo 30 giorni (copre anche il mensile)
        log(f"Diario giornaliero aggiornato: {len(daily_log)} giorni accumulati.")

    # ---- Report settimanale ----
    weekly_updated = load_period_updated("WEEKLY_UPDATED")
    weekly_time_due = should_run_deep_mode(weekly_updated, threshold_hours=24 * 7)
    weekly_mode = weekly_time_due and len(daily_log) >= 7
    if weekly_time_due and not weekly_mode:
        log(f"Report settimanale: passato abbastanza tempo ma solo {len(daily_log)}/7 giorni di diario — aspetto ancora.")
    if weekly_mode:
        log(f"Generazione report settimanale (diario: {len(daily_log)} giorni disponibili)...")
        weekly_html = call_claude_for_period_report("settimanale", daily_log[-7:], portfolio, risk_profile)
        weekly_updated = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    else:
        weekly_html = load_html_block("WEEKLY_REPORT", "")

    # ---- Report mensile ----
    monthly_updated = load_period_updated("MONTHLY_UPDATED")
    monthly_time_due = should_run_deep_mode(monthly_updated, threshold_hours=24 * 30)
    monthly_mode = monthly_time_due and len(daily_log) >= 30
    if monthly_time_due and not monthly_mode:
        log(f"Report mensile: passato abbastanza tempo ma solo {len(daily_log)}/30 giorni di diario — aspetto ancora.")
    if monthly_mode:
        log(f"Generazione report mensile (diario: {len(daily_log)} giorni disponibili)...")
        monthly_html = call_claude_for_period_report("mensile", daily_log[-30:], portfolio, risk_profile)
        monthly_updated = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    else:
        monthly_html = load_html_block("MONTHLY_REPORT", "")

    # ---- Avvisi email su condizioni rilevanti per le posizioni reali ----
    already_alerted = load_already_alerted()
    new_alerts, already_alerted = check_portfolio_alerts(portfolio, charts_data, already_alerted)
    if new_alerts:
        log(f"Trovati {len(new_alerts)} nuovi avvisi da inviare via email.")
        send_alert_email(new_alerts)
    else:
        log("Nessun nuovo avviso da inviare.")

    log("Generazione dashboard...")
    render_dashboard(picks_data, charts_data, deep_updated, portfolio_advice_html, portfolio, closed, processed_ids,
                      feed_html, community_html, airdrops_html, comparison_note, updates_updated, updates_summary_raw,
                      daily_log, weekly_html, weekly_updated, monthly_html, monthly_updated, analysis_history,
                      already_alerted)

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


def load_daily_log():
    return load_seed_array("DAILY_LOG")


def load_analysis_history():
    """Legge lo storico delle analisi per ogni asset: {ticker: [{data, ...}, ...]}.
    È un oggetto (non un array), quindi non usa load_seed_array."""
    if not os.path.exists(DASHBOARD_PATH):
        return {}
    with open(DASHBOARD_PATH, "r", encoding="utf-8") as f:
        content = f.read()
    m = re.search(r"__ANALYSIS_HISTORY__\s*\*/\s*(\{.*?\})\s*/\*\s*__ANALYSIS_HISTORY_END__", content, re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return {}


def load_period_updated(marker_name):
    if not os.path.exists(DASHBOARD_PATH):
        return "—"
    with open(DASHBOARD_PATH, "r", encoding="utf-8") as f:
        content = f.read()
    m = re.search(rf'__{marker_name}__\s*\*/\s*"([^"]*)"', content)
    return m.group(1) if m else "—"


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"ERRORE: {e}")
        sys.exit(1)
