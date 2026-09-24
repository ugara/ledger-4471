"""
Cryptonary Daily Report + Dashboard generator.

Login tramite sessione già autenticata (catturata dal tuo browser reale con un
segnalibro speciale e passata via GitHub Issue) — evita del tutto la schermata
di login automatica, che Cloudflare blocca quando è un robot a provarci.
Legge le pick attive (crypto + stock), l'analisi tecnica con dati storici
reali, e genera/aggiorna docs/index.html.

Nessuno schedule automatico: lo script gira SOLO quando lo lanci tu a mano
("Run workflow" su GitHub Actions), dopo aver passato una sessione fresca.

Variabili d'ambiente richieste:
  ANTHROPIC_API_KEY - per le chiamate a Claude
  GH_TOKEN          - personal access token con scope 'repo' per leggere/scrivere le Issue
  GH_REPO           - "utente/nome-repo"
"""

import os
import json
import re
import sys
from datetime import datetime, timezone, timedelta

from playwright.sync_api import sync_playwright
import anthropic

BASE_URL = "https://cryptonary.com"
SUPABASE_PROJECT = "loiogbgrlcppzxshaenr"
AUTH_STORAGE_KEY = f"sb-{SUPABASE_PROJECT}-auth-token"

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "docs")
DASHBOARD_PATH = os.path.join(OUTPUT_DIR, "index.html")
TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "dashboard_v2_phase1.html")


def log(msg):
    print(f"[{datetime.now().isoformat()}] {msg}", flush=True)


# ============================================================
# LOGIN tramite sessione iniettata (niente più OTP automatico)
# ============================================================

def fetch_and_consume_session_issue():
    """Cerca una Issue con etichetta 'sessione-login' (aperta dal segnalibro
    quando sei loggato nel tuo browser), ne estrae la sessione, e la CONSUMA
    subito (chiude la issue) perché è a tutti gli effetti una credenziale —
    non deve restare in giro più del necessario. Ritorna la stringa JSON
    della sessione, o None se non trovata."""
    gh_token = os.environ.get("GH_TOKEN")
    gh_repo = os.environ.get("GH_REPO")
    if not gh_token or not gh_repo:
        raise RuntimeError("GH_TOKEN/GH_REPO non impostati: impossibile leggere la sessione.")

    import requests
    headers = {"Authorization": f"Bearer {gh_token}", "Accept": "application/vnd.github+json"}
    resp = requests.get(
        f"https://api.github.com/repos/{gh_repo}/issues",
        headers=headers,
        params={"state": "open", "labels": "sessione-login", "per_page": 5},
    )
    resp.raise_for_status()
    issues = resp.json()
    if not issues:
        return None

    issue = issues[0]
    body = issue.get("body", "") or ""
    match = re.search(r"```json\s*(\{.*?\})\s*```", body, re.S)
    session_json = match.group(1) if match else body.strip()

    # consumiamo subito la issue (chiusa + commento), la sessione non deve restare esposta
    requests.post(
        f"https://api.github.com/repos/{gh_repo}/issues/{issue['number']}/comments",
        headers=headers, json={"body": "Sessione ricevuta e usata per il login di questa esecuzione."},
    )
    requests.patch(
        f"https://api.github.com/repos/{gh_repo}/issues/{issue['number']}",
        headers=headers, json={"state": "closed"},
    )
    return session_json


def login_with_session(page, session_json_raw):
    """Inietta la sessione catturata dal browser reale dell'utente, USATA
    COSÌ COM'È — non forziamo più un refresh immediato (expires_at=0): è
    proprio quello a causare il problema. Il client Supabase del tuo browser
    tiene un timer che rinnova il token in background anche solo con la
    scheda aperta, e i refresh token sono a uso singolo — se noi forziamo
    un altro refresh con un token magari già usato dal tuo browser nel
    frattempo, quello nostro viene rifiutato. Usando l'access_token diretto
    (valido di solito per circa un'ora) evitiamo del tutto questa corsa,
    finché l'esecuzione parte entro quella finestra di validità."""
    page.goto(BASE_URL, wait_until="domcontentloaded")

    session = json.loads(session_json_raw)

    expires_at = session.get("expires_at")
    if expires_at:
        seconds_left = expires_at - datetime.now(timezone.utc).timestamp()
        if seconds_left <= 0:
            raise RuntimeError(
                f"Il token catturato è già scaduto da {abs(seconds_left):.0f} secondi "
                "(troppo tempo tra il click sul segnalibro e questa esecuzione). "
                "Rifai login, clicca il segnalibro, e lancia il workflow subito dopo."
            )
        log(f"Token valido ancora per {seconds_left:.0f} secondi al momento dell'uso.")

    page.evaluate(
        "([key, value]) => window.localStorage.setItem(key, value)",
        [AUTH_STORAGE_KEY, json.dumps(session)],
    )
    page.goto(f"{BASE_URL}/home", wait_until="domcontentloaded")
    page.wait_for_timeout(3000)  # non "networkidle": polling continuo del ticker prezzi "Live", non scatterebbe mai
    page.wait_for_timeout(2000)

    title = page.title()
    body_text = page.evaluate("() => document.body.innerText.slice(0, 600)")
    log(f"Login con sessione — titolo pagina dopo il refresh: {title!r}")

    # Il titolo da solo non basta: una sessione "iniettata ma non accettata"
    # non mostra "Sign In" nel titolo, ma la pagina resta comunque quella
    # pubblica (muro di iscrizione) invece della dashboard personale — lo
    # riconosciamo dal contenuto della pagina, non dal titolo/URL.
    logged_out_markers = ["Join for free", "Sign In Now", "Join Cryptonary", "Start Your"]
    if "Sign In" in title or "sign-in" in page.url or any(m in body_text for m in logged_out_markers):
        raise RuntimeError(
            "La sessione non ha funzionato: la pagina risulta ancora NON autenticata "
            "(probabile causa: il token è scaduto o è già stato 'ruotato' dal tuo "
            "browser prima che lo script lo usasse — i token Supabase si invalidano "
            "a ogni utilizzo). Rifai login su Cryptonary, clicca subito il segnalibro "
            "senza continuare a navigare in quella scheda, conferma la Issue e "
            "rilancia il workflow il prima possibile dopo.\n"
            f"Contenuto pagina al momento del controllo: {body_text[:300]!r}"
        )
    log("Login con sessione confermato: pagina personale caricata correttamente.")


def scrape_picks(page, url, debug_label=""):
    """Estrae le righe della tabella pick (crypto o stock) leggendo il testo
    visibile — robusto a piccoli cambi di stile, fragile a cambi di struttura."""
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_timeout(3000)  # non "networkidle": polling continuo del ticker prezzi "Live", non scatterebbe mai

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
    """Legge il feed principale. I post PULSE restano solo l'anteprima della
    card (già abbastanza completa per queste notizie brevi); per i post
    PINNED — quelli che Cryptonary segna come più importanti — entriamo
    dentro e leggiamo il testo integrale dell'articolo, non solo l'anteprima
    (altrimenti l'analisi non ha i dati veri per verificare cose come livelli
    di prezzo specifici). Ritorna una lista di dizionari, non stringhe, così
    il testo integrale resta disponibile per la visualizzazione espandibile
    sulla dashboard senza dover farlo ripetere a Claude nella risposta."""
    page.goto(f"{BASE_URL}/home", wait_until="domcontentloaded")
    page.wait_for_timeout(3000)  # non "networkidle": polling continuo del ticker prezzi "Live", non scatterebbe mai
    page.wait_for_timeout(1500)

    card_previews = page.evaluate(
        """
        () => {
            const cards = Array.from(document.querySelectorAll('.first\\\\:pt-0'));
            return cards.map((c, idx) => ({
                idx,
                text: c.innerText.replace(/\\n/g, ' | ').trim(),
                isPinned: c.innerText.trim().startsWith('PINNED'),
            })).filter(c => c.text);
        }
        """
    )

    items = []
    for card in card_previews:
        entry = {"preview": card["text"], "is_pinned": card["isPinned"], "full_text": None}

        if not card["isPinned"]:
            items.append(entry)
            continue

        try:
            cards_now = page.query_selector_all(".first\\:pt-0")
            if card["idx"] >= len(cards_now):
                items.append(entry)
                continue
            cards_now[card["idx"]].click()
            page.wait_for_timeout(2000)

            full_text = page.evaluate(
                """
                () => {
                    const candidates = Array.from(document.querySelectorAll('main article, main section, main div'))
                        .filter(el => el.innerText && el.innerText.length > 200);
                    if (!candidates.length) return null;
                    candidates.sort((a, b) => b.innerText.length - a.innerText.length);
                    let text = candidates[0].innerText.trim();
                    // rimuove il rumore iniziale (breadcrumb, titolo duplicato,
                    // tag categoria, autore, contatori like/commenti) tagliando
                    // tutto prima di "Published ... ago", che segna l'inizio
                    // vero del contenuto dell'articolo
                    const publishedMatch = text.match(/Published .{0,30}ago/);
                    if (publishedMatch) {
                        text = text.slice(publishedMatch.index + publishedMatch[0].length).trim();
                    }
                    text = text.replace(/^(\\s*\\d+\\s*)+/, '').trim();
                    return text.slice(0, 15000);
                }
                """
            )
            entry["full_text"] = full_text
            items.append(entry)

            page.go_back(wait_until="domcontentloaded")
            page.wait_for_timeout(3000)  # non "networkidle": polling continuo del ticker prezzi "Live", non scatterebbe mai
            page.wait_for_timeout(1000)
        except Exception as e:
            log(f"  Impossibile leggere il testo integrale di un post PINNED, uso solo l'anteprima: {e}")
            items.append(entry)
            try:
                page.goto(f"{BASE_URL}/home", wait_until="domcontentloaded")
                page.wait_for_timeout(3000)  # non "networkidle": polling continuo del ticker prezzi "Live", non scatterebbe mai
                page.wait_for_timeout(1000)
            except Exception:
                pass

    return items


def scrape_community_highlights(page):
    """Legge i messaggi recenti visibili nella sezione Community (canali di
    chat). Ogni riga canale è un tag <a> con questa classe specifica —
    verificato navigando dal vivo il sito."""
    try:
        page.goto(f"{BASE_URL}/home", wait_until="domcontentloaded")
        page.wait_for_timeout(3000)  # non "networkidle": polling continuo del ticker prezzi "Live", non scatterebbe mai
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
        page.goto(f"{BASE_URL}/airdrops/shortlist", wait_until="domcontentloaded")
        page.wait_for_timeout(3000)  # non "networkidle": polling continuo del ticker prezzi "Live", non scatterebbe mai
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

ISTRUZIONI SUI DATI CALCOLATI (obbligatorio, importante):
- Il campo "timeframes" contiene TRE blocchi separati — "short", "medium", "long" — ognuno con
  i propri dati calcolati SU QUELLA SCALA TEMPORALE specifica, non riciclati dagli altri:
  * "short" (breve termine): trend calcolato su SMA10/SMA30 giornaliere, supporto/resistenza
    sugli ultimi ~30 giorni, campione prezzi giornaliero, divergenza RSI giornaliera
  * "medium" (medio termine): trend su SMA50/SMA200 giornaliere, supporto/resistenza sugli
    ultimi ~180 giorni, campione prezzi SETTIMANALE, divergenza RSI settimanale
  * "long" (lungo termine): trend calcolato sui dati mensili, supporto/resistenza su tutto lo
    storico disponibile, campione prezzi MENSILE, divergenza RSI mensile
- OBBLIGATORIO: quando scrivi il blocco "timeframes.short" del tuo output, usa SOLO i dati di
  "timeframes.short" qui sopra. Stesso discorso per medium e long. Non mescolare mai i dati di
  un orizzonte nell'analisi di un altro — è successo in passato (es. citare una divergenza
  giornaliera anche nel commento di lungo termine) ed è un errore di analisi tecnica reale,
  non un dettaglio stilistico: ogni orizzonte deve riflettere SOLO i suoi dati
- "price_sample" in ogni blocco contiene prezzi di chiusura reali alla granularità indicata da
  "price_sample_granularity" (giornaliera/settimanale/mensile) — usali per ancorare i tuoi
  commenti su pattern/struttura a dati veri di QUELLA scala temporale, non a numeri riassuntivi
- Se "timeframes.long.rsi_divergence_unavailable" è true, lo storico disponibile è ancora
  troppo corto per calcolare una divergenza mensile affidabile — dillo esplicitamente nel
  pattern_analysis del lungo termine invece di ometterlo o inventare un risultato
- "timeframes.long.trend" può indicare uno storico mensile ancora corto (es. "storico ancora
  corto per una lettura più solida") — se lo indica, ripeti questa cautela nella tua analisi
  di lungo termine invece di scrivere con sicurezza come se i dati fossero solidi

ISTRUZIONI SULLA RICERCA WEB (obbligatorio):
- Usa la ricerca web SOLO per notizie recenti specifiche su {name} ({ticker}) — il contesto
  macro generale ti è già stato fornito sopra, non serve ricercarlo di nuovo
- Dai priorità a fonti primarie e di alta affidabilità: comunicati ufficiali, agenzie di stampa
  di prima fascia (Reuters, Associated Press, Bloomberg). Evita blog, aggregatori, fonti anonime
- Se non trovi notizie specifiche recenti sull'asset, va benissimo dirlo esplicitamente
  invece di forzare una ricerca inutile

ISTRUZIONI SULL'INDIPENDENZA DI GIUDIZIO (obbligatorio):
- NON limitarti a ripetere il sentiment dominante di mercato o la narrativa di Cryptonary: valutali criticamente con i tuoi stessi dati
- Se il tuo parere diverge da quello di Cryptonary, dillo chiaramente e spiega perché, con il campo "divergence_from_cryptonary"
- Considera esplicitamente scenario a favore E scenario contrario prima di ogni conclusione
- Il conteggio delle onde di Elliott è intrinsecamente soggettivo: presentalo come "una lettura plausibile", mai come un fatto certo
- Il conteggio delle onde per OGNI orizzonte va fatto guardando SOLO il "price_sample" di quello
  stesso blocco "timeframes.short/medium/long" (rispettivamente giornaliero/settimanale/mensile) —
  un conteggio di lungo termine basato su un campione giornaliero di poche settimane non è un
  conteggio di lungo termine, è un errore di analisi tecnica, non solo un dettaglio stilistico
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
        max_tokens=6000,
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
    airdrop interessanti, con parere critico e confronto con ieri.

    'feed_headlines' è una lista di dizionari {preview, is_pinned, full_text}
    (vedi scrape_daily_feed_headlines). Chiediamo a Claude un giudizio PER
    OGNI voce, nello stesso ordine — poi ricombiniamo noi in Python con il
    testo integrale originale, così Claude non deve mai ripeterlo nella
    risposta (più economico) e possiamo renderlo espandibile sulla pagina."""
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    feed_for_prompt = [
        {"indice": i, "anteprima": item["preview"], "pinnato": item["is_pinned"],
         "testo_integrale": item["full_text"] if item["is_pinned"] else None}
        for i, item in enumerate(feed_headlines)
    ]

    prompt = f"""Sei un analista indipendente che prepara un riepilogo giornaliero per un investitore italiano.

STANDARD DI QUALITÀ (obbligatorio):
- Non dare per buono un dato senza averlo verificato; se qualcosa è ambiguo, dillo
- Sii critico verso le view di Cryptonary: hanno un interesse a mostrarle positivamente
- Linguaggio calibrato all'incertezza, mai assoluto
- Se il testo grezzo qui sotto è rumoroso o poco chiaro, fai del tuo meglio ma segnalalo

POST DEL FEED EDITORIALE (ultime ore). Per i post PINNATI hai anche il testo
integrale dell'articolo (non solo l'anteprima) — usalo per valutare
criticamente eventuali affermazioni concrete (livelli di prezzo, target,
previsioni), non limitarti a riportarle:
{json.dumps(feed_for_prompt, ensure_ascii=False, indent=2)}

MESSAGGI DALLA COMMUNITY (testo grezzo, potrebbe contenere rumore/chiacchiere non rilevanti):
{json.dumps(community_highlights, ensure_ascii=False, indent=2)}

PAGINA AIRDROP (testo grezzo):
{airdrops_text[:3000]}

RIEPILOGO DI IERI (per confronto, se disponibile):
{previous_updates_summary or "Non disponibile."}

ISTRUZIONI SUI POST PINNATI (obbligatorio, importante):
- Quando hai il testo integrale, valuta esplicitamente QUANTO SONO FATTIBILI le
  affermazioni concrete fatte (livelli di prezzo, target, previsioni): sono
  coerenti con il contesto tecnico/macro che conosci? Sono presentate con
  cautela adeguata o come certezze? C'è un incentivo di Cryptonary a essere
  ottimista che va segnalato?
- Questo commento critico va nel campo "commento_critico" (massimo 4-5 frasi, sii conciso) — se un post PULSE
  non ha affermazioni concrete da valutare, lascialo vuoto ("")
- Per ogni post con testo integrale, valuta e commenta criticamente il contenuto
  in italiano (la traduzione del testo integrale viene gestita separatamente,
  non serve che tu lo traduca)

ALTRE ISTRUZIONI:
- Dalla community: estrai SOLO ciò che è genuinamente rilevante — ignora small talk; se non trovi nulla di rilevante, dillo onestamente
- Dagli airdrop: segnala solo quelli che sembrano genuinamente interessanti, con una breve motivazione
- Se hai il riepilogo di ieri, segnala esplicitamente cosa è cambiato di significativo

Rispondi SOLO con un oggetto JSON valido (nessun markdown, nessun testo fuori dal JSON):
{{
  "feed_items": [
    {{"indice": 0, "titolo": "titolo breve della notizia", "tag": "bull" o "bear" o "neutral",
      "riassunto": "2-3 frasi di riassunto", "commento_critico": "vedi istruzioni sopra, stringa vuota se non applicabile"}}
    // un oggetto per OGNI voce del feed sopra, stesso indice, stesso ordine — non saltarne nessuna
  ],
  "community_html": "HTML con un <div class='update-item'> per ogni punto rilevante trovato in community, o un unico <p>Nessun punto rilevante nella community oggi.</p> se non c'è nulla",
  "airdrops_html": "HTML con un <div class='update-item'> per ogni airdrop interessante segnalato, con nome e breve motivazione, o <p>Nessun airdrop di particolare interesse oggi.</p> se non ce ne sono",
  "comparison_note": "1-2 frasi su cosa è cambiato rispetto a ieri, o stringa vuota se non disponibile un confronto",
  "summary_for_tomorrow": "3-4 frasi di sintesi complessiva della giornata, da riusare domani come 'riepilogo di ieri' per il confronto"
}}"""

    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8192,
        messages=[{"role": "user", "content": prompt}],
    )
    text = resp.content[0].text
    text = re.sub(r"^```json\s*|\s*```$", "", text.strip())
    try:
        result = json.loads(text)
    except json.JSONDecodeError as e:
        log(f"ATTENZIONE: risposta di Claude (updates) non è JSON valido ({e}). Uso un fallback vuoto.")
        return {
            "feed_html": "<p>Dati non disponibili in questo aggiornamento a causa di un errore tecnico.</p>",
            "community_html": "<p>Non disponibile.</p>",
            "airdrops_html": "<p>Non disponibile.</p>",
            "comparison_note": "",
            "summary_for_tomorrow": "",
        }

    # Ricombiniamo qui in Python il testo integrale originale (che Claude non
    # ha mai dovuto ripetere) con il giudizio di Claude, per costruire un
    # blocco espandibile nativo (<details>) per ogni post pinnato.
    feed_items_by_idx = {fi.get("indice"): fi for fi in result.get("feed_items", [])}
    html_parts = []
    for i, item in enumerate(feed_headlines):
        judged = feed_items_by_idx.get(i, {})
        tag = judged.get("tag", "neutral")
        titolo = judged.get("titolo") or item["preview"][:80]
        riassunto = judged.get("riassunto", "")
        commento = judged.get("commento_critico", "")

        block = f'<div class="update-item"><h4>{titolo} <span class="tag {tag}">{tag.capitalize()}</span></h4><p>{riassunto}</p>'
        if commento:
            block += f'<p class="critical-note">⚠️ {commento}</p>'
        if item["is_pinned"] and item.get("full_text"):
            testo_da_mostrare = item.get("full_text_it") or item["full_text"] or ""
            full_text_escaped = testo_da_mostrare.replace("<", "&lt;").replace(">", "&gt;")
            block += (f'<details><summary>Espandi per leggere il testo integrale del post</summary>'
                      f'<pre class="full-text">{full_text_escaped}</pre></details>')
        block += '</div>'
        html_parts.append(block)

    result["feed_html"] = "\n".join(html_parts) if html_parts else "<p>Nessun post nel feed oggi.</p>"
    return result


def translate_to_italian(text):
    """Traduzione dedicata, con budget di token tutto per sé — non compete
    con il resto della risposta del digest giornaliero, che era la causa più
    probabile del troncamento osservato."""
    if not text:
        return ""
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    prompt = f"""Traduci il seguente testo in italiano. Traduzione COMPLETA e fedele
(non un riassunto), mantenendo i paragrafi originali. Rispondi SOLO con il
testo tradotto, nessun commento, nessuna premessa.

TESTO DA TRADURRE:
{text}"""
    try:
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=8192,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text.strip()
    except Exception as e:
        log(f"Errore nella traduzione, uso il testo originale: {e}")
        return text


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
    updates_updated = load_previous_updates_updated()
    # Nessun controllo "è già passato abbastanza tempo?": lo script gira SOLO
    # quando lo lanci tu a mano, quindi ogni esecuzione fa sempre il lavoro
    # completo (analisi approfondita + aggiornamenti) — non c'è più bisogno
    # di gating, il gating sei tu che decidi se lanciarlo o no.
    deep_mode = True
    updates_mode = True
    log(f"Ora UTC: {utc_hour} — ultima analisi approfondita: {deep_updated} — "
        f"esecuzione manuale: analisi completa + aggiornamenti completi")

    portfolio = load_portfolio_from_previous_dashboard()
    closed = load_closed_from_previous_dashboard()
    processed_ids = load_processed_issue_ids()

    log("Elaborazione issue 'portfolio' in sospeso...")
    portfolio, closed, processed_ids = process_pending_issues(portfolio, closed, processed_ids)

    risk_profile = "PRUDENTE sulle stock picks, MODERATA sulle crypto picks"

    feed_headlines, community_highlights, airdrops_text = [], [], ""

    log("Lettura della sessione passata dal segnalibro...")
    session_json = fetch_and_consume_session_issue()
    if not session_json:
        raise RuntimeError(
            "Nessuna sessione trovata (Issue con etichetta 'sessione-login'). "
            "Devi prima loggarti su Cryptonary nel tuo browser e cliccare il "
            "segnalibro per generarne una fresca, poi confermare la Issue su GitHub "
            "PRIMA di lanciare questa esecuzione."
        )

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        log("Login con la sessione ricevuta...")
        login_with_session(page, session_json)

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

            for item in feed_headlines:
                if item["is_pinned"] and item.get("full_text"):
                    log(f"  Traduzione testo integrale post pinnato ({len(item['full_text'])} caratteri)...")
                    item["full_text_it"] = translate_to_italian(item["full_text"])

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
                "rsiDivergenceBearishShort": ind_data.get("rsi_divergence_bearish_short"),
                "rsiDivergenceBullishShort": ind_data.get("rsi_divergence_bullish_short"),
                "rsiDivergenceBearishMedium": ind_data.get("rsi_divergence_bearish_medium"),
                "rsiDivergenceBullishMedium": ind_data.get("rsi_divergence_bullish_medium"),
                "rsiDivergenceBearishLong": ind_data.get("rsi_divergence_bearish_long"),
                "rsiDivergenceBullishLong": ind_data.get("rsi_divergence_bullish_long"),
                "rsiDivergenceLongUnavailable": ind_data.get("rsi_divergence_long_unavailable", False),
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

    # ---- Avvisi su condizioni rilevanti per le posizioni reali ----
    # (calcolati comunque, ma non più inviati per email: dato che l'esecuzione
    # è manuale, li vedi comunque subito aprendo la dashboard appena generata)
    already_alerted = load_already_alerted()
    new_alerts, already_alerted = check_portfolio_alerts(portfolio, charts_data, already_alerted)
    if new_alerts:
        log(f"Trovati {len(new_alerts)} nuovi avvisi:")
        for a in new_alerts:
            log(f"  - {a['name']} ({a['ticker']}): {a['detail']}")
    else:
        log("Nessun nuovo avviso.")

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
