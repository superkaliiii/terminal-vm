#!/bin/sh
# Installs terminal-linux for the current user: runtime in PREFIX/lib/terminal-linux, launcher in PREFIX/bin.
# No root needed. Usage: sh install.sh [--prefix /absolute/path]   (default prefix: ~/.local)
set -eu
PREFIX="$HOME/.local"
[ "${1:-}" = "--prefix" ] && PREFIX="$2"
case "$PREFIX" in /*) ;; *) echo "prefix must be absolute" >&2; exit 1 ;; esac
SRC=$(cd "$(dirname "$0")" && pwd)
command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }
LIB="$PREFIX/lib/terminal-linux"
mkdir -p "$LIB/skills/terminal-linux" "$PREFIX/bin"
cp "$SRC/terminal_linux.py" "$LIB/"
cp "$SRC/skills/terminal-linux/SKILL.md" "$LIB/skills/terminal-linux/"
printf '#!/bin/sh\nexec python3 "%s/terminal_linux.py" "$@"\n' "$LIB" > "$PREFIX/bin/terminal-linux"
chmod 755 "$PREFIX/bin/terminal-linux"
echo "installed: $PREFIX/bin/terminal-linux"
case ":$PATH:" in *":$PREFIX/bin:"*) ;; *) echo "add $PREFIX/bin to your PATH" ;; esac
"$PREFIX/bin/terminal-linux" doctor || true
