#!/bin/sh
# Installs terminal-vm for the current user: runtime in PREFIX/lib/terminal-vm, launcher in PREFIX/bin.
# No root needed. Usage: sh install.sh [--prefix /absolute/path]   (default prefix: ~/.local)
set -eu
PREFIX="$HOME/.local"
[ "${1:-}" = "--prefix" ] && PREFIX="$2"
case "$PREFIX" in /*) ;; *) echo "prefix must be absolute" >&2; exit 1 ;; esac
SRC=$(cd "$(dirname "$0")" && pwd)
command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }
LIB="$PREFIX/lib/terminal-vm"
mkdir -p "$LIB/skills/terminal-vm" "$PREFIX/bin"
cp "$SRC/terminal_vm.py" "$LIB/"
cp "$SRC/skills/terminal-vm/SKILL.md" "$LIB/skills/terminal-vm/"
printf '#!/bin/sh\nexec python3 "%s/terminal_vm.py" "$@"\n' "$LIB" > "$PREFIX/bin/terminal-vm"
chmod 755 "$PREFIX/bin/terminal-vm"
echo "installed: $PREFIX/bin/terminal-vm"
case ":$PATH:" in *":$PREFIX/bin:"*) ;; *) echo "add $PREFIX/bin to your PATH" ;; esac
"$PREFIX/bin/terminal-vm" doctor || true
