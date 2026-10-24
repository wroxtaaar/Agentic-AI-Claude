# Running on free tokens

Goal: keep `vps-agents` working for $0 by pooling the free tiers of many providers behind **FreeLLMAPI**, and by not
wasting what you get. Free-tier limits and terms change often. Treat every number here as a starting point and check
the provider's own dashboard. Figures are from community lists and vendor pages as of **October 2026**.

## 1. How the pieces fit

```
vps-agents sandbox ──> FreeLLMAPI (one OpenAI-compatible endpoint, rotates keys, falls back on 429/5xx)
                           ├─ Google, Groq, Cerebras, Mistral, OpenRouter, NVIDIA, Cloudflare, Cohere, Z.ai, ... (34 providers)
                           └─ optional: any other OpenAI-compatible endpoint (custom provider)
```

FreeLLMAPI already aggregates 34 providers, so **more free tokens = more provider keys added to FreeLLMAPI**
(its dashboard, "Keys" page). You do not need a second router. OmniRoute is a similar gateway; FreeLLMAPI can accept an
OpenAI-compatible endpoint as an extra upstream, so OmniRoute could be added that way, but that is untested here and
adds another hop that can fail. Try it only if you run out of tokens.

## 2. Accounts worth creating (one per provider)

| Provider | Free limit (approx.) | Notes |
|---|---|---|
| Google AI Studio (Gemini) | per-model quotas shown in AI Studio | No card. Free-tier prompts may be used to improve Google products (not in EEA/UK/CH). |
| Groq | ~30 req/min, ~1,000 req/day | Fast. Models get deprecated, check the list. |
| Cerebras | ~1M tokens/day (vendor figure) | Very fast inference. |
| Mistral | ~1 req/s, ~500K tokens/min | Free-mode data may train models unless you opt out in settings. |
| OpenRouter | 20 req/min, 50 req/day per free model | Free providers may log prompts. |
| NVIDIA NIM | ~40 req/min, ~10,000 req/day | Developer Program membership; trial use; usage logged. |
| Cloudflare Workers AI | ~10,000 "Neurons"/day | Requests fail at the limit, you are not billed. |
| Z.ai (Zhipu) | 1 concurrent request | GLM models; chat API needs no real-name check. |
| Cohere | ~1,000 calls/month | **Non-commercial use only.** |
| OVHcloud / Kilo / LLM7 | small, per-IP | Work without signup; limits can drop without notice. |

Skip ModelScope and SiliconFlow: they need real-name verification or a mainland-China account.

Steps: create the account, create an API key, paste it into FreeLLMAPI's Keys page. Then re-run the probe (section 5).

## 3. Rules that keep you out of trouble

- **One account per provider.** Making several to dodge limits breaks their terms and gets the lot banned.
- **No cookie / OAuth / "reuse a subscription" routes.** Some gateways offer them; they likely violate those services' terms.
- Keep the FreeLLMAPI encryption key and dashboard private (localhost + SSH tunnel). Never commit keys.
- Respect per-provider terms (e.g. Cohere non-commercial).

## 4. Privacy: free often means "your prompts may be logged or used for training"

Google, Mistral, NVIDIA and OpenRouter free tiers say as much. The agent sends **your code** in prompts.

- Public / hobby code: fine.
- Private code you care about: prefer a local model on the VPS (Ollama, llama.cpp) added to FreeLLMAPI as a custom
  provider, if the machine has spare RAM; or keep those projects at tier 0 and use them only for read-only questions.
- Whatever you choose, the sandbox never gets secrets, and everything it outputs is redacted, but model providers still
  see the files the agent reads. Do not put secrets in repositories.

## 5. Pick models with evidence

```bash
python -m vpsagents.probe --base-url http://127.0.0.1:3001/v1 --key-env FREELLMAPI_API_KEY --limit 40
```
Only models showing `ok` in the `args` column return valid tool calls. Set `llm.model` to one of them (or to a
FreeLLMAPI group/`auto` option that routes only among such models). Re-run after adding keys or when providers change.

## 6. Don't waste tokens: the budget guard

Each worker session (an edit, a repair attempt, or a review pass) is one **agent run**. Limits in `config.yaml`:

```yaml
max_agent_runs_per_job: 8        # a runaway repair loop stops here
max_agent_runs_per_day: 150      # UTC day, across all jobs; null = unlimited
```

- Normal change job: 2 runs (edit + review). Each failed verification adds a repair run.
- Hitting the per-job cap fails the job with a clear message. If only the *review* is out of budget, the verified change
  still waits for your approval with the review marked `BLOCK` (skipped).
- Hitting the daily cap fails new jobs until the next UTC day, or until you raise it. Incident triage jobs count too.
- `GET /budget` shows runs used today and what remains.

**What it does not do:** it counts agent runs, not the individual model requests an agent makes *inside* one run.
A single run can make dozens of requests. Bound that with your agent's own max-steps/turn flag in `worker.command`,
and with `sandbox.timeout_seconds` (default 30 min). Exact request/token metering would need a small proxy between the
sandbox and FreeLLMAPI that counts per job; that is a possible next step.

A rough way to size the daily cap: `daily free requests you actually have ÷ typical requests per run`. Measure the
second number from FreeLLMAPI's usage page after a few real jobs, then set the cap a bit under your real daily budget.

## Strong models for architecture, weak models for the work

A change job now runs: **architect plan (strong) → engineer edits (weak) → verify → [architect diagnosis (strong) if it keeps failing] → reviewer (strong)**.
Read-only jobs (investigate, incident, security) use the `analyst` class. Set the names under `llm.models`.

- Strong picks: the largest reasoning/coding models your gateway offers (e.g. a 70B+/frontier-class free model). Weak picks: small, fast, high-quota models.
- Why it saves tokens: planning and review are 2-3 short strong calls per job; the many tool-call-heavy editing turns run on the cheap model.
- Cap the scarce ones with `max_strong_runs_per_day`. When it is hit, planning is skipped (worker proceeds), diagnosis is skipped, and review fails safe to BLOCK so a human decides.
- A weak worker only gets the plan as text; it can still edit only inside the throwaway worktree, and verification and review are unchanged.
- Limit: the count is worker *sessions*, not inner model requests (same as the other budgets).

## 7. Stretching what you have

- `review_pass: false` saves one of a job's usual two runs. You lose the second-opinion verdict, so tier-3 projects
  will ask for approval before applying (auto-apply requires a review `APPROVE`).
- Keep `max_concurrent_jobs` at 1–2 so you don't trip per-minute limits on several providers at once.
- Give jobs small, specific tasks. Big vague tasks mean long agent sessions.
- Cheap, fast `verify_commands` (unit tests, not full builds) keep repair loops short.
- Prefer models with large daily budgets for the worker and a different model for review so one provider's limit
  doesn't stall both.
- Many 429s in a row: FreeLLMAPI is rotating through exhausted keys. Add more provider keys or lower concurrency.

## 8. Checklist

1. Create accounts (section 2) and add the keys to FreeLLMAPI.
2. Run the probe; choose `llm.model`.
3. Set `max_agent_runs_per_day` a little under your real daily budget.
4. Start every project at tier 1 or 2 until you've watched a few jobs.
5. Watch FreeLLMAPI's usage page for a week, then adjust.
