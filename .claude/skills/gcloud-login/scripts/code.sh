#!/usr/bin/env bash
# Feed the verification code the user pasted to the login started by login.sh.
set -euo pipefail
code="${1:?usage: code.sh <verification code> [prefix]}"
prefix="${2:-${CLAUDE_SCRATCHPAD:-/tmp}}"
fifo="$prefix/gcloud-login.fifo"
log="$prefix/gcloud-login.log"
[ -p "$fifo" ] || { echo "no login is waiting; run login.sh first" >&2; exit 1; }
echo "$code" > "$fifo"
for _ in $(seq 1 60); do
  if grep -q "You are now logged in\|ERROR" "$log" 2>/dev/null; then
    grep -o -m1 "You are now logged in.*\|ERROR.*" "$log"
    rm -f "$fifo"
    grep -q "You are now logged in" "$log"
    exit $?
  fi
  sleep 1
done
echo "no answer from the login after 60 s; see $log" >&2
exit 1
