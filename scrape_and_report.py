"""
Cryptonary Daily Report + Dashboard generator.

Si autentica su cryptonary.com iniettando un refresh_token Supabase in localStorage
(nessuna password, nessun OTP necessario), legge le pick attive (crypto + stock),
chiede a Claude un'analisi in italiano, e genera/aggiorna docs/index.html
(pubblicato via GitHub Pages).

Variabili d'ambiente richieste:
  CRYPTONARY_REFRESH_TOKEN  - refresh token Supabase (estratto manualmente una volta)
  ANTHROPIC_API_KEY         - per la chiamata a Claude
  GH_TOKEN                  - (opzionale) personal access token con scope 'repo' per
                              aggiornare il secret CRYPTONARY_REFRESH_TOKEN se ruota
  GH_REPO                   - "utente/nome-repo", richiesto se si aggiorna il secret
"""

import os
import json
import re
import sys
from datetime import datetime, timezone

from playwright.sync_api import sync_playwright
import anthropic

SUPABASE_PROJECT = "loiogbgrlcppzxshaenr"
AUTH_STORAGE_KEY = f"sb-{SUPABASE_PROJECT}-auth-token"
BASE_URL = "https://cryptonary.com"

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "docs")
DASHBOARD_PATH = os.path.join(OUTPUT_DIR, "index.html")
TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "dashboard_template.html")


def log(msg):
    print(f"[{datetime.now().isoformat()}] {msg}", flush=True)


def inject_session(page, refresh_token):
    """Naviga sul sito e inietta un token di sessione scaduto ma con refresh_token
    valido: il client Supabase del sito lo rileva e si auto-rinnova all'avvio."""
    page.goto(BASE_URL, wait_until="domcontentloaded")
    fake_session = json.dumps({
        "access_token": "",
        "token_type": "bearer",
        "expires_in": 0,
        "expires_at": 0,  # nel passato: forza un refresh immediato
        "refresh_token": refresh_token,
        "user": {},
    })
    page.evaluate(
        "([key, value]) => window.localStorage.setItem(key, value)",
        [AUTH_STORAGE_KEY, fake_session],
    )
    page.reload(wait_until="networkidle")


def extract_rotated_refresh_token(page):
    """Dopo il refresh automatico, Supabase potrebbe aver ruotato il token:
    lo rileggiamo per poterlo salvare per la prossima esecuzione."""
    raw = page.evaluate(
        "(key) => window.localStorage.getItem(key)", AUTH_STORAGE_KEY
    )
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return data.get("refresh_token")
    except (json.JSONDecodeError, AttributeError):
        return None


def scrape_picks(page, url):
    """Estrae le righe della tabella pick (crypto o stock) leggendo il testo
    visibile — robusto a piccoli cambi di stile, fragile a cambi di struttura."""
    page.goto(url, wait_until="networkidle")
    page.wait_for_timeout(1500)

    rows = page.evaluate(
        """
        () => {
            const rows = Array.from(document.querySelectorAll('table tbody tr'));
            return rows.map(row => {
                const cells = Array.from(row.querySelectorAll('td'));
                return cells.map(c => c.innerText.trim());
            }).filter(r => r.length > 0);
        }
        """
    )
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


def call_claude_for_analysis(crypto_rows, stock_rows, headlines, portfolio):
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
  "picks_table_html": "<tr>...</tr> righe HTML per ogni pick con classe 'tag bull/bear/neutral'",
  "portfolio_briefing_html": "<p>...</p> un paragrafo per ogni posizione utente + sintesi finale",
  "updated_prices": {{"TICKER": prezzo_numero, ...}}
}}"""

    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
    )
    text = resp.content[0].text
    text = re.sub(r"^```json\s*|\s*```$", "", text.strip())
    return json.loads(text)


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


def render_dashboard(analysis, portfolio, closed):
    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        html = f.read()

    now_str = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    html = html.replace(
        '<!-- LAST_UPDATED -->non ancora sincronizzato',
        f'<!-- LAST_UPDATED -->{now_str}',
    )
    html = re.sub(
        r"<!-- CRYPTONARY_PICKS_START -->.*?<!-- CRYPTONARY_PICKS_END -->",
        f'<!-- CRYPTONARY_PICKS_START -->\n<div class="table-card"><table class="picks-table">'
        f'<thead><tr><th>Asset</th><th>Prezzo</th><th>RSI</th><th>Giudizio</th></tr></thead>'
        f'<tbody>{analysis["picks_table_html"]}</tbody></table></div>\n'
        f'<!-- CRYPTONARY_PICKS_END -->',
        html,
        flags=re.S,
    )
    html = re.sub(
        r"<!-- AI_BRIEFING_START -->.*?<!-- AI_BRIEFING_END -->",
        f'<!-- AI_BRIEFING_START -->\n<div class="briefing">{analysis["portfolio_briefing_html"]}</div>\n'
        f'<!-- AI_BRIEFING_END -->',
        html,
        flags=re.S,
    )

    updated_prices = analysis.get("updated_prices", {})
    for p in portfolio:
        ticker = p.get("ticker", "").upper()
        if ticker in updated_prices:
            p["current"] = updated_prices[ticker]

    html = re.sub(
        r"__SEED_ACTIVE__ \*/ \[\] /\* __SEED_ACTIVE_END__",
        f"__SEED_ACTIVE__ */ {json.dumps(portfolio, ensure_ascii=False)} /* __SEED_ACTIVE_END__",
        html,
    )
    html = re.sub(
        r"__SEED_CLOSED__ \*/ \[\] /\* __SEED_CLOSED_END__",
        f"__SEED_CLOSED__ */ {json.dumps(closed, ensure_ascii=False)} /* __SEED_CLOSED_END__",
        html,
    )

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(DASHBOARD_PATH, "w", encoding="utf-8") as f:
        f.write(html)
    log(f"Dashboard scritta in {DASHBOARD_PATH}")


def update_github_secret_if_rotated(new_refresh_token, old_refresh_token):
    if not new_refresh_token or new_refresh_token == old_refresh_token:
        log("Refresh token non ruotato, nessun aggiornamento secret necessario.")
        return
    gh_token = os.environ.get("GH_TOKEN")
    gh_repo = os.environ.get("GH_REPO")
    if not gh_token or not gh_repo:
        log("ATTENZIONE: il token è ruotato ma GH_TOKEN/GH_REPO non sono impostati: "
            "il prossimo run FALLIRÀ. Aggiorna il secret manualmente.")
        return

    import requests
    from nacl import encoding, public

    pub_key_resp = requests.get(
        f"https://api.github.com/repos/{gh_repo}/actions/secrets/public-key",
        headers={"Authorization": f"Bearer {gh_token}", "Accept": "application/vnd.github+json"},
    )
    pub_key_resp.raise_for_status()
    key_data = pub_key_resp.json()

    public_key = public.PublicKey(key_data["key"].encode("utf-8"), encoding.Base64Encoder())
    sealed_box = public.SealedBox(public_key)
    encrypted = sealed_box.encrypt(new_refresh_token.encode("utf-8"))
    encrypted_b64 = encoding.Base64Encoder.encode(encrypted).decode("utf-8")

    put_resp = requests.put(
        f"https://api.github.com/repos/{gh_repo}/actions/secrets/CRYPTONARY_REFRESH_TOKEN",
        headers={"Authorization": f"Bearer {gh_token}", "Accept": "application/vnd.github+json"},
        json={"encrypted_value": encrypted_b64, "key_id": key_data["key_id"]},
    )
    put_resp.raise_for_status()
    log("Secret CRYPTONARY_REFRESH_TOKEN aggiornato con il nuovo refresh_token.")


def main():
    refresh_token = os.environ["CRYPTONARY_REFRESH_TOKEN"]
    portfolio = load_portfolio_from_previous_dashboard()
    closed = load_closed_from_previous_dashboard()

    log("Elaborazione issue 'portfolio' in sospeso...")
    portfolio, closed = process_pending_issues(portfolio, closed)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        log("Iniezione sessione...")
        inject_session(page, refresh_token)

        log("Lettura pick crypto...")
        crypto_rows = scrape_picks(page, f"{BASE_URL}/tools/assets-picks")
        log(f"  {len(crypto_rows)} righe trovate")
        log(f"  Esempio: {crypto_rows[:2] if crypto_rows else 'vuoto'}")

        log("Lettura pick stock...")
        stock_rows = scrape_picks(page, f"{BASE_URL}/tools/stock-picks")
        log(f"  {len(stock_rows)} righe trovate")
        log(f"  Esempio: {stock_rows[:2] if stock_rows else 'vuoto'}")

        log("Lettura feed per notizie recenti...")
        headlines = scrape_daily_feed_headlines(page)

        rotated_token = extract_rotated_refresh_token(page)
        browser.close()

    log("Chiamata a Claude per l'analisi...")
    analysis = call_claude_for_analysis(crypto_rows, stock_rows, headlines, portfolio)

    log("Generazione dashboard...")
    render_dashboard(analysis, portfolio, closed)

    update_github_secret_if_rotated(rotated_token, refresh_token)

    log("Fatto.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"ERRORE: {e}")
        sys.exit(1)
