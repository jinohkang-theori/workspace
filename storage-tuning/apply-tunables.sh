#!/bin/sh
# Compatibility shim: the tuning logic now lives in apply-tunables.py, which detects
# the storage stack instead of assuming loop3/loop4/csfs. Kept so existing callers
# (devcontainer postStartCommand etc.) keep working. Never fails the caller.
here=$(dirname "$(readlink -f "$0" 2>/dev/null || echo "$0")")
if command -v python3 >/dev/null 2>&1; then
  exec python3 "$here/apply-tunables.py" "$@"
fi
echo "[apply-tunables] python3 not found; storage tunables not applied" >&2
exit 0
