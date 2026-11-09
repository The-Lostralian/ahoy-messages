#!/bin/sh
# Looks for secrets and private details in what's about to be pushed (this repo is public).
#
#   scripts/check-secrets.sh                 lines added since origin/main
#   scripts/check-secrets.sh <from>..<to>    lines added in a range of commits
#   scripts/check-secrets.sh --staged        lines staged for the next commit
#
# Runs from .git/hooks/pre-push (install with scripts/install-hooks.sh). It checks only
# lines being added, so old text doesn't trip it. Matches are printed with their values
# masked. This script itself is skipped (it holds the patterns). Exit 1 means something was found: fix it (amend or rewrite the commit), or if
# it's a false alarm, push with --no-verify.

set -u
cd "$(git rev-parse --show-toplevel)" || exit 2

case "${1:-}" in
  --staged) diff_args="--cached" ;;
  "")       diff_args="origin/main...HEAD" ;;
  *)        diff_args="$1" ;;
esac

found=0
report() { echo "  $1"; found=1; }

# 1. Files that must never be in the repo
# shellcheck disable=SC2086
git diff --name-only --diff-filter=AR $diff_args -- . ":!scripts/check-secrets.sh" | while IFS= read -r f; do
  case "$f" in
    keystore.properties|local.properties|*.jks|*.keystore|*.p12|*.pem|*.key|.env|*/.env|*.env|google-services.json)
      echo "FILE $f" ;;
  esac
done > /tmp/check-secrets.files.$$
if [ -s /tmp/check-secrets.files.$$ ]; then
  echo "Files that should never be committed:"
  sed 's/^FILE /  /' /tmp/check-secrets.files.$$
  found=1
fi
rm -f /tmp/check-secrets.files.$$

# 2. Added lines that look like keys, tokens, passwords or private details.
# Each check: label|extended regex. Placeholders used in the docs are allowed below.
checks='private key|-----BEGIN [A-Z ]*PRIVATE KEY-----
GitHub token|(gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})
AWS key|AKIA[0-9A-Z]{16}
OpenAI/Anthropic-style key|sk-(ant-)?[A-Za-z0-9_-]{20,}
ntfy token|tk_[a-z0-9]{29}
32-byte base64 key (like SMS_SHARED_KEY)|[A-Za-z0-9+/]{43}=
password or token value|(password|passwd|pass|secret|token|api[_-]?key|storePassword|keyPassword)["'"'"']?[[:space:]]*[:=][[:space:]]*["'"'"']?[^[:space:]"'"'"'$<{]{6,}
home server name|chonkey
LAN address|(192\.168|10\.0\.0)\.[0-9]{1,3}
personal email|@(proton\.me|protonmail\.com|pm\.me)|lostralian@
Australian mobile number|(\+?61[[:space:]]?4|04)[0-9]{2}[[:space:]]?[0-9]{3}[[:space:]]?[0-9]{3}'

# Placeholders that are fine: the example numbers in the docs, the emulator's host address
allowed='0412345678|0412 345 678|0400000000|0499 ?000 ?000|10\.0\.2\.2'

# shellcheck disable=SC2086
added=$(git diff -U0 $diff_args -- . ':!scripts/check-secrets.sh' | awk '
  /^\+\+\+ / { file = substr($0, 7); next }
  /^@@/      { split($3, a, ","); line = substr(a[1], 2); next }
  /^\+/      { print file ":" line ": " substr($0, 2); line++ }')

echo "$checks" | while IFS='|' read -r label pattern; do
  hits=$(printf '%s\n' "$added" | grep -E -i -- "$pattern" | grep -E -v -- "$allowed")
  [ -n "$hits" ] || continue
  echo "$label:"
  # mask anything long enough to be a real value
  printf '%s\n' "$hits" | sed -E 's/([A-Za-z0-9+\/_-]{4})[A-Za-z0-9+\/_=-]{12,}/\1…[masked]/g' | cut -c1-160 | sed 's/^/  /'
done > /tmp/check-secrets.lines.$$
if [ -s /tmp/check-secrets.lines.$$ ]; then
  cat /tmp/check-secrets.lines.$$
  found=1
fi
rm -f /tmp/check-secrets.lines.$$

if [ "$found" -ne 0 ]; then
  echo
  echo "check-secrets: possible secrets or private details above. Fix them before pushing,"
  echo "or push with --no-verify if they're false alarms."
  exit 1
fi
echo "check-secrets: nothing found."
