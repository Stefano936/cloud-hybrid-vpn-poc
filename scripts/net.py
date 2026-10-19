#!/usr/bin/env python3
"""Ejecutar en EC2. Sólo altera PostgreSQL e ICMP del par dedicado wg-poc."""

import argparse
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys

IFACE = "wg-poc"
PEER = "10.77.0.2"
STATE_DIR = Path("/run/cloud-hybrid-vpn-poc")
OWNER = STATE_DIR / "qdisc-owned"
COMMENT = "cloud-hybrid-vpn-poc"


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
    return [
        ("OUTPUT", ["-o", IFACE, "-d", PEER, "-p", "tcp", "--dport", "5432"]),
        ("INPUT", ["-i", IFACE, "-s", PEER, "-p", "tcp", "--sport", "5432"]),
        ("OUTPUT", ["-o", IFACE, "-d", PEER, "-p", "icmp", "--icmp-type", "echo-request"]),
        ("INPUT", ["-i", IFACE, "-s", PEER, "-p", "icmp", "--icmp-type", "echo-reply"]),
    ]


def set_block(blocked):
    if blocked:
        validate_tunnel()
        clear_delay()  # Nunca combinar el retardo con la interrupción.
    for chain, match in firewall_rules():
        rule = [*match, "-m", "comment", "--comment", COMMENT, "-j", "DROP"]
        existing = run("iptables", "-w", "3", "-C", chain, *rule, check=False)
        if existing.returncode not in (0, 1):
            raise RuntimeError(existing.stderr)
        if blocked and existing.returncode == 1:
            run("iptables", "-w", "3", "-I", chain, "1", *rule)
        if not blocked:
            while existing.returncode == 0:
                run("iptables", "-w", "3", "-D", chain, *rule)
                existing = run("iptables", "-w", "3", "-C", chain, *rule, check=False)
            if existing.returncode != 1:
                raise RuntimeError(existing.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["delay", "clear-delay", "block", "restore", "cleanup", "status"])
    parser.add_argument("--ms", type=int, default=100, help="Retardo en ms, entero 1..500")
    args = parser.parse_args()
    if not 1 <= args.ms <= 500:
        parser.error("--ms debe ser un entero entre 1 y 500")
    if os.geteuid() != 0:
        parser.error("Ejecutar con sudo /usr/bin/python3 scripts/net.py ...")
    STATE_DIR.mkdir(mode=0o700, exist_ok=True)
    with (STATE_DIR / "lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if args.action == "delay":
            apply_delay(args.ms)
        elif args.action == "clear-delay":
            clear_delay()
        elif args.action == "block":
            set_block(True)
        elif args.action == "restore":
            set_block(False)
            clear_delay()
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
            validate_tunnel()
            print(run("tc", "qdisc", "show", "dev", IFACE).stdout, end="")
            for chain in ("INPUT", "OUTPUT"):
                for line in run("iptables", "-S", chain).stdout.splitlines():
                    if COMMENT in line:
                        print(line)
        print(f"OK: {args.action}")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, subprocess.SubprocessError, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
