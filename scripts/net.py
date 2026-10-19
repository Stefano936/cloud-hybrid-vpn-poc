#!/usr/bin/env python3
"""EC2: retardo a PostgreSQL o corte del transporte UDP del WireGuard dedicado."""

import argparse
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import sys
import shutil
import time

IFACE = "wg-poc"
PEER = "10.77.0.2"
STATE_DIR = Path("/run/cloud-hybrid-vpn-poc")
OWNER = STATE_DIR / "qdisc-owned"
COMMENT = "cloud-hybrid-vpn-poc"
PORT = "51820"


def stamp():
    return {"ts_ms": time.time_ns() / 1_000_000, "mono_ns": time.monotonic_ns()}


def check_tools():
    missing = [name for name in ("wg", "ip", "tc", "iptables", "ip6tables") if not shutil.which(name)]
    if missing:
        raise RuntimeError(f"Faltan herramientas: {', '.join(missing)}. Instale wireguard-tools iproute2 iptables")


def run(*args, check=True):
    return subprocess.run(args, check=check, capture_output=True, text=True, timeout=10)


def validate_tunnel():
    run("wg", "show", IFACE)
    addresses = json.loads(run("ip", "-j", "address", "show", "dev", IFACE).stdout)
    if not any(a["local"] == "10.77.0.1" for dev in addresses for a in dev["addr_info"]):
        raise RuntimeError("Ejecutar en EC2: wg-poc debe tener 10.77.0.1")
    route = json.loads(run("ip", "-j", "route", "get", PEER).stdout)
    if route[0].get("dev") != IFACE:
        raise RuntimeError("La ruta al par no pasa por wg-poc")
    allowed = run("wg", "show", IFACE, "allowed-ips").stdout.splitlines()
    if len(allowed) != 1 or allowed[0].split()[1:] != [f"{PEER}/32"]:
        raise RuntimeError("wg-poc debe tener un único peer con AllowedIPs=10.77.0.2/32")
    ports = [line.split() for line in run("wg", "show", "all", "listen-port").stdout.splitlines()]
    if [entry[0] for entry in ports if entry[1] == PORT] != [IFACE]:
        raise RuntimeError("UDP 51820 debe estar reservado exclusivamente a wg-poc; revise ListenPort")
    endpoints = run("wg", "show", IFACE, "endpoints").stdout.splitlines()
    if len(endpoints) != 1 or endpoints[0].split()[0] != allowed[0].split()[0]:
        raise RuntimeError("El endpoint no corresponde al único peer configurado")
    endpoint = endpoints[0].split()[1]
    try:
        host, port = endpoint.rsplit(":", 1)
        address = ipaddress.ip_address(host.strip("[]"))
        if not 1 <= int(port) <= 65535 or address.is_unspecified or address.is_multicast or address.is_loopback:
            raise ValueError()
    except ValueError:
        raise RuntimeError("Peer sin endpoint válido. Inicie un ping desde Linux local y revise wg show") from None
    route = json.loads(run("ip", "-j", "route", "get", str(address)).stdout)
    if route[0].get("dev") == IFACE:
        raise RuntimeError("El endpoint exterior no puede enrutarse por wg-poc")
    return {"interface": IFACE, "listen_port": int(PORT), "endpoint": endpoint}


def roots():
    result = run("tc", "-j", "qdisc", "show", "dev", IFACE, check=False)
    if result.returncode:
        if not Path(f"/sys/class/net/{IFACE}").exists():
            return []
        raise RuntimeError(result.stderr)
    return [q for q in json.loads(result.stdout) if q.get("root")]


def clear_delay():
    current = roots()
    ours = [q for q in current if q.get("handle") == "71:" and q.get("kind") == "prio"]
    if ours:
        if not OWNER.exists():
            raise RuntimeError("qdisc 71: sin marca de propiedad: no se modifica")
        run("tc", "qdisc", "del", "dev", IFACE, "root", "handle", "71:")
    elif OWNER.exists() and any(q.get("kind") != "noqueue" for q in current):
        raise RuntimeError("El qdisc cambió desde la aplicación: no se elimina una regla ajena")
    OWNER.unlink(missing_ok=True)


def apply_delay(delay_ms):
    validate_tunnel()
    clear_delay()
    if any(q.get("kind") != "noqueue" for q in roots()):
        raise RuntimeError("wg-poc ya tiene un qdisc ajeno; se conserva sin modificar")
    # La marca precede a la creación para poder limpiar una aplicación incompleta.
    OWNER.touch(mode=0o600)
    try:
        run("tc", "qdisc", "add", "dev", IFACE, "root", "handle", "71:",
            "prio", "bands", "3", "priomap", *(["0"] * 16))
        run("tc", "qdisc", "add", "dev", IFACE, "parent", "71:3", "handle", "713:",
            "netem", "delay", f"{delay_ms}ms", "limit", "1000")
        run("tc", "filter", "add", "dev", IFACE, "parent", "71:", "protocol", "ip",
            "pref", "10", "flower", "dst_ip", f"{PEER}/32", "ip_proto", "tcp",
            "dst_port", "5432", "classid", "71:3")
    except Exception:
        clear_delay()
        raise


def firewall_rules():
    # Puerto LOCAL reservado: bloquear también si NAT cambia el endpoint del peer.
    # El transporte exterior no circula por wg-poc; no filtrar por esa interfaz.
    return [(tool, chain, ["-p", "udp", flag, PORT, "-m", "comment", "--comment", COMMENT, "-j", "DROP"])
            for tool in ("iptables", "ip6tables")
            for chain, flag in (("INPUT", "--dport"), ("OUTPUT", "--sport"))]


def present(tool, chain, rule):
    result = run(tool, "-w", "3", "-C", chain, *rule, check=False)
    if result.returncode not in (0, 1):
        raise RuntimeError(f"No se puede consultar {tool}; revise permisos/soporte del kernel: {result.stderr}")
    return result.returncode == 0


def remove_block():
    started = stamp()
    last_removal = None
    errors = []
    for tool, chain, rule in firewall_rules():
        try:
            while present(tool, chain, rule):
                run(tool, "-w", "3", "-D", chain, *rule)
                # Antes de verificaciones, mensajes, limpieza o salida del proceso.
                last_removal = stamp()
        except (RuntimeError, OSError, subprocess.SubprocessError) as error:
            errors.append(str(error))
    if errors:
        raise RuntimeError("Retiro incompleto; repita cleanup con sudo: " + "; ".join(errors))
    end = last_removal if last_removal is not None else stamp()
    return {**end, "operation_started_ms": started["ts_ms"],
            "operation_started_mono_ns": started["mono_ns"], "operation_ended_ms": end["ts_ms"],
            "block_removed": last_removal is not None}


def set_block(blocked):
    if not blocked:
        return remove_block()
    validate_tunnel()
    clear_delay()
    try:
        for tool, chain, rule in firewall_rules():
            if not present(tool, chain, rule):
                run(tool, "-w", "3", "-I", chain, "1", *rule)
    except (RuntimeError, OSError, subprocess.SubprocessError):
        # Las reglas son deterministas: también se recuperan tras SIGKILL con cleanup.
        remove_block()
        raise
    return {"blocked": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["delay", "clear-delay", "block", "restore", "cleanup", "status", "check"])
    parser.add_argument("--ms", type=int, default=100, help="Retardo en ms, entero 1..500")
    parser.add_argument("--json", action="store_true", help="Resultado estructurado; restore incluye la marca t0")
    args = parser.parse_args()
    if not 1 <= args.ms <= 500:
        parser.error("--ms debe ser un entero entre 1 y 500")
    if os.geteuid() != 0:
        parser.error("Ejecutar con sudo /usr/bin/python3 scripts/net.py ...")
    check_tools()
    STATE_DIR.mkdir(mode=0o700, exist_ok=True)
    result = {}
    with (STATE_DIR / "lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if args.action == "delay":
            apply_delay(args.ms)
        elif args.action == "clear-delay":
            clear_delay()
        elif args.action == "block":
            result = set_block(True)
        elif args.action == "restore":
            clear_delay()
            result = remove_block()
        elif args.action == "cleanup":
            # Intentar ambas limpiezas incluso si falla una de ellas.
            errors = []
            for operation in (lambda: set_block(False), clear_delay):
                try:
                    operation()
                except Exception as error:
                    errors.append(str(error))
            if errors:
                raise RuntimeError("; ".join(errors))
        else:
            result = validate_tunnel()
            result["qdisc"] = roots()
            result["block_rules"] = sum(present(*rule) for rule in firewall_rules())
            if args.action == "check" and (result["block_rules"] or
                                           any(q.get("kind") != "noqueue" for q in result["qdisc"])):
                raise RuntimeError("Hay alteraciones o qdiscs activos; ejecute cleanup e inspeccione tc antes de iniciar")
        if args.json:
            print(json.dumps({"action": args.action, **result}))
        else:
            print(f"OK: {args.action}")
            if result:
                print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, subprocess.SubprocessError, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
