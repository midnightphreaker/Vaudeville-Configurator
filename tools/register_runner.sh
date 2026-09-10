#!/usr/bin/env bash
# Register a Forgejo act_runner so .forgejo/workflows/release.yml can execute.
#   VLM_RUNNER_TOKEN=*** bash tools/register_runner.sh linux|windows
# The token is read from the environment or stdin and is never logged.
# Get it: repo Settings -> Actions -> Runners -> "Register new runner"
# (or the admin API, site-admin only).
set -euo pipefail
kind="${1:-}"
instance="${FORGEJO_INSTANCE:-https://git.phrk.org}"
if [ -n "${VLM_RUNNER_TOKEN:-}" ]; then
  token="$VLM_RUNNER_TOKEN"
else
  read -r -s -p "runner registration token: " token; echo
fi
[ -n "$token" ] || { echo "no registration token supplied" >&2; exit 2; }
command -v act_runner >/dev/null 2>&1 || {
  echo "act_runner not installed (Arch AUR: act_runner; Windows: grab the release binary)" >&2
  exit 3
}
case "$kind" in
  linux)   labels="ubuntu-latest:host";  name="linux-build" ;;
  windows) labels="windows-latest:host"; name="windows-build" ;;
  *) echo "usage: $0 linux|windows" >&2; exit 2 ;;
esac
act_runner register --instance "$instance" --token "$token" \
    --no-interactive --name "$name" --labels "$labels"
echo "### registered '$name' with labels: $labels"
echo "### run it with: act_runner daemon   (or enable the act_runner service)"
