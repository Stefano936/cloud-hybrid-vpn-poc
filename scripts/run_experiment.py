#!/usr/bin/env python3
"""Orquestador en EC2: k6, eventos y ping usan el reloj de la misma máquina."""

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
PEER = "10.77.0.2"


def stamp():
    return {"ts_ms": time.time_ns() / 1_000_000, "mono_ns": time.monotonic_ns()}


def emit(file, **values):
    file.write(json.dumps(values) + "\n")
    file.flush()


def net(action, *args):
    subprocess.run(["sudo", "-n", "/usr/bin/python3", str(ROOT / "scripts/net.py"), action, *args], check=True)


def ping():
    start = stamp()
    try:
        result = subprocess.run(["ping", "-n", "-c", "1", "-W", "0.4", PEER],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=0.8)
        success = result.returncode == 0
    except subprocess.TimeoutExpired:
        success = False
    return {"started_ms": start["ts_ms"], "started_mono_ns": start["mono_ns"],
            "ended_ms": time.time_ns() / 1_000_000, "ended_mono_ns": time.monotonic_ns(),
            "success": success}


def probe_loop(path, stop, errors):
    try:
        with path.open("x", encoding="utf-8") as output:
            while not stop.is_set():
                started = time.monotonic()
                emit(output, **ping())
                stop.wait(max(0, 0.5 - (time.monotonic() - started)))
    except Exception as error:
        errors.append(str(error))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for stage, default in [("normal", 30), ("latency", 30), ("outage", 15), ("restored", 30)]:
        parser.add_argument(f"--{stage}", type=int, default=default, help="Duración en segundos (5..300)")
    parser.add_argument("--delay-ms", type=int, default=100)
    parser.add_argument("--rate", type=int, default=1, help="Solicitudes por segundo (1..5)")
    parser.add_argument("--out", type=Path, required=True, help="Directorio NUEVO de resultados")
    args = parser.parse_args()
    durations = [args.normal, args.latency, args.outage, args.restored]
    if any(not 5 <= d <= 300 for d in durations) or not 1 <= args.delay_ms <= 500 or not 1 <= args.rate <= 5:
        parser.error("Duraciones 5..300, retardo 1..500, ritmo 1..5; todos enteros")
    if os.geteuid() == 0:
        parser.error("Ejecutar como usuario normal, luego de sudo -v")
    args.out.mkdir(parents=True, exist_ok=False)
    stop = threading.Event()
    errors = []
    process = None
    thread = None
    completed = False
    metadata = {"schema": 1, "synthetic": False, "config": {
        **{key: value for key, value in vars(args).items() if key != "out"},
        "probe_interval_s": 0.5, "probe_timeout_s": 0.4, "http_timeout_s": 4,
    }}
    try:
        subprocess.run(["sudo", "-n", "true"], check=True)
        metadata["k6_version"] = subprocess.check_output(["k6", "version"], text=True).strip()
        net("cleanup")
        net("status")
        for endpoint in ("healthz", "products?limit=10"):
            with urlopen(f"http://127.0.0.1:8000/{endpoint}", timeout=4) as response:
                if response.status != 200:
                    raise RuntimeError(f"Preflight falló: {endpoint}")
        if not ping()["success"]:
            raise RuntimeError("Preflight: no hay respuesta ICMP de 10.77.0.2")
        with (args.out / "events.jsonl").open("x", encoding="utf-8") as events, \
                (args.out / "k6.log").open("x", encoding="utf-8") as log:
            thread = threading.Thread(target=probe_loop, args=(args.out / "probes.jsonl", stop, errors))
            thread.start()
            emit(events, event="phase", phase="normal", **stamp())
            # Un segundo de margen para las transiciones; k6 finaliza naturalmente.
            env = {**os.environ, "RATE": str(args.rate), "DURATION_S": str(sum(durations) + 1),
                   "K6_NO_USAGE_REPORT": "true"}
            process = subprocess.Popen(["k6", "run", "--no-color", "--out",
                                        f"json={args.out.resolve() / 'k6.jsonl'}",
                                        str(ROOT / "load/products.js")], env=env, stdout=log, stderr=log)

            def hold(seconds):
                deadline = time.monotonic() + seconds
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError(f"k6 terminó antes de tiempo ({process.returncode}); ver k6.log")
                    if errors:
                        raise RuntimeError(f"Falló el muestreo ICMP: {errors}")
                    time.sleep(min(0.2, max(0, deadline - time.monotonic())))

            hold(args.normal)
            before = stamp()
            net("delay", "--ms", str(args.delay_ms))
            emit(events, event="phase", phase="latency", operation_started_ms=before["ts_ms"], **stamp())
            hold(args.latency)
            before = stamp()
            net("clear-delay")
            emit(events, event="delay_removed", operation_started_ms=before["ts_ms"], **stamp())
            before = stamp()
            net("block")
            emit(events, event="phase", phase="outage", operation_started_ms=before["ts_ms"], **stamp())
            hold(args.outage)
            before = stamp()
            net("restore")
            emit(events, event="phase", phase="restored", operation_started_ms=before["ts_ms"], **stamp())
            restored_start = time.monotonic()
            returncode = process.wait(timeout=args.restored + 10)
            emit(events, event="end", **stamp())
            if returncode != 0:
                raise RuntimeError(f"k6 terminó con código {returncode}; ver k6.log")
            if time.monotonic() - restored_start < args.restored - 2:
                raise RuntimeError("Transiciones demasiado lentas: etapa restablecida insuficiente; repetir")
            if errors:
                raise RuntimeError(f"Falló el muestreo ICMP: {errors}")
            completed = True
    except (Exception, KeyboardInterrupt) as error:
        metadata["error"] = str(error) or "Interrumpido por el operador"
        raise
    finally:
        if process is not None and process.poll() is None:
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=12)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        stop.set()
        if thread is not None:
            thread.join(timeout=2)
        try:
            net("cleanup")
        except Exception as error:
            completed = False
            metadata["cleanup_error"] = str(error)
            print("LIMPIEZA FALLIDA: ejecutar sudo python3 scripts/net.py cleanup", file=sys.stderr)
        metadata["completed"] = completed
        (args.out / "run.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    if not completed:
        raise RuntimeError("Ejecución incompleta; revisar run.json y limpiar la red")
    print(f"Resultados crudos: {args.out}")


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit("SIGTERM recibido"))
    main()
