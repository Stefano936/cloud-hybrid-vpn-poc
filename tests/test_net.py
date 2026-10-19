"""Pruebas de comandos simulados: no sustituyen una prueba real de WireGuard."""
import json
from types import SimpleNamespace

import pytest

from scripts import net


def fake_firewall(monkeypatch, fail_insert=None, fail_delete=None):
    rules = set()
    calls = []

    def run(tool, *args, check=True):
        calls.append((tool, *args))
        action, chain = args[2:4]
        rule = args[5:] if action == "-I" else args[4:]
        key = (tool, chain, tuple(rule))
        if action == "-I":
            if (tool, chain) == fail_insert:
                raise OSError("inserción simulada fallida")
            rules.add(key)
        if action == "-D":
            if (tool, chain) == run.delete_failure:
                raise OSError("borrado simulado fallido")
            rules.remove(key)
        return SimpleNamespace(returncode=int(action == "-C" and key not in rules), stdout="", stderr="")

    run.delete_failure = fail_delete
    monkeypatch.setattr(net, "run", run)
    monkeypatch.setattr(net, "validate_tunnel", lambda: None)
    monkeypatch.setattr(net, "clear_delay", lambda: calls.append(("clear-delay",)))
    return rules, calls


def test_udp_cut_both_families_idempotent_and_preserves_foreign(monkeypatch):
    rules, calls = fake_firewall(monkeypatch)
    foreign = ("iptables", "OUTPUT", ("foreign-rule",))
    rules.add(foreign)
    net.set_block(True)
    assert calls[0] == ("clear-delay",)
    assert len(rules) == 5
    for tool, chain, rule in net.firewall_rules():
        assert rule[:2] == ["-p", "udp"]
        assert rule[2:4] == ["--dport" if chain == "INPUT" else "--sport", "51820"]
        assert "-i" not in rule and "-o" not in rule
    net.set_block(True)
    assert len(rules) == 5
    result = net.remove_block()
    assert result["block_removed"]
    assert result["operation_started_ms"] <= result["ts_ms"] == result["operation_ended_ms"]
    assert rules == {foreign}
    assert not net.remove_block()["block_removed"]


def test_partial_insert_rolls_back(monkeypatch):
    rules, _ = fake_firewall(monkeypatch, fail_insert=("ip6tables", "INPUT"))
    with pytest.raises(OSError, match="inserción"):
        net.set_block(True)
    assert not rules


def test_partial_restore_attempts_other_rules_then_retry(monkeypatch):
    rules, _ = fake_firewall(monkeypatch, fail_delete=("iptables", "INPUT"))
    net.set_block(True)
    with pytest.raises(RuntimeError, match="Retiro incompleto"):
        net.remove_block()
    assert len(rules) == 1
    assert next(iter(rules))[:2] == ("iptables", "INPUT")
    net.run.delete_failure = None
    net.remove_block()
    assert not rules


def test_timestamp_precedes_auxiliary_checks(monkeypatch):
    _, calls = fake_firewall(monkeypatch)
    net.set_block(True)
    calls.clear()
    clock = []

    def stamp():
        clock.append(len(calls))
        return {"ts_ms": len(calls), "mono_ns": len(calls) * 1_000_000}

    monkeypatch.setattr(net, "stamp", stamp)
    result = net.remove_block()
    last_delete = max(i for i, call in enumerate(calls) if "-D" in call)
    assert result["ts_ms"] == last_delete + 1
    assert result["ts_ms"] < len(calls)  # La verificación posterior no mueve t0.


@pytest.mark.parametrize("problem", ["port", "duplicate", "peer", "endpoint", "route"])
def test_tunnel_validation_rejects_unsafe_topology(monkeypatch, problem):
    def run(*args, **kwargs):
        command = " ".join(args)
        result = ""
        if "address show" in command:
            result = json.dumps([{"addr_info": [{"local": "10.77.0.1"}]}])
        elif "route get" in command:
            dev = "wg-poc" if args[-1] == net.PEER or problem == "route" else "eth0"
            result = json.dumps([{"dev": dev}])
        elif "allowed-ips" in command:
            result = "publickey\t10.77.0.2/32" + ("\nother\t10.77.0.3/32" if problem == "peer" else "")
        elif "listen-port" in command:
            result = "wg-poc\t" + ("51821" if problem == "port" else "51820")
            if problem == "duplicate":
                result += "\nother\t51820"
        elif "endpoints" in command:
            result = "publickey\t" + ("(none)" if problem == "endpoint" else "192.0.2.2:51820")
        return SimpleNamespace(returncode=0, stdout=result, stderr="")
    monkeypatch.setattr(net, "run", run)
    with pytest.raises(RuntimeError):
        net.validate_tunnel()
