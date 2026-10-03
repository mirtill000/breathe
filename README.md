# breathe — respiri al minuto dal microfono

Script Python per macOS che ascolta un microfono (anche esterno/USB), riconosce
il suono di ogni atto respiratorio di una persona o di un animale e mostra in
tempo reale i **respiri al minuto (BPM)** su una pagina web locale.

## Installazione (macOS)

Serve solo Python 3.9 o successivo (quello di macOS va bene: `python3 --version`).
PortAudio è già incluso nel pacchetto `sounddevice`, quindi Homebrew non serve.

```bash
cd breathe
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

**macOS vecchi (10.9–10.12, es. El Capitan):** l'installer di Python 3.12.10
è compilato per macOS 10.13 e va in crash (`Symbol not found: _getentropy`).
Usa Python **3.11.9**
(<https://www.python.org/ftp/python/3.11.9/python-3.11.9-macos11.pkg>), poi
esegui *Install Certificates.command* nella cartella Applicazioni/Python 3.11,
e installa le dipendenze **solo da pacchetti precompilati** (altrimenti pip
prova a compilare scipy e fallisce):

```bash
pip install --only-binary=:all: -r requirements.txt
```

Il vecchio `git` di El Capitan non accetta i certificati di GitHub: scarica il
progetto come zip con Python:

```bash
python3 -c "import urllib.request; urllib.request.urlretrieve('https://github.com/mirtill000/breathe/archive/refs/heads/claude/breath-rate-detector-macos-wn7wco.zip', 'breathe.zip')"
unzip breathe.zip
```

Se `python3` non c'è, macOS propone di installare gli strumenti da riga di
comando (`xcode-select --install`), oppure usa l'installer di
<https://www.python.org/downloads/macos/>.

> Se Homebrew dà `unknown or unsupported macOS version: :sequoia`, è Homebrew
> a essere vecchio: aggiornalo con `brew update` (o reinstallalo da
> <https://brew.sh>). Per questo progetto comunque non è necessario.

La prima volta macOS chiederà il permesso di usare il microfono per il
Terminale (o iTerm/VS Code): concedilo in
*Impostazioni di Sistema → Privacy e sicurezza → Microfono*.

## Uso

```bash
python3 breathe.py --list-devices      # trova l'indice del microfono esterno
python3 breathe.py --device 2          # avvia con quel microfono
```

Poi apri <http://localhost:8000>. La pagina mostra il valore BPM, la qualità
della stima, il grafico del suono del respiro (i punti rossi sono i respiri
riconosciuti) e l'andamento nel tempo. Si aggiorna ogni secondo.

Prova senza microfono, con respiri sintetici:

```bash
python3 breathe.py --simulate 18
```

### Opzioni utili

| Opzione | Default | Quando cambiarla |
|---|---|---|
| `--device N` | microfono di sistema | per scegliere il microfono esterno |
| `--host 0.0.0.0` | `127.0.0.1` | per vedere la pagina da telefono/altri PC nella stessa rete (`http://<ip-del-mac>:8000`) |
| `--port` | `8000` | se la porta è occupata |
| `--sounds-per-breath 2` | `1` | se si sentono **sia** inspirazione **sia** espirazione: altrimenti il valore risulta doppio |
| `--max-bpm` | `80` | alzalo per animali piccoli (gatti/cani piccoli a riposo 20–40, roditori molto di più) |
| `--min-bpm` | `4` | sotto questo ritmo la stima viene scartata |
| `--sensitivity` | `0.35` | abbassala (es. 0.25) se perde respiri deboli, alzala se conta rumori |
| `--min-db` | `3` | ampiezza minima di un respiro sopra il fondo |
| `--low` / `--high` | `150` / `2500` Hz | banda del filtro: restringila se c'è rumore (es. ventole) |
| `--window` | `45` s | finestra su cui si calcola la media: più lunga = più stabile, più lenta |
| `--log file.csv` | – | salva ogni secondo timestamp, BPM, qualità e stato |

Il valore corrente è disponibile anche come JSON su `/api/latest` e come
stream Server-Sent Events su `/events`, utile per integrarlo altrove.

## Come funziona

1. **Filtro passa-banda** (150–2500 Hz) per tenere il fruscio del respiro e
   togliere ronzii e rumori a bassa frequenza.
2. **Inviluppo**: energia RMS ogni 50 ms, in dB, smussata su ~0,4 s.
3. **Linea di base**: sottrazione della mediana mobile su 8 s, così il rumore
   di fondo che cambia lentamente non conta.
4. **Rilevamento picchi** con soglia adattiva (frazione della dinamica del
   segnale, con un minimo in dB) e distanza minima legata a `--max-bpm`.
5. **BPM** = 60 / mediana degli intervalli tra respiri nella finestra; la
   **qualità** misura la regolarità degli intervalli.

## Consigli pratici

- Metti il microfono vicino a naso/bocca (10–30 cm) o, per un animale che
  dorme, vicino al muso; un microfono a contatto o a clip aiuta molto.
- Ambiente silenzioso: ventilatori, TV e voci disturbano la misura.
- Controlla il grafico: ogni respiro dovrebbe avere **un** punto rosso. Se ne
  vedi due per respiro usa `--sounds-per-breath 2`.
- Non è un dispositivo medico: usalo solo come indicazione.
