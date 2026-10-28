#!/bin/sh
# Like pyrun.sh, but FAILS CLOSED: used for guards that must not let a call through when they cannot run
# (the broker's Play/scene MCP guard). No Python, or the script crashing (exit other than 0/2), blocks the call.
for py in python3 python py; do
  if command -v "$py" >/dev/null 2>&1 && "$py" -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" >/dev/null 2>&1; then
    "$py" -I "$@"
    rc=$?
    if [ "$rc" -eq 0 ] || [ "$rc" -eq 2 ]; then
      exit "$rc"
    fi
    echo "Blocked by unity-job-guard: its Play/scene guard failed (exit $rc), so this Unity call is blocked to be safe. Use ujob (lease play / test / compile) instead." >&2
    exit 2
  fi
done
echo "Blocked by unity-job-guard: no Python 3 found to run its Play/scene guard, so this Unity call is blocked to be safe. Use ujob instead." >&2
exit 2
