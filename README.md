# Cryptonary Cloud Report

Automazione 24/7 (cloud, nessun PC acceso richiesto) che ogni giorno legge le pick
di Cryptonary, incrocia il tuo portafoglio reale, genera un'analisi con Claude, e
pubblica tutto su un'unica pagina web sempre aggiornata.

## Avviso sulla privacy

GitHub Pages gratuito richiede un repository **pubblico**: la pagina finale è
raggiungibile da chiunque conosca l'URL (nessun login richiesto da GitHub). Ho
aggiunto tre livelli di protezione pratica, nessuno dei quali è "vera" sicurezza:
- `noindex` per tenerla fuori da Google
- `robots.txt` per scoraggiare i crawler
- una passphrase locale (deterrente contro chi trova l'URL per caso, non contro un
  attaccante motivato — chiunque può leggere il codice sorgente della pagina)

Le tue **credenziali** (refresh token, API key) restano sempre private come GitHub
Secrets, indipendentemente da questo: non sono mai nel codice del repository.

Se in futuro vuoi vera privacy sulla pagina, l'unica strada è passare a GitHub Pro
(~4$/mese) e rendere il repository privato.

---

## Setup, passo per passo

### 1. Crea il repository

Su github.com, crea un nuovo repository **pubblico**. Dagli un nome poco
riconoscibile (non "cryptonary-portfolio-riccardo" — meglio qualcosa di anonimo),
per rendere l'URL meno ovvio da indovinare/associare a te.

Carica tutti i file di questo progetto nel repository (via web upload, o `git push`
se hai familiarità con git).

### 2. Genera l'hash della tua passphrase

Apri la console del browser (F12 → Console) su una pagina qualsiasi e scrivi:

```js
crypto.subtle.digest('SHA-256', new TextEncoder().encode('LA-TUA-PASSPHRASE'))
  .then(buf => console.log(Array.from(new Uint8Array(buf)).map(b => b.toString(16).padStart(2,'0')).join('')))
```

Copia la stringa che esce e sostituiscila in `dashboard_template.html`, alla riga:
```js
const PASSPHRASE_HASH = "REPLACE_WITH_YOUR_HASH";
```

### 3. Configura il nome del repository nella dashboard

In `dashboard_template.html`, sostituisci:
```js
const GITHUB_REPO = "REPLACE_ME/cryptonary-cloud";
```
con `"tuoutente/nome-del-tuo-repo"`.

### 4. Recupera il refresh token (unica cosa che devi fare tu manualmente)

1. Su Cryptonary, loggato, apri DevTools (F12) → tab **Application** → **Local
   Storage** → `cryptonary.com`
2. Trova la chiave che inizia con `sb-` e finisce con `-auth-token`
3. Apri il valore (è un JSON), copia solo il campo `"refresh_token"`

### 5. Crea un GitHub Personal Access Token (per aggiornare secret e issue)

1. Su GitHub → foto profilo → **Settings** → **Developer settings** →
   **Personal access tokens** → **Fine-grained tokens** → **Generate new token**
2. Dagli accesso solo al repository che hai creato, con permessi: **Secrets**
   (Read/Write), **Issues** (Read/Write), **Contents** (Read/Write)
3. Copia il token generato (comincia con `github_pat_...`)

### 6. Aggiungi i secret al repository

Nel repository → **Settings** → **Secrets and variables** → **Actions** →
**New repository secret**. Aggiungi:

| Nome | Valore |
|---|---|
| `CRYPTONARY_REFRESH_TOKEN` | il refresh_token del punto 4 |
| `ANTHROPIC_API_KEY` | la tua API key Anthropic (console.anthropic.com) |
| `GH_PAT` | il personal access token del punto 5 |

### 7. Attiva GitHub Pages

Repository → **Settings** → **Pages** → sotto "Build and deployment", **Source**:
"Deploy from a branch" → branch `main`, cartella `/docs` → **Save**.

L'URL della tua pagina sarà simile a `https://tuoutente.github.io/nome-repo/` —
lo trovi scritto in quella stessa pagina di impostazioni dopo il salvataggio.

### 8. Primo run manuale

Repository → tab **Actions** → seleziona il workflow "Cryptonary Daily Report" →
**Run workflow** → **Run workflow**. Aspetta 2-5 minuti, poi controlla i log: se
qualcosa fallisce (probabile al primo tentativo — i selettori di scraping possono
aver bisogno di un aggiustamento), il log ti dice dove.

Da qui in poi girerà da solo ogni giorno alle 7:00 UTC. Puoi anche lanciarlo a mano
in qualsiasi momento dalla stessa schermata "Actions" → "Run workflow".

---

## Come funziona il portafoglio (aggiungi/vendi)

Sulla dashboard pubblicata, il bottone "Aggiungi"/"vendi" apre una nuova scheda
GitHub con una Issue precompilata nel tuo repository. Basta cliccare **Submit new
issue** (sei già loggato su GitHub, è il tuo repository). Al prossimo run
automatico (o lanciandolo a mano da Actions se non vuoi aspettare), lo script:
1. Legge le issue aperte con etichetta `portfolio`
2. Applica l'aggiunta/vendita/rimozione
3. Chiude la issue con un commento di conferma

## Limiti onesti da conoscere

- **I selettori di scraping potrebbero rompersi** se Cryptonary cambia il design
  del sito. Se il report smette di aggiornarsi, controlla i log in Actions.
- **Il refresh_token può scadere o essere revocato** (es. se cambi password, fai
  logout ovunque, o Cryptonary invalida le sessioni per sicurezza). Se lo script
  inizia a fallire nel login, dovrai ripetere il punto 4.
- **Il report settimanale e mensile non sono ancora inclusi in questa versione
  cloud** — questo script copre il report giornaliero + dashboard. Se vuoi
  portare anche quelli sul cloud, si estende lo stesso schema (fammi sapere).
