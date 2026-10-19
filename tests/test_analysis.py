import json

import pytest

from scripts.analyze import build_report, percentile95, write_report
from scripts.make_synthetic import generate


def test_errors_are_not_success_latency_and_recoveries_differ(tmp_path):
    path = tmp_path / "synthetic"
    generate(path)
    metadata, phases, bounds, requests, summary, warnings = build_report(path)
    assert metadata["synthetic"]
    assert summary[2]["solicitudes"] == 30
    assert summary[2]["exitosas"] == 0
    assert summary[2]["fallidas"] == 30
    assert summary[2]["media_ms"] is None
    assert summary[2]["p95_ms"] is None
    assert summary[3]["recuperacion_conectividad_ms"] == 5
    assert summary[3]["recuperacion_aplicacion_ms"] == 126
    assert not warnings
    out = write_report(path)
    assert "SINTÉTICOS" in (out / "summary.md").read_text()
    assert "N/A" in (out / "summary.csv").read_text()
    assert (out / "timeline.png").stat().st_size > 1000


def test_p95_requires_enough_samples():
    assert percentile95([]) is None
    assert percentile95([20] * 19) is None
    assert percentile95(list(range(1, 21))) == pytest.approx(19.05)


def test_missing_recovery_is_na_and_dropped_iterations_warn(tmp_path):
    path = tmp_path / "synthetic"
    generate(path)
    rows = [json.loads(line) for line in (path / "k6.jsonl").read_text().splitlines()]
    for row in rows:
        if float(row["data"]["tags"]["started_ms"]) >= 1_700_000_090_000:
            row["data"]["tags"].update(success="false", status="0")
    rows.append({"type": "Point", "metric": "dropped_iterations", "data": {"value": 1}})
    (path / "k6.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    *_, summary, warnings = build_report(path)
    assert summary[3]["recuperacion_aplicacion_ms"] is None
    assert any("descartó" in message for message in warnings)
    assert any("no se observó recuperación" in message for message in warnings)


def test_rejects_incomplete_and_clock_jump(tmp_path):
    path = tmp_path / "synthetic"
    generate(path)
    (path / "run.json").write_text('{"completed": false}')
    with pytest.raises(ValueError, match="incompleta"):
        build_report(path)
    (path / "run.json").write_text('{"completed": true, "synthetic": true}')
    rows = [json.loads(line) for line in (path / "events.jsonl").read_text().splitlines()]
    for index, row in enumerate(rows):
        row["mono_ns"] = index * 30_000_000_000
    rows[-1]["ts_ms"] += 200
    (path / "events.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    with pytest.raises(ValueError, match="reloj"):
        build_report(path)
