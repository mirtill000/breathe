#!/usr/bin/env python3
"""Rilevatore di frequenza respiratoria tramite microfono.

Cattura l'audio da un microfono (es. esterno USB), estrae l'inviluppo del
suono del respiro, individua i singoli atti respiratori e pubblica in tempo
reale i respiri al minuto (BPM) su una pagina web locale.

Uso rapido:
    python3 breathe.py --list-devices
    python3 breathe.py --device 2
    # poi apri http://localhost:8000
"""

from __future__ import annotations

import argparse
import collections
import csv
import json
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
from scipy import ndimage, signal

STATIC_DIR = Path(__file__).resolve().parent / "static"

ENV_RATE = 20  # campioni di inviluppo al secondo


class BreathDetector:
    """Trasforma l'audio grezzo in inviluppo e stima i respiri al minuto."""

    def __init__(self, samplerate, low_hz, high_hz, window_s, min_bpm, max_bpm,
                 sounds_per_breath, sensitivity, min_db):
        self.samplerate = samplerate
        self.block = samplerate // ENV_RATE
        self.sos = signal.butter(4, [low_hz, high_hz], btype="bandpass",
                                 fs=samplerate, output="sos")
        self.zi = signal.sosfilt_zi(self.sos) * 0.0
        self.window_s = window_s
        self.min_bpm = min_bpm
        self.max_bpm = max_bpm
        self.sounds_per_breath = sounds_per_breath
        self.sensitivity = sensitivity
        self.min_db = min_db

        self.lock = threading.Lock()
        self.pending = np.zeros(0, dtype=np.float32)
        self.env = collections.deque(maxlen=int(window_s * ENV_RATE))
        self.t0 = None
        self.n_env = 0  # numero totale di campioni di inviluppo prodotti

    def feed(self, samples: np.ndarray):
        """Aggiunge audio mono (float32). Chiamabile dal thread audio."""
        filtered, self.zi = signal.sosfilt(self.sos, samples, zi=self.zi)
        buf = np.concatenate([self.pending, filtered.astype(np.float32)])
        n = len(buf) // self.block
        if n:
            frames = buf[: n * self.block].reshape(n, self.block)
            rms = np.sqrt(np.mean(frames ** 2, axis=1))
            with self.lock:
                if self.t0 is None:
                    self.t0 = time.time()
                self.env.extend(rms.tolist())
                self.n_env += n
        self.pending = buf[n * self.block:]

    def analyze(self) -> dict:
        with self.lock:
            env = np.array(self.env, dtype=np.float64)
            n_env = self.n_env
        now = n_env / ENV_RATE  # secondi di audio elaborati
        result = {"t": now, "bpm": None, "quality": 0.0, "level": 0.0,
                  "envelope": [], "peaks": [], "status": "in ascolto"}
        if len(env) < 2 * ENV_RATE:
            result["status"] = "raccolta dati..."
            return result

        # Scala logaritmica: rende confrontabili respiri deboli e forti.
        db = 20 * np.log10(env + 1e-9)
        # Smussatura (~0.4 s) per fondere i fruscii di un singolo atto.
        k = max(1, int(0.4 * ENV_RATE))
        smooth = ndimage.uniform_filter1d(db, k, mode="nearest")
        # Rimozione della linea di base lenta (rumore di fondo variabile).
        baseline = ndimage.median_filter(smooth, size=8 * ENV_RATE, mode="nearest")
        x = smooth - baseline

        # Soglia adattiva: frazione della dinamica del segnale, con un minimo
        # assoluto in dB per non scambiare il rumore di fondo per respiri.
        spread = np.percentile(x, 95) - np.percentile(x, 5)
        prominence = max(self.sensitivity * spread, self.min_db)
        min_dist = 60.0 / self.max_bpm / self.sounds_per_breath
        peaks, props = signal.find_peaks(
            x, prominence=prominence, distance=max(1, int(min_dist * ENV_RATE)))

        start_t = now - len(env) / ENV_RATE
        result["level"] = float(db[-ENV_RATE:].mean())
        # Inviluppo sottocampionato (5 Hz) per il grafico della pagina web.
        step = ENV_RATE // 5
        result["envelope"] = [round(float(v), 2) for v in x[::step]]
        result["env_dt"] = step / ENV_RATE
        result["start_t"] = start_t
        result["peaks"] = [round(start_t + p / ENV_RATE, 2) for p in peaks]

        max_gap = 60.0 / self.min_bpm
        if len(peaks) < 3:
            result["status"] = "nessun respiro rilevato"
            return result
        last_peak_age = (len(x) - 1 - peaks[-1]) / ENV_RATE
        if last_peak_age > max_gap * 1.5:
            result["status"] = "respiro assente da %.0f s" % last_peak_age
            return result

        intervals = np.diff(peaks) / ENV_RATE
        intervals = intervals[intervals <= max_gap]
        if len(intervals) < 2:
            result["status"] = "segnale irregolare"
            return result

        period = float(np.median(intervals)) * self.sounds_per_breath
        bpm = 60.0 / period
        # Qualità: regolarità degli intervalli (1 = perfettamente regolare).
        cv = float(np.std(intervals) / (np.mean(intervals) + 1e-9))
        result["bpm"] = round(bpm, 1)
        result["quality"] = round(max(0.0, 1.0 - cv), 2)
        result["status"] = "ok"
        return result


class Broadcaster:
    """Distribuisce gli aggiornamenti a tutti i client SSE connessi."""

    def __init__(self):
        self.clients: list[queue.Queue] = []
        self.lock = threading.Lock()
        self.latest = None

    def subscribe(self):
        q = queue.Queue(maxsize=10)
        with self.lock:
            self.clients.append(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.clients:
                self.clients.remove(q)

    def publish(self, data: dict):
        msg = json.dumps(data)
        with self.lock:
            self.latest = msg
            for q in self.clients:
                try:
                    q.put_nowait(msg)
                except queue.Full:
                    pass


def make_handler(broadcaster: Broadcaster):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                body = (STATIC_DIR / "index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/api/latest":
                body = (broadcaster.latest or "{}").encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/events":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                q = broadcaster.subscribe()
                try:
                    while True:
                        try:
                            msg = q.get(timeout=15)
                            self.wfile.write(f"data: {msg}\n\n".encode())
                        except queue.Empty:
                            self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    broadcaster.unsubscribe(q)
            else:
                self.send_error(404)

    return Handler


def simulate(detector: BreathDetector, bpm: float, samplerate: int, stop: threading.Event):
    """Genera respiri sintetici (rumore modulato) per provare senza microfono."""
    rng = np.random.default_rng()
    chunk = samplerate // 10
    t = 0.0
    while not stop.is_set():
        ts = t + np.arange(chunk) / samplerate
        cur_bpm = bpm * (1 + 0.1 * np.sin(2 * np.pi * ts / 120))
        phase = (ts * cur_bpm / 60.0) % 1.0
        # Espirazione udibile nel primo 40% del ciclo.
        amp = np.where(phase < 0.4, np.sin(np.pi * phase / 0.4) ** 2, 0.0)
        noise = rng.normal(0, 1, chunk)
        detector.feed((0.2 * amp * noise + 0.01 * noise).astype(np.float32))
        t += chunk / samplerate
        time.sleep(chunk / samplerate)


def main():
    p = argparse.ArgumentParser(description="Rileva i respiri al minuto dal microfono "
                                            "e li mostra su una pagina web.")
    p.add_argument("--list-devices", action="store_true", help="elenca i dispositivi audio ed esce")
    p.add_argument("--device", help="indice o nome del microfono (default: quello di sistema)")
    p.add_argument("--samplerate", type=int, default=16000)
    p.add_argument("--port", type=int, default=8000, help="porta HTTP (default 8000)")
    p.add_argument("--host", default="127.0.0.1",
                   help="indirizzo di ascolto; usa 0.0.0.0 per vedere la pagina da altri dispositivi")
    p.add_argument("--low", type=float, default=150.0, help="frequenza minima del filtro (Hz)")
    p.add_argument("--high", type=float, default=2500.0, help="frequenza massima del filtro (Hz)")
    p.add_argument("--window", type=float, default=45.0, help="finestra di analisi in secondi")
    p.add_argument("--min-bpm", type=float, default=4.0)
    p.add_argument("--max-bpm", type=float, default=80.0,
                   help="limite superiore (alzalo per animali piccoli, es. 150)")
    p.add_argument("--sounds-per-breath", type=int, choices=(1, 2), default=1,
                   help="2 se si sentono sia inspirazione che espirazione come suoni separati")
    p.add_argument("--sensitivity", type=float, default=0.35,
                   help="soglia di picco come frazione della dinamica (0-1): più bassa = più sensibile")
    p.add_argument("--min-db", type=float, default=3.0,
                   help="ampiezza minima in dB di un respiro rispetto al fondo")
    p.add_argument("--log", help="salva i valori in un file CSV")
    p.add_argument("--simulate", type=float, metavar="BPM",
                   help="usa respiri sintetici invece del microfono (per test)")
    args = p.parse_args()

    if args.list_devices:
        import sounddevice as sd
        print(sd.query_devices())
        return

    detector = BreathDetector(args.samplerate, args.low, args.high, args.window,
                              args.min_bpm, args.max_bpm, args.sounds_per_breath,
                              args.sensitivity, args.min_db)
    broadcaster = Broadcaster()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(broadcaster))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    shown_host = "localhost" if args.host in ("127.0.0.1", "0.0.0.0") else args.host
    print(f"Pagina web: http://{shown_host}:{args.port}  (Ctrl+C per uscire)")

    stop = threading.Event()
    stream = None
    if args.simulate:
        threading.Thread(target=simulate, args=(detector, args.simulate, args.samplerate, stop),
                         daemon=True).start()
        source = f"simulazione {args.simulate} BPM"
    else:
        import sounddevice as sd
        device = int(args.device) if args.device and args.device.isdigit() else args.device

        def callback(indata, frames, time_info, status):
            if status:
                print(status)
            detector.feed(indata[:, 0].copy())

        stream = sd.InputStream(device=device, channels=1, samplerate=args.samplerate,
                                dtype="float32", blocksize=args.samplerate // 10,
                                callback=callback)
        stream.start()
        source = sd.query_devices(stream.device)["name"]
    print(f"Sorgente audio: {source}")

    log_file = writer = None
    if args.log:
        log_file = open(args.log, "a", newline="")
        writer = csv.writer(log_file)
        if log_file.tell() == 0:
            writer.writerow(["timestamp", "bpm", "quality", "status"])

    try:
        while True:
            time.sleep(1.0)
            r = detector.analyze()
            r["source"] = source
            r["sounds_per_breath"] = args.sounds_per_breath
            broadcaster.publish(r)
            bpm = f"{r['bpm']:5.1f}" if r["bpm"] is not None else "  -- "
            print(f"\rBPM: {bpm}  qualità: {r['quality']:.2f}  stato: {r['status']:<28}",
                  end="", flush=True)
            if writer:
                writer.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), r["bpm"],
                                 r["quality"], r["status"]])
                log_file.flush()
    except KeyboardInterrupt:
        print("\nChiusura...")
    finally:
        stop.set()
        if stream:
            stream.stop()
            stream.close()
        if log_file:
            log_file.close()
        server.shutdown()


if __name__ == "__main__":
    main()
