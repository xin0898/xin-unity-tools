#!/bin/sh
# Run a plugin Python script with the first working Python 3 (python3, python, py).
# If none works, exit 0 so a missing interpreter never blocks a tool call.
for py in python3 python py; do
  if command -v "$py" >/dev/null 2>&1 && "$py" -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" >/dev/null 2>&1; then
    exec "$py" -I "$@"
  fi
done
exit 0
