# Security policy

## Supported versions

Security fixes are applied to the latest commit on `main`. This project does not currently maintain release branches.

## Reporting a vulnerability

Use [GitHub private vulnerability reporting](https://github.com/kyoukisu/unified-inbox/security/advisories/new). Do not post credentials, tokens, private messages, screenshots of DMs, or exploit details in a public issue.

Immediately revoke and replace any credential that may have been exposed:

- Telegram bot tokens through BotFather;
- the Discord user token by changing the account password and signing out sessions;
- the Steam refresh token by revoking authorized devices;
- `secrets/internal_api_token` by regenerating it and restarting all containers.

## Security boundaries

- Runtime credentials live in ignored `0600` files and are mounted read-only.
- Containers run as the host UID configured by `APP_UID`, with read-only root filesystems, dropped Linux capabilities, and `no-new-privileges`.
- HTTP services bind only to `127.0.0.1` and require a shared bearer token.
- Telegram routing is restricted to one numeric chat ID and one numeric user ID.
- Media downloads require HTTPS, use platform-specific host allowlists, and enforce a size limit.
- SQLite and the Steam refresh token live in Docker named volumes, outside Git and the Nix store.

## Platform risk

The Discord adapter is a self-bot. Discord prohibits automating normal user accounts and may suspend or terminate the account. This is an operational risk, not a vulnerability in this repository.
