import json

import pytest

from scripts.analyze import build_report, measure_recovery, percentile95, write_report
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
    text = (out / "summary.md").read_text()
    assert "Media exitosa (ms)" in text and "Retardo: 100 ms" in text
    assert "Ritmo: 1 solicitudes/s" in text and "Resolución aproximada" in text
    assert "media_ms" in (out / "summary.csv").read_text().splitlines()[0]


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


def test_success_during_removal_is_retained_not_replaced():
    samples = [{"ended_ms": 50, "success": False}, {"ended_ms": 95, "success": True},
               {"ended_ms": 150, "success": True}]
    result = measure_recovery(samples, 10, 90, 100, 200)
    assert result["status"] == "during_removal"
    assert result["observed_ms"] == 95
    assert result["offset_from_t0_ms"] == -5
    assert result["value_ms"] is None


def test_success_at_t0_and_missing_failure():
    assert measure_recovery([{"ended_ms": 50, "success": False}, {"ended_ms": 100, "success": True}],
                            10, 90, 100, 200)["value_ms"] == 0
    assert measure_recovery([{"ended_ms": 95, "success": True}],
                            10, 90, 100, 200)["status"] == "no_failure"


def test_transition_recovery_in_report_and_console(tmp_path, capsys):
    path = tmp_path / "synthetic"
    generate(path)
    with (path / "probes.jsonl").open("a") as output:
        output.write(json.dumps({"started_ms": 1_700_000_089_990, "ended_ms": 1_700_000_089_995,
                                 "success": True}) + "\n")
    output = write_report(path)
    result = json.loads((output / "recovery.json").read_text())
    assert result["connectivity"]["status"] == "during_removal"
    assert result["connectivity"]["offset_from_t0_ms"] == -5
    assert "N/A — recuperación observada durante el retiro" in capsys.readouterr().out
    assert "5.00 ms antes de t0" in (output / "summary.md").read_text()


def test_schema2_uses_network_timestamp_not_process_exit(tmp_path):
    path = tmp_path / "synthetic"
    generate(path)
    metadata = json.loads((path / "run.json").read_text())
    metadata["schema"] = 2
    (path / "run.json").write_text(json.dumps(metadata))
    events = [json.loads(line) for line in (path / "events.jsonl").read_text().splitlines()]
    t0 = events[3]["ts_ms"]
    events[3].update(operation_started_ms=t0 - 10, operation_ended_ms=t0, command_finished_ms=t0 + 1000)
    (path / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    *_, summary, warnings = build_report(path)
    assert summary[3]["recuperacion_conectividad_ms"] == 5
    assert summary[3]["recuperacion_aplicacion_ms"] == 126
    events[3]["operation_ended_ms"] = t0 + 1
    (path / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    with pytest.raises(ValueError, match="t0"):
        build_report(path)
