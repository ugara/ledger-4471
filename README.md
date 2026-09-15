# Cryptonary Cloud Report

Automazione 24/7 (cloud, nessun PC acceso richiesto) che ogni giorno legge le pick
di Cryptonary, calcola indicatori tecnici reali, genera un'analisi multi-timeframe
con Claude, e pubblica tutto su un'unica pagina web sempre aggiornata.

## Come funziona il login (importante)

Cryptonary usa un login "passwordless": inserisci solo l'email, ricevi un codice
via email, lo inserisci. Lo script fa lo stesso, ma legge il codice **automaticamente
dalla tua casella Gmail** (non serve una password di Cryptonary, perché non esiste).

Questo è molto più stabile del vecchio approccio a "token di sessione": qui si fa
un login vero e completo ad ogni esecuzione, quindi non c'è nulla che possa scadere
o rompersi in modo imprevedibile come prima.

## Avviso sulla privacy

GitHub Pages gratuito richiede un repository **pubblico**: la pagina finale è
raggiungibile da chiunque conosca l'URL. Ho aggiunto: `noindex` (fuori da Google),
`robots.txt`, e una passphrase locale (deterrente, non vera sicurezza). Le tue
**credenziali** restano sempre private come GitHub Secrets, indipendentemente da
questo.

---

## Setup, passo per passo

### 1. Crea il repository

Repository GitHub **pubblico**, nome poco riconoscibile.

### 2. Abilita le App Password su Gmail

1. Vai su myaccount.google.com → **Sicurezza**
2. Assicurati che la **Verifica in due passaggi** sia attiva (obbligatoria per le App Password)
3. Cerca **"Password per le app"** (o vai direttamente su myaccount.google.com/apppasswords)
4. Crea una nuova app password, dalle un nome (es. "cryptonary-automazione")
5. Copia la password di 16 caratteri generata — **questo è l'unico "segreto" tecnico
   che serve**, non è la tua password Gmail reale

### 3. Configura repository e passphrase nella dashboard

In `dashboard_v2_phase1.html`, sostituisci `GITHUB_REPO` con il tuo, e genera
l'hash della tua passphrase con la Console del browser (vedi commento nel file).

### 4. Carica tutti i file su GitHub

Tutti i file di questo progetto, inclusa la cartella `.github/workflows` e `docs/`.

### 5. Aggiungi i secret al repository

Repository → **Settings** → **Secrets and variables** → **Actions**:

| Nome | Valore |
|---|---|
| `CRYPTONARY_EMAIL` | la tua email usata su Cryptonary (e su Gmail) |
| `GMAIL_APP_PASSWORD` | la password per le app di 16 caratteri del punto 2 |
| `ANTHROPIC_API_KEY` | la tua API key Anthropic (console.anthropic.com) |
| `GH_PAT` | Personal Access Token con permessi Issues (Read/write) e Contents (Read/write) sul repository |

### 6. Attiva GitHub Pages

Settings → Pages → Source: "Deploy from a branch" → `main` / `/docs`.

### 7. Primo run manuale

Actions → "Cryptonary Report" → Run workflow. Controlla i log: il login via OTP
richiede fino a un minuto (deve aspettare che l'email arrivi), è normale.

---

## Come funziona il portafoglio (aggiungi/vendi)

Il bottone "Aggiungi"/"vendi" sulla dashboard apre una GitHub Issue precompilata
con l'etichetta `portfolio`. Lo script la legge, applica la modifica, e la chiude
con un commento di conferma al prossimo run.

## Frequenza e modalità

- **Ogni 6 ore** (01:00, 07:00, 13:00, 19:00 UTC): aggiornamento prezzi/portafoglio/avvisi
- **13:00 UTC** (15:00 ora italiana estiva): anche l'analisi tecnica approfondita completa
  (indicatori, divergenze RSI, macro, multi-timeframe) — una sola volta al giorno per
  contenere il consumo di token dell'API

## Cosa manca ancora (prossimi passi)

Nessun pezzo del piano originale è più mancante — sistema completo:
Dashboard, Grafici e Analisi (con memoria storica), Aggiornamenti, Report
Settimanali/Mensili, avvisi email.

## Limiti onesti da conoscere

- **Il modulo OTP nella pagina di login potrebbe cambiare struttura** nel tempo:
  se il login inizia a fallire, controlla i log — probabile che vada aggiornato
  il selettore dei campi
- **Le crypto molto piccole/nuove** potrebbero non avere abbastanza storico prezzi
  per un'analisi tecnica completa (gestito con un messaggio onesto, non un crash)
- **I costi API Anthropic**: con la ricerca web abilitata per l'analisi approfondita,
  il consumo è più alto del semplice riepilogo — tienilo d'occhio su console.anthropic.com
  nelle prime settimane per farti un'idea del costo mensile reale
