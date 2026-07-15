# Unified Inbox

A Telegram forum-supergroup used as the UI for Steam and Discord direct messages. Use a private group unless every member is intentionally allowed to read those DMs.

## Architecture

- `core`: Python Telegram Bot API router, SQLite source of truth, ACL and persistent message/topic mapping.
- The Inbox bot relays peer messages; the Outbox bot mirrors messages sent from native Discord/Steam clients into existing topics.
- Native inbound messages and native self-messages can create persisted topics. Telegram has no command or flow for starting a new external DM.
- `discord-adapter`: isolated Python `discord.py-self` client.
- `steam-adapter`: minimal isolated Node.js Steam protocol process.
- Containers use host networking for reliable egress, but bind only to `127.0.0.1:8080-8082`.
- Credentials are mounted as read-only Compose secrets and never stored in git or SQLite.
- Secret files must be owned by host UID `1000` with mode `0600`; app processes use the same non-root UID.

## Bootstrap

```bash
just init
just lock
```

Fill `secrets/telegram_bot_token`, `secrets/telegram_outbox_bot_token`, `secrets/discord_user_token`, and the numeric values in `.env`. Add both Telegram bots to the forum group; Inbox needs topic-management rights and Outbox needs permission to post in topics.
Create the Steam refresh token interactively. QR mode is the default; credentials mode additionally reads `secrets/steam_account_name` and `secrets/steam_password`:

```bash
just steam-auth
```

After credentials authentication succeeds, empty `secrets/steam_password`; runtime uses only the refresh token stored in the Docker volume.

Then validate and start:

```bash
just config
just build
just up
just logs
```

Do not commit anything under `secrets/` or the local `.env`.

## Deployment boundary

NixOS declaratively enables Docker, installs `unified-inbox.service`, defines startup ordering, validates every required secret, and owns the Compose lifecycle. The private source checkout intentionally remains at `/home/user/unified-inbox`; SQLite, Steam refresh state, `.env`, and `0600` secrets remain outside both Git and the Nix store.

Update the private checkout with a fast-forward pull, then use the declarative unit's reload path to rebuild and reconcile containers:

```bash
git -C /home/user/unified-inbox pull --ff-only
sudo systemctl reload unified-inbox.service
```

Do not fetch the private repository or inject runtime credentials from a Nix derivation: Nix store paths are world-readable.

## Account risk

The Discord adapter automates a normal user account. Discord explicitly prohibits self-bots and may terminate that account. The adapter is isolated so its failure does not compromise Telegram routing or Steam state.
