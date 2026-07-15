#!/usr/bin/env bash
set -euo pipefail

project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
secrets_dir="$project_dir/secrets"
mkdir -p "$secrets_dir"
chmod 700 "$secrets_dir"

if [[ ! -s "$secrets_dir/internal_api_token" ]]; then
  umask 077
  python3 - <<'PY' > "$secrets_dir/internal_api_token"
import secrets
print(secrets.token_urlsafe(48))
PY
fi

for name in telegram_bot_token discord_user_token steam_account_name steam_password; do
  if [[ ! -e "$secrets_dir/$name" ]]; then
    install -m 600 /dev/null "$secrets_dir/$name"
  fi
done

chmod 600 "$secrets_dir"/*
printf 'Secret files are ready in %s\n' "$secrets_dir"
printf 'Fill Telegram/Discord tokens and optional Steam credential files.\n'
