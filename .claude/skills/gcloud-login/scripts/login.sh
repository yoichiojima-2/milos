#!/usr/bin/env bash
# Start a no-browser gcloud login that keeps waiting for the verification code,
# and print the sign-in URL. Pair with code.sh once the user pastes the code.
set -euo pipefail
prefix="${1:-${CLAUDE_SCRATCHPAD:-/tmp}}"
gcloud="$prefix/gcloud"
[ -x "$gcloud" ] || { echo "run install.sh first" >&2; exit 1; }
fifo="$prefix/gcloud-login.fifo"
log="$prefix/gcloud-login.log"
rm -f "$fifo" "$log"
mkfifo "$fifo"
# 0<> opens the fifo read-write so the process starts without a writer and
# survives until code.sh writes into it; setsid detaches it from this shell.
setsid nohup "$gcloud" auth login --no-launch-browser 0<>"$fifo" >"$log" 2>&1 &
for _ in $(seq 1 60); do
  if grep -q "https://accounts.google.com" "$log" 2>/dev/null; then
    grep -o "https://accounts.google.com[^ ]*" "$log" | head -1
    exit 0
  fi
  sleep 1
done
echo "no sign-in URL after 60 s; see $log" >&2
exit 1
