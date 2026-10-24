# vps-agents

A small control plane that lets AI agents maintain the projects on your VPS **without** giving a model the keys.
Models only ever edit a throwaway git worktree inside a locked-down container. Everything that touches your real
checkout, containers or deploys is done by this program, after a policy check and (usually) your approval.

```
you ──HTTP──> API ──> job queue ──> engine ──> sandbox container (worker edits worktree, tests run)
                          ▲            │ policy: tier + preconditions
 monitor ─incident jobs───┘            ├─> approval (bound to the exact commit) ──> you
                                       └─> executors: merge → deploy → health check → auto-rollback
```

## Status (read this first)

Built and tested here (86 tests, real git repos, stubbed model): policy matrix, job state machine, approvals bound to
commit SHA, worktree/merge/rollback safety, diff vetting (secrets, symlinks), repair loop, review verdict gate,
incident monitor, HTTP API + auth, redaction, role/persona loading, gateway probe.

**Not verified yet — needs your VPS:**
1. **A real Docker run.** `DockerSandbox` is unit-tested for its arguments only (no daemon was available).
2. **A real model worker.** `worker.command` is deliberately unset: pick the coding agent after `python -m vpsagents.probe`.
3. **Real FreeLLMAPI behaviour** (rate limits, tool-calling quality per model).

Run `vpsagents check --config config.yaml` to see what's missing on your machine.

Running on free tokens? See [docs/FREE-TOKENS.md](docs/FREE-TOKENS.md): which provider accounts to add to FreeLLMAPI, privacy trade-offs, and the quota budget guard.

## Autonomy tiers (per project)

| tier | meaning | start change | apply to checkout | deploy / restart |
|---|---|---|---|---|
| 0 | observe | denied | denied | denied |
| 1 | propose | **approval** | approval | approval |
| 2 | assisted | auto | approval | approval |
| 3 | trusted | auto | **auto if** verified + review `APPROVE` (no review pass = asks) | **auto if** health check configured; failed deploy auto-rolls back |

Read-only jobs (investigate, incident, security_review) run at every tier. At tier 3 an auto action silently
**downgrades to approval** when its safety net is missing (no verify commands, reviewer didn't say `VERDICT: APPROVE`,
no `health_url`). Never allowed at any tier: force-push, deleting branches/projects, reading secrets, touching production data.

## Safety design

- **Sandbox**: `--cap-drop ALL`, `no-new-privileges`, read-only root, pids/memory/cpu limits, no docker socket, no host
  home, exactly one worktree mounted (`:ro` for read-only jobs). Workers reach only the model gateway network; verify
  runs have **no network** unless the project opts in. Secrets go in via a 0600 env-file, never argv.
- **The agent never runs git on your repo.** The control plane commits, refuses diffs that touch secrets/`.git`/symlinks,
  and fast-forwards your checkout only if it is clean, on the default branch, and the commit equals what you approved.
- **Approvals are single-use and bound** to the commit sha (apply), merged sha (deploy) or container (restart); any
  change after approval requires a new one. They expire.
- **Your checkout is never edited in place.** If you commit while a job runs, the job rebases, re-verifies and asks again.
- **Rollback only undoes its own merge**: it resets only if HEAD is still the commit we created and the tree is clean.
- **Commands are argv lists from your config**, never model-generated and never shell strings.
- Everything leaving a sandbox is redacted. The API requires a bearer token (≥24 chars) and should stay on localhost/private network.
- Run the control plane **on the host** (systemd), not in a container with the docker socket mounted; it needs docker only to
  launch sandboxes, restart registered containers and run your deploy command.

## Install

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -e .
cp config/config.example.yaml config.yaml          # edit projects
export VPSA_API_TOKEN=$(openssl rand -hex 24)
export FREELLMAPI_API_KEY=...                      # from the FreeLLMAPI dashboard

# model gateway reachable from sandboxes but with no internet of its own
docker network create --internal vpsa-gateway
docker network connect vpsa-gateway freellmapi      # freellmapi keeps its normal network for provider calls

docker build -f docker/Dockerfile.agent -t vpsa-agent:latest .
vpsagents check --config config.yaml
vpsagents serve --config config.yaml                # API on 127.0.0.1:8787 + runner + monitor
```

Budget guard (free-quota protection): `max_agent_runs_per_job` (default 8) and `max_agent_runs_per_day` (default unlimited); `GET /budget` shows usage.

Optional: `VPSA_TELEGRAM_TOKEN` + `VPSA_TELEGRAM_CHAT_ID` (or `VPSA_WEBHOOK_URL`) for approval/incident notifications.

## Choosing the worker

```bash
python -m vpsagents.probe --base-url http://127.0.0.1:3001/v1 --key-env FREELLMAPI_API_KEY
```
Shows which gateway models return valid tool calls. Pick one coding agent that can run non-interactively against an
OpenAI-compatible endpoint, install it in `docker/Dockerfile.agent`, and set `worker.command`. The env it receives:
`LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` plus `OPENAI_*` aliases.

## Publishing to your remote (`publish: push_main`)

For projects that already deploy from Git (for example a GitHub Actions workflow that runs on every push to `main`),
set `publish: push_main`. After you approve, the control plane merges the tested change into the checkout and then
**pushes it to `origin/main`**; your own workflow takes over from there. Branches and PRs are not used.

- The approval summary says `WILL PUSH to origin/main`. Approving it is the deploy decision.
- Before work starts and before applying, the checkout is synced with the remote (fast-forward only). If the local
  branch has unpushed commits, has diverged, or is dirty while behind, the job stops and touches nothing.
- If the remote moves while a change is waiting, the change is rebased, re-verified and approval is asked again.
- If the push is rejected (someone pushed first), the local merge is rolled back so no unpushed commit is left for a
  later `git reset --hard origin/main` to silently drop. Pushes are never forced.
- The token (`push_token_env`) lives only in the control plane's environment, is handed to git through a host-scoped
  environment variable (not argv, not the remote URL, not disk) and is never given to the sandbox. Use a fine-grained
  token limited to that one repo with *Contents: read/write*; leaving out *Workflows* also means agents cannot push
  changes to your workflow files.
- A pushed deploy is **not** auto-rolled-back by this system (GitHub deploys it). The monitor's health check will open
  an incident; at tier 3 auto-push additionally requires a `health_url`.
- Leave `deploy_command` unset for these projects, or you will deploy twice.

A ready example for `terabox-telegram-bot` is in `config/config.terabox-bot.yaml`.

## Using it

```bash
H="Authorization: Bearer $VPSA_API_TOKEN"; U=http://127.0.0.1:8787
curl -H "$H" -d '{"project":"torrent-studio","kind":"change","task":"Fix the crash when the magnet link is empty","deploy":true}' $U/jobs
curl -H "$H" $U/approvals                      # what is waiting for you (summary has diffstat, verify + review result)
curl -H "$H" -X POST $U/approvals/<id>/approve # or /reject
curl -H "$H" $U/jobs/<job id>                  # events, diff, verify log, review, result
curl -H "$H" -d '{"project":"notes-app","kind":"investigate","task":"How does auth work?"}' $U/jobs
curl -H "$H" -d '{"project":"torrent-studio","kind":"restart","task":"torrent-studio"}' $U/jobs
```
Job kinds: `change`, `investigate`, `incident`, `security_review`, `restart`. The monitor opens `incident` jobs itself
(two consecutive failures, de-duplicated, cooldown) and notifies you; it never restarts anything.

## Roles (from agency-agents)

`src/vpsagents/roles/roles.yaml` maps roles to persona files vendored from
[msitarzewski/agency-agents](https://github.com/msitarzewski/agency-agents) (MIT, see `roles/personas/LICENSE.agency-agents`).
Personas are trimmed to mission / rules / workflow, then wrapped in constraints written for this system (no invented
experience, no claims of unrun checks, no secrets). Roles: `engineer`, `tester`, `devops` (change) and `reviewer`,
`investigator`, `sre`, `security`, `git` (read-only). Add a role by adding a persona file and a yaml entry.

## Known limits / next steps

- No web UI yet (curl/API only). A small approvals page + Telegram approve buttons would be the next convenience.
- Failed jobs keep their worktree for inspection; there is no garbage collector yet.
- Auto-restart on incident for tier-3 projects is not implemented (the monitor only triages).
- No branches or pull requests: publishing is a direct push to the default branch (`publish: push_main`). Use branch protection on GitHub if you want a hard backstop.
- The budget guard counts agent runs (worker sessions), not individual model requests inside a run; bound those with your agent's max-steps flag and `sandbox.timeout_seconds`.
- One running job per project; jobs for different projects run in parallel (`max_concurrent_jobs`).

## Tests

```bash
PYTHONPATH=src:. python3 -m unittest discover -s tests -t .
```
