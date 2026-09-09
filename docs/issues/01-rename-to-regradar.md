# Rename casp-radar to RegRadar

**Size:** S  **Depends on:** none  **Design doc:** docs/design/regradar-architecture.md §1

## Why

The product is becoming sector-neutral. A name that says "CASP" tells a bank's compliance team it is not for them before they read the README. Doing the rename first and alone keeps every later PR free of incidental renames, and it is the one change that touches the DB filename, the Docker volume, the compose project name, every skill and every script at once.

## Scope

- Name check before any code moves: confirm "RegRadar" is not a registered mark in the UK or EU for software or information services, and that the GitHub org can hold `regradar`. Record the result in this issue. Fallbacks if it fails: RegLens, RegWire.
- Repository rename on GitHub to `klinikal/regradar`. GitHub redirects the old URL; update the clone URL in the README anyway.
- Python package and service names: `collector`, `mcpserver`, `webui` stay as they are. Only user-facing identifiers change.
- Identifiers that change, with old to new:
  - DB file `casp_radar.db` to `regradar.db` (`DB_PATH` default, `litestream.yml`, `.env.example`, README).
  - Docker volume `casp_radar_data` to `regradar_data`; compose project name `casp-radar` to `regradar`; Litestream backup path `/backups/casp_radar` to `/backups/regradar`.
  - `USER_AGENT` default to `regradar/2.0 (+https://github.com/klinikal/regradar)`.
  - MCP server name string in `mcpserver/server.py` and the `claude mcp add` example.
  - Skills directories `skills/casp-radar-onboarding` and `skills/casp-radar-operator` to `skills/regradar-onboarding` and `skills/regradar-operator`, and every internal reference in `SKILL.md` and `refs/example.context.md`.
  - Log format prefix, script docstrings and help text under `scripts/`.
- A one-off migration path for existing installs: a documented `docker compose` sequence that renames the volume (create new volume, copy, remove old) and moves the Litestream backup directory. This must be in the README under an "Upgrading from casp-radar" heading, with the exact commands.
- CHANGELOG entry and a `2.0.0-alpha.1` tag once merged, so the rename is a clear line in history.

## Non-goals

- No functional change of any kind. Tests must pass with no assertions changed except string comparisons on names.
- No rename of Python modules, functions or database tables.
- No change to the item id scheme (`make_item_id` uses `regulator_id|url|published_at`, not the product name, so ids are stable across the rename).

## Design notes

Files known to reference the old name (from `grep -rl 'casp.radar\|casp_radar'`): `.env.example`, `CHANGELOG.md`, all three Dockerfiles, `README.md`, `collector/fetcher.py`, `collector/main.py`, `config/regulators.yaml` (notes only), `docker-compose.yml`, `litestream.yml`, `mcpserver/server.py`, every script under `scripts/`, both skills, `tests/test_server.py`, `webui/app.py`. Roughly 100 occurrences.

Keep the old volume name readable by the compose file for one release via an env override, so a user who upgrades without reading the README does not boot against an empty database:

```yaml
volumes:
  regradar_data:
    name: ${DATA_VOLUME_NAME:-regradar_data}
```

with the upgrade section telling them to set `DATA_VOLUME_NAME=casp_radar_data` if they have not migrated yet, and to remove that line once they have.

The `collector/main.py` integrity-check log message and the operator skill's backup and recovery section both name the DB path; update both.

## Acceptance criteria

- [ ] `grep -ri 'casp.radar\|casp_radar' --exclude-dir=.git --exclude=CHANGELOG.md .` returns only the upgrade section of the README and the notes field in `regulators.yaml`.
- [ ] `docker compose up -d` on a clean machine creates volume `regradar_data` and a DB at `/app/data/regradar.db`.
- [ ] Following the README upgrade steps on a machine with an existing `casp_radar_data` volume produces a working stack with all prior items present (`count_items_tool` returns the same number before and after).
- [ ] `claude mcp add --transport http regradar http://localhost:8000/mcp` connects and `get_health_tool` reports the new DB path.
- [ ] Full test suite passes.
- [ ] Name check result recorded in this issue before the repository rename is executed.

## Open questions

- Whether to keep `casp-radar` as an archived fork or rely on GitHub's redirect alone. Recommendation: redirect only, with a one-line note at the top of the README for six months.
