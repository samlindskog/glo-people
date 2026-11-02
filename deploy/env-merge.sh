#!/bin/sh
# Merge KEY=VALUE lines from $1 into the env file $2: a key already there is replaced in place, a
# new one is appended, everything else is left alone. Values are never printed. Used by
# `make prod-env` to carry API keys from the developer's .env to /opt/glo-people/.env.
set -eu
src=$1; dst=$2
[ -f "$dst" ] || { umask 077; : > "$dst"; }
tmp=$(mktemp)
cp "$dst" "$tmp"
while IFS= read -r line || [ -n "$line" ]; do
  case "$line" in ''|'#'*) continue ;; esac
  key=${line%%=*}
  if grep -q "^$key=" "$tmp"; then
    awk -v k="$key" -v l="$line" 'index($0, k "=") == 1 { print l; next } { print }' "$tmp" > "$tmp.new" && mv "$tmp.new" "$tmp"
  else
    printf '%s\n' "$line" >> "$tmp"
  fi
  echo "set $key"
done < "$src"
install -m 600 "$tmp" "$dst"
rm -f "$tmp"
