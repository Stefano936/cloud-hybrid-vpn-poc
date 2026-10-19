#!/usr/bin/env python3
"""Fixture determinista para validar gráficas; nunca es evidencia del laboratorio."""

import argparse
import json
from pathlib import Path


def generate(directory):
    directory.mkdir(parents=True, exist_ok=False)
    base = 1_700_000_000_000
    phases = ["normal", "latency", "outage", "restored"]
    events = [{"event": "phase", "phase": p, "ts_ms": base + i * 30_000} for i, p in enumerate(phases)]
    events[3].update(operation_started_ms=base + 89_990, operation_ended_ms=base + 90_000)
    events.append({"event": "end", "ts_ms": base + 120_000})
    points, probes = [], []
    for n in range(120):
        start = base + n * 1000 + 100
        success = not 60 <= n < 90
        duration = 2000 if not success else (420 if 30 <= n < 60 else 20) + n % 7
        points.append({"type": "Point", "metric": "product_latency_ms", "data": {
            "value": duration, "tags": {"started_ms": str(start), "ended_ms": str(start + duration),
                                       "success": str(success).lower(), "status": "200" if success else "503"}}})
    for n in range(240):
        start = base + n * 500
        success = not 120 <= n < 180
        probes.append({"started_ms": start, "ended_ms": start + (5 if success else 400), "success": success})
    for name, rows in [("events.jsonl", events), ("k6.jsonl", points), ("probes.jsonl", probes)]:
        (directory / name).write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    (directory / "run.json").write_text(json.dumps({"schema": 2, "completed": True, "synthetic": True,
                                                   "config": {"delay_ms": 100, "rate": 1, "probe_interval_s": 0.5}}), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("examples/synthetic/output"))
    generate(parser.parse_args().out)
