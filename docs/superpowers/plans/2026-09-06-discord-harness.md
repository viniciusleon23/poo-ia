# Poo-IA Discord harness implementation plan

**Design:** `docs/superpowers/specs/2026-09-06-discord-harness-design.md`

**Goal:** deploy a persistent, owner-only Discord assistant on the Beelink that remembers context, researches Capnet with OpenCode, and prepares or publishes small repository changes through a host Codex worker. AWS stays disabled and unconfigured.

## Delivery strategy

Implement in testable vertical increments. Keep the existing Discord/Ollama/OpenCode behavior working after every increment. Use the project virtual environment or Docker for authoritative tests. Do not copy secrets into the repository, test fixtures, process output, or commit history.

## Task 1: configuration and domain models

**Files**

- Modify: `app/config.py`
- Create: `app/models.py`
- Modify: `.env.example`
- Modify: `tests/test_config.py`
- Create: `tests/test_models.py`

**Work**

1. Add validated settings for SQLite, memory retention, the global job limit, worker HTTP access, and Codex enablement.
2. Make `ALLOWED_USER_ID` mandatory for the deployed application.
3. Keep AWS explicitly disabled and reject `AWS_ENABLED=true` with a clear “not implemented in this phase” configuration error.
4. Define neutral dataclasses/enums for inbound messages, conversations, intents, jobs, validation, results, and outbox parts.
5. Ensure secret fields stay out of `repr` and validation messages.

**Verification**

- Defaults, boundary values and malformed settings are covered.
- Existing Ollama/OpenCode configuration tests remain green after updating owner fixtures.

## Task 2: SQLite migrations and storage

**Files**

- Create: `app/migrations/001_initial.sql`
- Create: `app/storage.py`
- Create: `tests/test_storage.py`

**Work**

1. Open SQLite with foreign keys, WAL, busy timeout and explicit transactions.
2. Add `schema_migrations`, `conversations`, `inbound_requests`, `exchanges`, `jobs`, `job_events`, `outbox` and `outbox_parts`.
3. Implement atomic inbound-message registration and deterministic request/job IDs.
4. Implement validated job transitions and append-only job events.
5. Add pruning for seven-day conversational memory and 30-day terminal operational records.
6. Reopen the database in tests to prove persistence.

**Verification**

- Migration is idempotent.
- Duplicate Discord message IDs resolve to the same request/job.
- Invalid job transitions roll back.
- No retained operational text can be selected as conversation context after forget.

## Task 3: memory, context and outbox

**Files**

- Create: `app/memory.py`
- Create: `app/outbox.py`
- Modify: `app/prompt_loader.py`
- Create: `tests/test_memory.py`
- Create: `tests/test_outbox.py`
- Modify: `tests/test_prompt_loader.py`

**Work**

1. Load the newest ten completed exchanges inside seven days, trimming oldest content until the prompt is at most 12,000 characters.
2. Store an exchange only after every Discord fragment has a recorded successful send.
3. Implement exact normalized control phrase `olvida la conversación`; delete exchanges and reset active repo/last-job context without deleting operational audit rows.
4. Format history as clearly delimited user/assistant turns for all model backends.
5. Pre-split outbox messages at 1,900 characters and resume pending parts in creation order.

**Verification**

- Retention, ordering, character cap, isolation and forget use a controlled clock.
- A partial send does not create an exchange; after the final ACK it creates exactly one.
- Reopening SQLite retains context and pending outbox state.

## Task 4: routing and persistent scheduler

**Files**

- Rewrite: `app/router.py`
- Create: `app/scheduler.py`
- Create: `app/orchestrator.py`
- Modify: `tests/test_router.py`
- Create: `tests/test_scheduler.py`
- Create: `tests/test_orchestrator.py`

**Work**

1. Recognize deterministic controls for forget, status, cancel, prepare change, publish PR and deferred AWS.
2. Preserve exact small-talk routing to Qwen and use OpenCode as conservative technical default.
3. Resolve follow-ups from conversation state and the active job/repository; return one clarification when a mutable request still lacks a unique repo.
4. Put OpenCode, Codex and GitHub work through one persistent FIFO scheduler with concurrency one.
5. Recover queued/running jobs by kind and reuse the existing deterministic ID.
6. Publish user-facing progress and final results through the outbox.

**Verification**

- “ese repo”, “hazlo” and “arma el PR de ese trabajo” resolve from saved state.
- Ambiguous model output cannot become a mutable intent.
- At most one heavy job runs in concurrency tests.
- The same inbound message never schedules twice.

## Task 5: OpenCode context and cancellation

**Files**

- Modify: `app/opencode_client.py`
- Modify: `app/orchestrator.py`
- Modify: `tests/test_opencode_client.py`
- Modify: `tests/test_orchestrator.py`

**Work**

1. Pass rules, personality, completed history and active repo as delimited context.
2. Expose the active OpenCode session ID to the operation so cancellation/timeout invokes `/abort` before deletion.
3. Keep session deletion in every outcome and retain Basic Auth secrecy.
4. Produce a documented preflight bundle for each code change before Codex unless the approved explicit skip condition applies.

**Verification**

- Realistic history appears in the prompt without operational audit text.
- Timeout order is abort then delete.
- OpenCode remains unable to edit or shell.

## Task 6: integrate Discord with the core

**Files**

- Rewrite: `app/discord_bot.py`
- Modify: `app/__main__.py`
- Modify: `tests/test_discord_bot.py`
- Create: `tests/test_discord_delivery.py`

**Work**

1. Initialize storage, migrations, scheduler, clients and orchestrator in `setup_hook`.
2. Convert a Discord event to the neutral inbound message and pass it to the orchestrator.
3. Send immediate job acknowledgement, periodic meaningful progress and final output without holding a typing context for the full job.
4. Record each returned Discord message ID as the outbox-part ACK.
5. On ready, flush pending outbox parts before accepting a new response for the same conversation.
6. Close scheduler, clients and database cleanly.

**Verification**

- Existing channel/bot/empty/owner filters remain first.
- Long output stays within 1,900 characters.
- Simulated disconnect/reconnect resumes pending parts and saves one exchange.

## Task 7: worker configuration, state and authenticated API

**Files**

- Create: `worker/__init__.py`
- Create: `worker/__main__.py`
- Create: `worker/config.py`
- Create: `worker/models.py`
- Create: `worker/store.py`
- Create: `worker/api.py`
- Create: `app/worker_client.py`
- Create: `tests/worker/test_config.py`
- Create: `tests/worker/test_store.py`
- Create: `tests/worker/test_api.py`
- Create: `tests/test_worker_client.py`

**Work**

1. Parse host-only paths, loopback address, credentials, Codex/GitHub settings and limits.
2. Refuse a non-loopback bind.
3. Persist one manifest per job with payload hash, state, PID/unit, repo, worktree, branch, validation, result and PR URL.
4. Implement authenticated health, create/get/cancel/publish endpoints; omit an AWS execution endpoint.
5. Make duplicate payloads idempotent and mismatched payloads conflict.
6. Redact credentials from errors/logging.

**Verification**

- Unauthenticated and wrong-password calls are rejected.
- The same job request returns the same manifest.
- Restarting the API reloads manifests without losing status.

## Task 8: repository and Codex execution

**Files**

- Create: `worker/repositories.py`
- Create: `worker/codex_runner.py`
- Create: `worker/validation.py`
- Create: `tests/worker/test_repositories.py`
- Create: `tests/worker/test_codex_runner.py`
- Create: `tests/worker/test_validation.py`

**Work**

1. Inventory Git repositories directly below `CAPNET_WORKSPACE`, support unambiguous case-insensitive aliases, and reject path traversal or a repo outside the workspace.
2. Require a clean base working tree and resolve its current base commit without switching branches.
3. Create an isolated worktree/branch deterministically for the job.
4. Launch `codex exec` with an argv list, `--sandbox workspace-write`, no model override, JSONL output and a final-message file.
5. Run Codex through a user transient unit or equivalent detached process with file-backed output so a container/worker restart can reconcile it.
6. Detect test commands from repository instructions/manifests, record `passed`, `unchanged_failure`, `failed`, `unavailable` or `timed_out`, and preserve logs.
7. Measure diff budget using `git diff --numstat`; treat binaries as over budget.
8. Return a concise summary and write large diffs/logs to worker state files.

**Verification**

- Tests use temporary repositories and a fake Codex executable; no Capnet repo is modified.
- Worktree and branch names are deterministic.
- Cancel/timeout terminates the test process and preserves diagnostics.
- The base working tree and base ref are unchanged.

## Task 9: GitHub publication

**Files**

- Create: `worker/github.py`
- Modify: `worker/api.py`
- Create: `tests/worker/test_github.py`

**Work**

1. Commit the prepared worktree with a job-linked message when publication is authorized.
2. Check authentication, remote branch and existing PR before every mutation.
3. Push the deterministic branch and create a PR with `gh pr create`.
4. Store and return the canonical PR URL.
5. Allow explicit override for over-budget or failed/timed-out validation only when the follow-up request identifies the job.

**Verification**

- Fake git/gh tests cover first publication, retry after push, retry after PR and auth failure.
- Repeating publish returns one URL and does not create another PR.

## Task 10: packaging, services, rules and documentation

**Files**

- Modify: `Dockerfile`
- Modify: `compose.yml`
- Modify: `requirements.txt`
- Create: `requirements-worker.txt`
- Create: `ops/poo-ia-worker.service`
- Create: `ops/poo-ia-worker.env.example`
- Create: `rules/memory.md`
- Create: `rules/research.md`
- Create: `rules/code-changes.md`
- Create: `rules/aws-future.md`
- Modify: `README.md`
- Modify: `.gitignore`
- Modify: `.dockerignore`

**Work**

1. Add a persistent Docker volume at `/app/data` while retaining host networking and read-only rules/personality mounts.
2. Install the worker into a dedicated host virtual environment and add a user systemd unit bound to loopback.
3. Document generated private environment files and state/worktree directories.
4. Document natural-language examples for memory, research, changes, status, cancel and PR.
5. State prominently that AWS is disabled and no credentials are required or touched.
6. Add operational health, logs, recovery, update and rollback instructions.

**Verification**

- `docker compose config` succeeds with a non-secret fixture environment.
- Docker image and worker package import successfully.
- Secret/state/worktree patterns remain ignored.

## Task 11: local quality gate

**Work**

1. Install exact dependencies in the existing disposable test virtual environment.
2. Run the complete test suite.
3. Build the Docker image and run tests inside it.
4. Run static syntax/import checks.
5. Scan tracked files and staged diff for credentials and environment files.
6. Review the complete diff against every design requirement.

**Verification**

- All tests pass twice: host test environment and container.
- Git diff is clean of whitespace errors and secrets.

## Task 12: deploy and authenticate on the Beelink

**Work**

1. Pull the final commit into `/home/poo/poo-ia` and preserve the existing private `.env`.
2. Discover the owner ID from an authenticated Discord API response without printing the bot token, then set `ALLOWED_USER_ID`.
3. Generate a distinct internal worker password without printing it and place matching values in private bot/worker environments.
4. Install the worker virtual environment and user service; enable/start it and verify authenticated/unauthenticated health.
5. Authenticate Codex CLI with ChatGPT subscription using device auth and the already authorized browser session when possible.
6. Authenticate GitHub CLI and Git SSH using the owner's GitHub session; verify repository read/push capability without creating a commit.
7. Keep `AWS_ENABLED=false`; do not install, inspect, copy or configure AWS credentials.
8. Rebuild/restart the bot and verify Ollama, OpenCode, worker and SQLite volume health.

**Verification**

- `codex login status` succeeds without `OPENAI_API_KEY`.
- `gh auth status` succeeds and the intended GitHub account is shown.
- Worker and bot remain active after restart.

## Task 13: end-to-end acceptance and final publication

**Work**

1. Record a clean-state inventory for all 20 workspace repos.
2. Exercise the orchestrator against real Ollama and OpenCode from the deployed container.
3. Verify memory across a container restart and the forget flow.
4. Generate a response longer than 1,900 characters and verify fragment sizes.
5. Run a harmless small change through Codex in an isolated worktree, validate the diff/tests and confirm the base checkout is untouched.
6. Use an explicitly authorized test request to create one real PR, then retry the same job and verify the same URL is returned.
7. Restart the bot during a real queued/running job and verify the same job ID completes.
8. Recheck every workspace repo; only the expected isolated worktree/branch/PR may differ.
9. Update README with any operational finding, rerun the full quality gate, commit all implementation changes and push `main`.
10. Notify the owner that Discord is ready and provide a short manual test script.

**Acceptance**

- Every numbered criterion in the design has direct command, test, database, service, Discord-message or PR evidence.
- AWS is still disabled and untouched.
- Local and remote `main` point to the final pushed commit.
