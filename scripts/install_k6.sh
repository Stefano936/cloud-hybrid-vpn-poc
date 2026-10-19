#!/usr/bin/env bash
set -euo pipefail
version=1.0.0
case "$(uname -m)" in
  x86_64) arch=amd64 ;;
  aarch64) arch=arm64 ;;
  *) echo 'Arquitectura no soportada por este instalador' >&2; exit 1 ;;
esac
filename="k6-v${version}-linux-${arch}.tar.gz"
temporary="$(mktemp -d)"
trap 'rm -rf -- "$temporary"' EXIT
cd "$temporary"
base="https://github.com/grafana/k6/releases/download/v${version}"
curl --fail --location --silent --show-error "$base/$filename" -o "$filename"
curl --fail --location --silent --show-error "$base/k6-v${version}-checksums.txt" -o checksums.txt
awk -v name="$filename" '$2 == name { print }' checksums.txt > selected.sha256
test -s selected.sha256
sha256sum --check selected.sha256
tar -xzf "$filename"
sudo install -m 0755 "k6-v${version}-linux-${arch}/k6" /usr/local/bin/k6
k6 version
