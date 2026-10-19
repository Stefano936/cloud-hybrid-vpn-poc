import io
import json
from types import SimpleNamespace

import pytest

from scripts import run_experiment as runner


def test_missing_load_tool_never_starts_load(monkeypatch):
    monkeypatch.setattr(runner.shutil, "which", lambda name: None if name == "k6" else "/bin/ping")
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *a, **k: pytest.fail("No debe iniciar carga"))
    with pytest.raises(RuntimeError, match="install_k6.sh"):
        runner.preflight({})


def test_api_preflight_checks_actual_products_response(monkeypatch):
    class Response(io.StringIO):
        status = 200
    monkeypatch.setattr(runner, "urlopen", lambda url, **kwargs: Response(json.dumps(
        {"status": "ok"} if url.endswith("healthz") else [])))
    with pytest.raises(RuntimeError, match="PostgreSQL, HBA"):
        runner.check_api()


def test_net_preserves_privileged_restore_timestamp(monkeypatch):
    payload = {"ts_ms": 123, "operation_started_ms": 120, "operation_ended_ms": 123, "block_removed": True}
    monkeypatch.setattr(runner.subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=0, stdout=json.dumps(payload), stderr=""))
    assert runner.net("restore") == payload
