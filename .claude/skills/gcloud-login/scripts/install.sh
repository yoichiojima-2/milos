#!/usr/bin/env bash
# Install the Google Cloud SDK into the session scratchpad (or $1) and print its bin directory.
# Idempotent: a present install is reused. Nothing here needs credentials.
set -euo pipefail
prefix="${1:-${CLAUDE_SCRATCHPAD:-/tmp}}"
sdk="$prefix/google-cloud-sdk"
if [ ! -x "$sdk/bin/gcloud" ]; then
  mkdir -p "$prefix"
  tarball="$prefix/google-cloud-cli.tar.gz"
  curl -sS --fail -m 600 -o "$tarball" \
    https://dl.google.com/dl/cloudsdk/channels/rapid/downloads/google-cloud-cli-linux-x86_64.tar.gz
  tar -xzf "$tarball" -C "$prefix"
  rm -f "$tarball"
fi
# The container's placeholder access token breaks gcloud; the wrapper strips it.
cat > "$prefix/gcloud" <<WRAP
#!/usr/bin/env bash
exec env -u CLOUDSDK_AUTH_ACCESS_TOKEN "$sdk/bin/gcloud" "\$@"
WRAP
chmod +x "$prefix/gcloud"
"$prefix/gcloud" --version | head -1 >&2
echo "$sdk/bin"
echo "wrapper: $prefix/gcloud" >&2
