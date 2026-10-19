#!/usr/bin/env python3
"""Convierte los archivos crudos en tablas y figuras; no completa datos ausentes."""

import argparse
import csv
import json
import math
from pathlib import Path
import statistics

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

PHASES = ["normal", "latency", "outage", "restored"]
LABELS = ["Normal", "Latencia añadida", "VPN interrumpida", "VPN restablecida"]
COLORS = ["#d9ead3", "#fff2cc", "#f4cccc", "#cfe2f3"]
HEADERS = ["Etapa", "Solicitudes", "Exitosas", "Fallidas", "Media exitosa (ms)", "p95 exitoso (ms)",
           "Recuperación de conectividad (ms)", "Recuperación de aplicación (ms)"]


def jsonl(path):
    with path.open(encoding="utf-8") as file:
        for number, line in enumerate(file, 1):
            if line.strip():
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as error:
                    raise ValueError(f"JSON inválido: {path.name}, línea {number}") from error


def percentile95(values):
    if len(values) < 20:
        return None
    values = sorted(values)
    position = (len(values) - 1) * 0.95
    low, high = math.floor(position), math.ceil(position)
    return values[low] + (values[high] - values[low]) * (position - low)


def fmt(value):
    if value is None:
        return "N/A"
    return f"{value:.2f}" if isinstance(value, float) else str(value)


def measure_recovery(samples, outage, transition_start, t0, end):
    if not any(not s["success"] and outage <= s["ended_ms"] < transition_start for s in samples):
        return {"value_ms": None, "status": "no_failure", "explanation": "no se observó una falla durante el corte"}
    successes = [s["ended_ms"] for s in samples if s["success"] and transition_start <= s["ended_ms"] < end]
    if not successes:
        return {"value_ms": None, "status": "not_recovered", "explanation": "no se observó recuperación antes del fin"}
    observed = min(successes)
    offset = observed - t0
    if observed < t0:
        return {"value_ms": None, "status": "during_removal", "observed_ms": observed, "offset_from_t0_ms": offset,
                "explanation": f"recuperación observada durante el retiro, {-offset:.2f} ms antes de t0; no es un tiempo posterior a t0"}
    return {"value_ms": offset, "status": "after_removal", "observed_ms": observed,
            "offset_from_t0_ms": offset, "explanation": "primera finalización exitosa desde t0"}


def recovery_text(detail):
    if detail["value_ms"] is None:
        return "N/A — " + detail["explanation"]
    return f"{detail['value_ms']:.2f} ms"


def build_report(directory):
    metadata = json.loads((directory / "run.json").read_text(encoding="utf-8"))
    if not metadata.get("completed"):
        raise ValueError("Ejecución incompleta: conservar crudos y repetir; revisar run.json")
    events = list(jsonl(directory / "events.jsonl"))
    phases = [e for e in events if e["event"] == "phase"]
    ends = [e for e in events if e["event"] == "end"]
    if [e["phase"] for e in phases] != PHASES or len(ends) != 1:
        raise ValueError("Se requieren cuatro etapas ordenadas y un único evento end")
    boundaries = [e["ts_ms"] for e in phases] + [ends[0]["ts_ms"]]
    if any(b <= a for a, b in zip(boundaries, boundaries[1:])):
        raise ValueError("Timestamps de etapas no crecientes")
    if all("mono_ns" in event for event in [*phases, ends[0]]):
        first = phases[0]
        for event in [*phases, ends[0]]:
            wall = event["ts_ms"] - first["ts_ms"]
            mono = (event["mono_ns"] - first["mono_ns"]) / 1_000_000
            if abs(wall - mono) > 100:
                raise ValueError("El reloj de pared saltó >100 ms; repetir la medición")

    requests = []
    dropped = 0
    for item in jsonl(directory / "k6.jsonl"):
        if item.get("type") != "Point":
            continue
        if item.get("metric") == "dropped_iterations":
            dropped += item["data"]["value"]
        if item.get("metric") != "product_latency_ms":
            continue
        data = item["data"]
        tags = data["tags"]
        start, end = float(tags["started_ms"]), float(tags["ended_ms"])
        duration = float(data["value"])
        if not all(math.isfinite(v) for v in (start, end, duration)) or end < start or duration < 0:
            raise ValueError("Muestra con timestamps o duración inválidos")
        if tags["success"] not in ("true", "false"):
            raise ValueError("Muestra sin indicador de éxito válido")
        phase = next((i for i in range(4) if boundaries[i] <= start < boundaries[i + 1]), None)
        if phase is None:
            continue
        requests.append({"phase": PHASES[phase], "started_ms": start, "ended_ms": end,
                         "latency_ms": duration,
                         "success": tags["success"] == "true" and tags["status"] == "200",
                         "status": tags["status"], "crosses_phase": end >= boundaries[phase + 1]})
    requests.sort(key=lambda r: r["started_ms"])
    probes = list(jsonl(directory / "probes.jsonl"))
    restore, end = boundaries[3:]
    transition_start = phases[3].get("operation_started_ms", restore)
    if not boundaries[2] <= transition_start <= restore:
        raise ValueError("Intervalo de retiro del bloqueo inválido")
    if metadata.get("schema", 1) >= 2 and phases[3].get("operation_ended_ms") != restore:
        raise ValueError("t0 debe coincidir con el fin del retiro efectivo del bloqueo")
    outage = boundaries[2]
    warnings = []
    if dropped:
        warnings.append(f"k6 descartó {dropped:g} iteraciones: el ritmo no se sostuvo; repetir la corrida.")

    metadata["recovery"] = {
        "connectivity": measure_recovery(probes, outage, transition_start, restore, end),
        "application": measure_recovery(requests, outage, transition_start, restore, end),
    }
    for key, label in [("connectivity", "Conectividad"), ("application", "Aplicación")]:
        detail = metadata["recovery"][key]
        if detail["value_ms"] is None:
            warnings.append(f"{label}: {recovery_text(detail)}.")
    connectivity = metadata["recovery"]["connectivity"]["value_ms"]
    application = metadata["recovery"]["application"]["value_ms"]
    summary = []
    for phase, label in zip(PHASES, LABELS):
        if phase == "outage" and metadata.get("schema", 1) >= 2:
            label += " (UDP)"
        rows = [r for r in requests if r["phase"] == phase]
        values = [r["latency_ms"] for r in rows if r["success"]]
        summary.append({"etapa": label, "solicitudes": len(rows), "exitosas": len(values),
                        "fallidas": len(rows) - len(values),
                        "media_ms": statistics.mean(values) if values else None,
                        "p95_ms": percentile95(values),
                        "recuperacion_conectividad_ms": connectivity if phase == "restored" else None,
                        "recuperacion_aplicacion_ms": application if phase == "restored" else None})
    if not requests:
        warnings.append("No hay consultas registradas dentro del intervalo del experimento.")
    return metadata, phases, boundaries, requests, summary, warnings


def write_report(directory):
    metadata, phases, boundaries, requests, summary, warnings = build_report(directory)
    output = directory / "report"
    output.mkdir(exist_ok=True)
    synthetic = metadata.get("synthetic", False)
    title = "DATOS SINTÉTICOS — NO SON RESULTADOS EXPERIMENTALES" if synthetic else "Mediciones del laboratorio"
    config = metadata.get("config", {})
    delay, rate = config.get("delay_ms"), config.get("rate")
    settings = f"Retardo: {fmt(delay)} ms, sólo EC2 → PostgreSQL (TCP 5432). Ritmo: {fmt(rate)} solicitudes/s."
    interval = 1000 / rate if isinstance(rate, (int, float)) and rate > 0 else None
    resolution = (f"Resolución aproximada: ping {fmt(config.get('probe_interval_s', 0.5) * 1000)} ms; "
                  f"consultas {fmt(interval)} ms, más respuesta y planificación.")
    recoveries = metadata["recovery"]
    recovery_lines = [f"Recuperación de conectividad: {recovery_text(recoveries['connectivity'])}",
                      f"Recuperación de aplicación: {recovery_text(recoveries['application'])}"]
    (output / "recovery.json").write_text(json.dumps(recoveries, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    with (output / "summary.csv").open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=summary[0].keys())
        writer.writeheader()
        writer.writerows({key: fmt(value) for key, value in row.items()} for row in summary)
    with (output / "requests.csv").open("w", newline="", encoding="utf-8") as file:
        columns = ["phase", "started_ms", "ended_ms", "latency_ms", "success", "status", "crosses_phase"]
        writer = csv.DictWriter(file, fieldnames=columns)
        writer.writeheader()
        writer.writerows(requests)
    columns = list(summary[0])
    markdown = [f"# {title}", "", settings, "", resolution, "", "Todas las duraciones están expresadas en milisegundos.", "",
                "| " + " | ".join(HEADERS) + " |", "| " + " | ".join(["---"] * len(columns)) + " |"]
    markdown.extend("| " + " | ".join(fmt(row[c]) for c in columns) + " |" for row in summary)
    markdown += ["", "Media: sólo éxitos. p95: interpolación lineal, N/A con menos de 20 éxitos.",
                 "Etapa asignada por inicio de consulta; requests.csv marca las que cruzan una frontera.",
                 "Recuperación: primera finalización exitosa desde el inicio del retiro, incluso si la solicitud estaba en curso.",
                 "Si termina antes de t0, se informa N/A y el adelanto observado: no se sustituye por un éxito posterior ni por cero.",
                 *recovery_lines,
                 "Abrir una conexión PostgreSQL por consulta puede acumular el retardo en varios intercambios.",
                 "", "## Calidad de la medición", ""]
    markdown.extend(f"- {warning}" for warning in warnings)
    if not warnings:
        markdown.append("Sin advertencias automáticas; revisar igualmente la gráfica y los crudos.")
    restore = phases[3]
    if "operation_started_ms" in restore:
        markdown.append(f"\nVentana de retiro: {restore['operation_started_ms']:.3f} a {restore['ts_ms']:.3f} ms Unix "
                        f"({restore['ts_ms'] - restore['operation_started_ms']:.2f} ms). "
                        "t0 es la marca inmediata posterior a la última eliminación de regla, antes de tareas auxiliares "
                        "en corridas de esquema 2. En esquema 1 corresponde al fin del comando original.")
    markdown += ["", "![Serie temporal](timeline.png)", "", "![Comparación](comparison.png)", ""]
    (output / "summary.md").write_text("\n".join(markdown), encoding="utf-8")

    plt.rcParams.update({"font.size": 10, "axes.spines.top": False, "axes.spines.right": False})
    fig, ax = plt.subplots(figsize=(12, 5), layout="constrained")
    origin = boundaries[0]
    labels = [row["etapa"] for row in summary]
    for i, (label, color) in enumerate(zip(labels, COLORS)):
        left, right = (boundaries[i] - origin) / 1000, (boundaries[i + 1] - origin) / 1000
        ax.axvspan(left, right, alpha=0.55, color=color)
        ax.text((left + right) / 2, 1.02, label, transform=ax.get_xaxis_transform(), ha="center", fontsize=9)
    for success, color, marker, label in [(True, "#16537e", "o", "Éxito"), (False, "#b42318", "x", "Falla / timeout")]:
        selected = [r for r in requests if r["success"] == success]
        ax.scatter([(r["started_ms"] - origin) / 1000 for r in selected],
                   [r["latency_ms"] for r in selected], c=color, marker=marker, s=22, label=label)
    ax.set(xlabel="Inicio de la consulta (s desde el inicio)", ylabel="Tiempo de respuesta observado (ms)",
           xlim=(0, (boundaries[-1] - origin) / 1000), ylim=(0, None))
    ax.legend(loc="upper left")
    ax.grid(axis="y", alpha=0.2)
    fig.suptitle(title + "\n" + settings, fontsize=11, y=1.14)
    footer = "Media/p95: sólo solicitudes exitosas. " + resolution + "\n" + "\n".join(recovery_lines)
    fig.text(0.5, -0.04, footer, ha="center", va="top", fontsize=9, wrap=True)
    fig.savefig(output / "timeline.png", dpi=160, bbox_inches="tight")
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(10, 5), layout="constrained")
    for offset, key, label, color in [(-0.18, "media_ms", "Media", "#16537e"), (0.18, "p95_ms", "p95", "#70adbd")]:
        for i, row in enumerate(summary):
            value = row[key]
            ax.bar(i + offset, value if value is not None else 0, width=0.34,
                   color=color, label=label if i == 0 else None)
            ax.text(i + offset, value or 0, fmt(value), ha="center", va="bottom", fontsize=8)
    ax.set_xticks(range(4), labels)
    ax.set(ylabel="Tiempo de respuesta (ms), sólo éxitos", ylim=(0, None))
    ax.margins(y=0.2)
    ax.legend()
    ax.grid(axis="y", alpha=0.2)
    fig.suptitle(title + "\n" + settings, fontsize=10)
    fig.text(0.5, -0.04, footer, ha="center", va="top", fontsize=9, wrap=True)
    fig.savefig(output / "comparison.png", dpi=160, bbox_inches="tight")
    plt.close(fig)
    print("\n".join(recovery_lines))
    print(resolution)
    for warning in warnings:
        print(f"Aviso: {warning}")
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    try:
        print(write_report(args.directory))
    except (ValueError, KeyError, OSError) as error:
        parser.exit(1, f"No se puede generar un informe válido: {error}\n")
