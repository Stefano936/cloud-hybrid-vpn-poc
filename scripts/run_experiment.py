#!/usr/bin/env python3
"""Orquestador en EC2: k6, eventos y ping usan el reloj de la misma máquina."""

import argparse
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import threading
import time
from urllib.request import urlopen
from urllib.error import URLError

ROOT = Path(__file__).resolve().parents[1]
PEER = "10.77.0.2"


def stamp():
    return {"ts_ms": time.time_ns() / 1_000_000, "mono_ns": time.monotonic_ns()}


def emit(file, **values):
    file.write(json.dumps(values) + "\n")
    file.flush()


def net(action, *args):
    result = subprocess.run(["sudo", "-n", "/usr/bin/python3", str(ROOT / "scripts/net.py"),
                             action, "--json", *args], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"Falló net.py {action}; revise sudo -v")
    return json.loads(result.stdout)


def say(message):
    print(message, flush=True)


def check_api():
    for endpoint in ("healthz", "products?limit=10"):
        try:
            with urlopen(f"http://127.0.0.1:8000/{endpoint}", timeout=4) as response:
                body = json.load(response)
                valid = body == {"status": "ok"} if endpoint == "healthz" else (
                    isinstance(body, list) and len(body) == 10 and
                    all(isinstance(p, dict) and isinstance(p.get("id"), int) for p in body))
                if response.status != 200 or not valid:
                    raise ValueError("Respuesta inesperada")
        except (URLError, TimeoutError, ValueError) as error:
            hint = ("inicie uvicorn en 127.0.0.1:8000" if endpoint == "healthz" else
                    "revise PostgreSQL, HBA, contraseña .env y la ruta a 10.77.0.2:5432")
            raise RuntimeError(f"Preflight /{endpoint}: {hint}. No se iniciará k6") from error


def preflight(metadata):
    missing = [name for name in ("k6", "ping") if not shutil.which(name)]
    if missing:
        raise RuntimeError(f"Faltan {', '.join(missing)}. Use bash scripts/install_k6.sh y apt-get install iputils-ping")
    metadata["k6_version"] = subprocess.check_output(["k6", "version"], text=True).strip()
    metadata["tunnel"] = net("check")
    # /products consulta realmente PostgreSQL por la IP fija del túnel; no duplicar con psql.
    check_api()
    if not ping()["success"]:
        raise RuntimeError("No responde ping a 10.77.0.2: revise WireGuard y reglas ICMP antes de ejecutar")
    say("Preparación verificada: herramientas, permisos, túnel, API, PostgreSQL e ICMP.")


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
    sudo_ready = False
    metadata = {"schema": 2, "synthetic": False, "config": {
        **{key: value for key, value in vars(args).items() if key != "out"},
        "probe_interval_s": 0.5, "probe_timeout_s": 0.4, "http_timeout_s": 4,
        "delay_direction": "EC2 → PostgreSQL; sólo egreso TCP 5432 por wg-poc",
        "outage_mechanism": "DROP transporte UDP local 51820, entrada y salida, IPv4 e IPv6",
    }}
    try:
        say("Preparación: verificando permisos y retirando alteraciones propias residuales...")
        if not shutil.which("sudo") or subprocess.run(["sudo", "-n", "true"], capture_output=True).returncode:
            raise RuntimeError("Se requiere sudo: ejecute sudo -v en esta terminal antes de iniciar")
        sudo_ready = True
        net("cleanup")
        preflight(metadata)
        with (args.out / "events.jsonl").open("x", encoding="utf-8") as events, \
                (args.out / "k6.log").open("x", encoding="utf-8") as log:
            thread = threading.Thread(target=probe_loop, args=(args.out / "probes.jsonl", stop, errors))
            thread.start()
            emit(events, event="phase", phase="normal", **stamp())
            say(f"[1/4] Normal — {args.normal} s previstos, {args.rate} solicitud(es)/s.")
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
            say(f"[2/4] Latencia — {args.latency} s previstos. +{args.delay_ms} ms sólo EC2 → PostgreSQL (TCP 5432).")
            hold(args.latency)
            before = stamp()
            net("clear-delay")
            emit(events, event="delay_removed", operation_started_ms=before["ts_ms"], **stamp())
            say("Retardo retirado. Iniciando corte del transporte UDP de WireGuard...")
            before = stamp()
            net("block")
            emit(events, event="phase", phase="outage", operation_started_ms=before["ts_ms"], **stamp())
            say(f"[3/4] Túnel interrumpido — {args.outage} s previstos. UDP 51820 bloqueado en ambos sentidos.")
            hold(args.outage)
            before = stamp()
            say("Retirando el bloqueo UDP; se conserva la interfaz WireGuard configurada...")
            restored = net("restore")
            if not restored["block_removed"]:
                raise RuntimeError("El bloqueo desapareció antes de restore; repetir sin cambios externos")
            emit(events, event="phase", phase="restored", command_started_ms=before["ts_ms"],
                 command_finished_ms=stamp()["ts_ms"], **restored)
            say(f"[4/4] Tráfico restablecido — unos {args.restored} s previstos. t0 registrado al retirar la última regla.")
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
            if sudo_ready:
                say("Finalizando: limpieza de reglas UDP y retardo propios...")
                net("cleanup")
                say("Limpieza completada correctamente.")
            else:
                say("No se aplicaron cambios. Si había alteraciones previas: sudo python3 scripts/net.py cleanup")
        except Exception as error:
            completed = False
            metadata["cleanup_error"] = str(error)
            print("LIMPIEZA FALLIDA: ejecutar sudo python3 scripts/net.py cleanup", file=sys.stderr)
        metadata["completed"] = completed
        (args.out / "run.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    if not completed:
        raise RuntimeError("Ejecución incompleta; revisar run.json y limpiar la red")
    say(f"Experimento finalizado. Resultados crudos: {args.out}")
    say(f"Para generar gráficas, cuadro y recuperaciones: python scripts/analyze.py {args.out}")


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit("SIGTERM recibido"))
    try:
        main()
    except (RuntimeError, OSError, subprocess.SubprocessError, KeyboardInterrupt) as error:
        print(f"ERROR: {error or 'Interrumpido por el operador'}. Revise run.json si se creó la carpeta.", file=sys.stderr)
        sys.exit(1)
