# Contributing

1. Open an issue describing the behavior change before starting a large refactor.
2. Never include real tokens, account IDs, private messages, or production database files in an issue, fixture, commit, or screenshot.
3. Keep JavaScript limited to the Steam adapter unless the Steam ecosystem requires otherwise.
4. Add behavior-focused tests for routing, deduplication, ACL, and persistence changes.
5. Run the complete local checks:

```bash
just lint
just unit
cd steam-adapter && npm audit --omit=dev
```

Use small commits and explain any Discord or Steam protocol assumptions in the pull request.
