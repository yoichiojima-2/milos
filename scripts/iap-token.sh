#!/usr/bin/env sh
# Print a self-signed JWT that IAP accepts for the public API, acting as a
# service account the caller may sign for (roles/iam.serviceAccountTokenCreator).
# Usage: MILOS_ID_TOKEN=$(scripts/iap-token.sh <service-account-email> <public-url>)
# IAP on Cloud Run matches the audience against the service URL with a path
# wildcard, so "<public-url>/*" is what gets signed.
set -eu
sa="$1"; aud="${2%/}/*"
now=$(date +%s)
claims=$(mktemp); out=$(mktemp)
trap 'rm -f "$claims" "$out"' EXIT
printf '{"iss":"%s","sub":"%s","email":"%s","aud":"%s","iat":%s,"exp":%s}\n' "$sa" "$sa" "$sa" "$aud" "$now" $((now + 3600)) > "$claims"
gcloud iam service-accounts sign-jwt --iam-account="$sa" "$claims" "$out" >/dev/null 2>&1
cat "$out"
