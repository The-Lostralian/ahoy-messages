#!/bin/sh
# Installs the pre-push hook that runs scripts/check-secrets.sh on every push.
# Git hooks aren't copied by a clone, so run this once in each new checkout.
set -e
cd "$(git rev-parse --show-toplevel)"
hook=.git/hooks/pre-push
cat > "$hook" <<'EOF'
#!/bin/sh
# Installed by scripts/install-hooks.sh: stops a push that adds secrets or private details.
# git gives one line per ref: <local ref> <local sha> <remote ref> <remote sha>
zero=0000000000000000000000000000000000000000
status=0
while read -r local_ref local_sha remote_ref remote_sha; do
  [ "$local_sha" = "$zero" ] && continue          # deleting a ref
  case "$local_ref" in refs/tags/*) continue ;; esac  # a tag points at commits checked with their branch
  if [ "$remote_sha" = "$zero" ]; then
    range="origin/main..$local_sha"               # new branch: compare with main
  else
    range="$remote_sha..$local_sha"
  fi
  echo "check-secrets: $remote_ref ($range)"
  scripts/check-secrets.sh "$range" || status=1
done
exit $status
EOF
chmod +x "$hook" scripts/check-secrets.sh
echo "Installed $hook"
