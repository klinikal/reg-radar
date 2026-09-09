---
name: regradar-onboarding
description: Use when a new regradar operator wants to go from a fresh clone to a fully working setup — checking Docker, bringing up the stack, verifying the MCP server, and interviewing them to build their private operator context. Triggers on "set up regradar", "onboard me on regradar", "get regradar running", "configure regradar for my company", "I'm new to regradar", or a request to (re)create refs/context.md.
---

# regradar Onboarding

Takes an operator from a fresh clone to a fully working regradar instance: verifies the environment, brings the stack up, confirms data is flowing and the MCP server is reachable, then interviews the operator to generate their private `refs/context.md` — the file `regradar-operator` reads to know which regulatory items actually matter to this entity, instead of treating 40-odd regulators and thousands of feed items as equally relevant.

Run this end-to-end. Don't skip straight to the interview — an operator context file is useless if the stack underneath it doesn't work.

All commands below assume you're running from the repo root (the directory containing `docker-compose.yml`). If you're not sure where that is, find it before starting.

---

## Phase 0 — Environment check

**Before anything else:** regradar requires Docker, a piece of third-party software this project does not bundle or install. If it's missing, tell the operator to download it themselves from the official site — [docker.com](https://www.docker.com/products/docker-desktop/) for Docker Desktop, or their distro's official Docker Engine + Compose plugin packages on Linux. Do not run any install script, `curl | sh`, package-manager install, or download from anywhere other than the operator's own action on the official site. Never install Docker (or anything else) on the operator's machine on their behalf — this is exactly the kind of unattended third-party software installation an agent should not do autonomously. Stop and wait for them to confirm it's installed before continuing.

1. **Docker present and running:**
   ```bash
   docker info
   ```
   If this fails because Docker isn't installed, point the operator to the official download per above and stop here — don't proceed without it. If Docker is installed but the daemon isn't running, tell them to start it (e.g. open Docker Desktop) and wait until `docker info` succeeds before continuing.

2. **`.env` exists:**
   ```bash
   test -f .env || cp .env.example .env
   ```
   Defaults are fine for a first run — don't ask the operator to tune `SCHEDULE`, `LOG_LEVEL`, etc. unless they bring it up.

3. **Bring the stack up:**
   ```bash
   docker compose up -d
   ```
   This starts `collector`, `mcp`, `webui`, and `flaresolverr`. If any service fails to start, read its logs (`docker compose logs <service>`) and fix the actual problem before continuing — don't tell the operator it's "probably fine."

   **Common failure — port conflict:** if `mcp` (host port 8000) or `webui` (host port 8080) fails to bind because the port is already in use by something else on the machine, don't touch the committed `docker-compose.yml`. Instead create an untracked `docker-compose.override.yml` (already covered by `.gitignore`) remapping whichever port(s) clashed, e.g.:
   ```yaml
   services:
     mcp:
       ports: !override
         - "127.0.0.1:8001:8000"
     webui:
       ports: !override
         - "127.0.0.1:8081:8080"
   ```
   Use the `!override` merge tag specifically — a plain list under `ports:` gets *appended* to the base port mapping by Compose, not replaced, and both would then try to bind and fail. Adjust the port number if 8001 is also taken. Track which port you actually land on — you'll need it in Phase 1 and when telling the operator how to connect their own agent later.

4. **Confirm data is flowing.** Check the run log:
   ```bash
   docker compose exec mcp python -c "
   from collector.storage import init_db
   from mcpserver.queries import get_run_log
   print(get_run_log(init_db('/app/data/regradar.db'), limit=1))
   "
   ```
   If there's no run yet (fresh install — the collector only runs on its schedule, default 06:00 UTC), trigger one immediately:
   ```bash
   docker compose exec collector python -m collector.main --once
   ```
   Don't finish onboarding without at least one successful run — an operator with an empty database can't verify anything works.

## Phase 1 — MCP server and web UI check

Confirm both actually answer before telling the operator it's ready — a running container isn't the same as a working service:
```bash
curl -s -o /dev/null -w "MCP:   %{http_code}\n" -X POST http://localhost:8000/mcp \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"onboarding-check","version":"1.0"}}}'
curl -s -o /dev/null -w "webui: %{http_code}\n" http://localhost:8080/
```
Expect `200` from both (adjust the ports if you remapped either in Phase 0). The web UI at `http://localhost:8080` is worth mentioning to the operator regardless of what MCP client they end up using — it's a plain browsable list/search view, useful for a human to sanity-check what's actually in the database. If either check fails, go back to Phase 0 — don't proceed to the interview with a broken service underneath it.

## Phase 2 — Connect your own MCP client

If you (the agent running this skill) have a way to register a remote MCP server for yourself, do it now, pointing at the URL confirmed in Phase 1 — this lets you actually query regradar once onboarding is done, not just talk about it. If you don't have that capability in this session, or you're a different agent than the one the operator will actually use day-to-day, skip this and tell the operator to connect their own client using the "Connecting an agent" section of `../../README.md` — it covers several common setups.

## Phase 3 — Interview

**Template to follow:** `../regradar-operator/refs/example.context.md` — read it first. The output must use the exact same headings, in the same order. Don't invent new sections or drop any.

1. **Read the template** at `../regradar-operator/refs/example.context.md` so you know every heading you need to fill.
2. **Check for an existing file** at `../regradar-operator/refs/context.md`. If one exists, tell the operator and ask whether this is a fresh rebuild or an update — don't silently overwrite prior answers.
3. **Interview the operator.** Ask in grouped batches (not all twelve headings one at a time — that's tedious), roughly:
   - Batch 1 — identity: About the Company, Entity Type & Incorporation, Location(s)
   - Batch 2 — licence: Licence & Regulatory Status, Authorised MiCA Services, Asset Scope
   - Batch 3 — infrastructure: Fiat Rails & Banking, Exclusions, Adjacent Interests, Relevant Affiliates
   - Batch 4 — content preferences: Things Interested In (offer the regradar-operator defaults — CASP circulars, MiCA text/interpretation, transition periods, warning notices, guidance notes — and ask what to add, remove, or reprioritise)

   While you are here, point the operator at `config/profiles/` — a profile turns those preferences into a saved filter (jurisdictions, activities, instruments, document types) that every surface accepts: the web UI picker, the `profile` parameter on the MCP query tools, the JSON API, `regradar export`, the Atom feeds, webhooks and digests. If one of the shipped templates (`eu-casp`, `eu-bank`, `eu-emi`, `uk-equities-dealer`) already matches what they described, name it; if not, say a custom profile YAML is a copy of the closest template with the lists edited.

   Use judgement on phrasing — ask plain-English questions, not "what's your Entity Type & Incorporation," and don't require the operator to know regulatory jargon. Infer the right heading from their answer.

4. **Draft the two synthesis sections yourself** — don't ask the operator to write "Things Interested In" priorities or "Relevance to regradar Monitoring" from scratch. Derive them from everything else they told you (their licence scope, jurisdiction, services, affiliates) and show the draft back to them for confirmation/edits.

## Phase 4 — Write, move, verify

5. **Write the draft** to `refs/context.md` in *this* skill's own folder (i.e. `regradar-onboarding/refs/context.md`, not the operator skill's folder yet) — create the directory if it doesn't exist. This keeps a half-finished interview from ever landing in the live location the operator skill reads.
6. **Show the full draft to the operator** and get explicit confirmation before moving it into place.
7. **Move it into place** once confirmed:
   ```bash
   mkdir -p ../regradar-operator/refs
   mv refs/context.md ../regradar-operator/refs/context.md
   ```
   (paths relative to this skill's folder — resolve to absolute paths in practice)
8. **Verify it's git-ignored** before finishing — this file contains private company facts and must never be committed:
   ```bash
   git check-ignore -v skills/regradar-operator/refs/context.md
   ```
   If that command doesn't report the file as ignored, stop and fix `.gitignore` (should already have `skills/*/refs/context.md`) before telling the operator you're done — don't finish an onboarding run with an unprotected private file sitting in a public repo's working tree.
9. **Confirm completion** — tell the operator: the stack is up, data is flowing, the MCP server and web UI both answered, the web UI is browsable at `http://localhost:8080`, and `regradar-operator` now has their context in place and will use it automatically. If Phase 2 was skipped, remind them to connect their own client per the README.

## Notes

- Never write real company facts into `refs/example.context.md` — that's the public template, tracked in git.
- If the operator gives you information that doesn't fit any existing heading, don't force it in awkwardly — ask whether it should become a new heading, and if so add it consistently to both the actual file and (in generalised/anonymised form) propose an update to the shared template.
- If a phase fails, fix the actual problem before moving on. Don't tell the operator "you're all set" while skipping a check silently.
- Never install third-party software on the operator's machine on their behalf — not Docker, not anything else this project might come to depend on later. If something's missing, name it, point to its official source, and wait for the operator to install it themselves.
