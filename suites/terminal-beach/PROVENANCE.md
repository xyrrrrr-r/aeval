# Provenance — terminal-beach

Every task in `tasks/` is derived, not authored. The task **content**
(instruction, tests, solution) is carried over verbatim from
`terminal-bench-core`; what this suite changes is the **environment** (the
agent runtime that is baked into the image) and the **session record slot**
(the deepAgent family publishes an ACP runner summary, not a DSH session
file). This file records both, so the deviations are auditable rather than
implied.

| field | value |
| --- | --- |
| dataset | `terminal-bench-core@0.1.1` |
| source | https://github.com/laude-institute/terminal-bench |
| branch | `dataset/terminal-bench-core/v0.1.x` |
| commit | `91e10457b5410f16c44364da1a34cb6de8c488a5` |
| license | Apache-2.0 |
| imported at | 2025-09-24 |
| migrator | harbor 0.23.0 (task migrate, mappers/terminal_bench.py) |
| converter | `aeval-vendor-tbench/1` |
| sibling suite | `suites/tbench-pilot` (same three tasks, DSH-driven) |

Verbatim upstream copies live under `vendor/upstream/` and are the reference
for the carried-over files (instruction / tests / solution). They are byte
comparison material only; they are never executed.

## Why the vendored tasks differ from the migration output

The migrations are the same as `tbench-pilot`'s. The differences below are
the ones this suite adds on top, all forced by the deepAgent runtime.

1. **`environment` block** — `network_mode` stays `allowlist`, but the host
   list is narrowed to the broker host (`193.126.4.2`) alone. The
   `tbench-pilot` list also carries `registry.npmjs.org` / `nodejs.org`
   because the DSH CLI is installed with `npm install --global` at trial
   time; deepAgent's CLI (`dcode`) is baked into the task image, so those
   two egress destinations no longer exist and are removed rather than kept
   as dead grants. `docker_image` is a digest-pinned internal image, and it is
   **per task** (`terminal-beach-<task>`) — not the shared base: the backend
   uses `docker_image` verbatim, so a task's own `environment/Dockerfile` never
   runs on a trial and the task image is the only place a task input can come
   from.

2. **`verifier.collect`** — the session-record slot is
   `agent_session_record`, not `dsh_session`. deepAgent's official session
   record is Harbor's own ACP runner summary (`acp-summary.json`: session id,
   stop reason, token usage, instruction), collected through the generic
   slot; the full event stream becomes the canonical transcript through
   Harbor's ACP → ATIF conversion. Everything else in the hook (run the
   upstream tests, then collect even on failure) is unchanged, so a failing
   trial still seals its evidence.

3. **`[agent].timeout_sec` 360 → 900** — the upstream bound assumes a
   scripted solver. deepAgent drives a real model over a multi-step task
   (inspect → edit → verify), so the per-trial agent wall clock is raised to
   the suite's declared budget (`budgets.yaml` `agent.run_timeout_sec`).

4. **`environment/Dockerfile`** — written as the recipe for that task's
   image: `FROM` the digest-pinned shared `terminal-beach-agent` base, the
   migrated `ENV TEST_DIR` and the `WORKDIR /app` restore, plus the task's own
   upstream provisioning carried over rather than dropped. Which upstream
   steps matter, and what they cost when lost:

   | task | upstream step | carried over as | if it were missing |
   | --- | --- | --- | --- |
   | hello-world | base image only | base `FROM` + `ENV`/`WORKDIR` | — |
   | openssl-selfsigned-cert | `apt-get install openssl` | nothing: the shared base already ships OpenSSL 3.0.2 (asserted by the build's self-check) | the task is unsolvable (all `openssl` calls fail) |
   | sqlite-db-truncate | `COPY trunc.db /app` | inlined byte-for-byte (`base64 -d` + `sha256sum -c`) into the task image, because the e2b template build API cannot receive build-context files | the sandbox has no input database: measured in the first real full run — the agent searched the whole filesystem, correctly reported `/app/trunc.db` missing and asked for clarification, and the verifier failed with `FileNotFoundError: /app/recover.json` |

   The upstream base-image selection, the offline verifier toolchain (uv +
   wheelhouse + verifier project) and the removal of build-time network
   installs are inherited from `tbench-pilot/images/base-image.Dockerfile` and
   are described in that suite's PROVENANCE.md, not repeated here.

## The agent image (`images/agent-image.Dockerfile`)

Built on the cluster host from two digest-pinned, already-verified internal
images — no egress at build time:

* `harbor:443/t-bench/agentlab-deepagent` — the deepAgent runtime:
  python3.12 (dcode requires ≥ 3.12), node 24 (the in-sandbox facade
  requires ≥ 18), `deepagents-code==0.1.78` (pinned equal to
  `aeval.agents.deepagent.agent._DEEPAGENTS_CODE_VERSION` by an integration
  test), uv, and Harbor's ACP runner prerequisites pre-staged at the exact
  paths its bootstrap checks (`/opt/harbor-acp-tools/uv`,
  `/opt/harbor-acp-venv`, `/opt/harbor-acp-python`) so the no-network
  sandbox never has to fetch them;
* `harbor:443/t-bench/base-pilot` — the verifier toolchain:
  `/usr/local/bin/uv`, `/opt/wheels` (the pinned pytest 9.1.1 closure),
  `/opt/verifier/pyproject.toml`.

The build ends with self-checks: the verifier's exact offline path
(`uv sync --offline` + `uv run pytest --version` from
`/opt/verifier/pyproject.toml`), the readiness seed `/workspace/ready`, the
deepAgent CLI, and OpenSSL. `images/build-agent-image.sh` then builds one image
per task from `tasks/<task>/environment/Dockerfile`, checks each image really
contains what its Dockerfile claims (`/app/trunc.db` present for sqlite), and
writes the two levels of digests back through `images/pin-digests.py` — the
only writer of those pins, so a Dockerfile cannot silently drift from what a
trial executes.


## Reporting a run of this suite

`budgets.yaml` is the single declaration of the trial budget and of the
upstream wire protocol (`responses`, because dcode speaks the OpenAI
Responses API). `tools/gen_broker_spec.py` turns it into the operator's
broker spec; the trajectory grader's constants are asserted equal to it by
`tests/integration/test_terminal_beach_suite.py`.

## Framework fixes this suite's real runs forced

Both were found by running the chain, not by reading it, and both are asserted
by `tests/integration/test_terminal_beach_suite.py` plus the adapter unit tests.

1. **The CLI must name the provider** (`aeval.agents.openai_acp`). dcode infers
   its provider from the model *name*, so a lease served as `deepseek-chat`
   resolved to the vendor provider: measured inside the built task image,
   `create_model("deepseek-chat")` raises
   `MissingCredentialsError: No credentials found for provider 'deepseek'`,
   while `create_model("openai:deepseek-chat")` returns the intended client
   (`ChatOpenAI(model='deepseek-chat', openai_api_base=<façade>,
   use_responses_api=True)`). The adapter now renders `--model
   openai:<lease name>` (`OpenAiAcpAgent.CLI_MODEL_PROVIDER`), keeping the wire
   model name identical to what the lease serves.
2. **Field gates must match the declared transcript capability** (this suite's
   two graders). The ACP bridge writes ATIF steps with tool calls but emits no
   per-step usage of its own (the adapter's declared capability is
   `token_usage: partial`); a `REQUIRED_FIELDS` gate that demands it turns a
   *decided* outcome into `cannot_judge` and blocks all nine trajectory
   metrics. Both graders now require `events` only; usage-dependent metrics
   report their own skip with the reason in the evidence.
3. **The metering ledger: where the "unavailable" fields come from**
   (`deepagents-eval-control/src/facade_call_log.ts`,
   `aeval/hooks/model_call_ledger.py`). The broker meters every call but keeps
   that accounting in host-side lease state, and the ACP server emits no
   `usage_update` — measured on the real run, `acp-summary.json` had
   `latest_usage_update: null`, every `step.timestamp` was null and
   `mock_call_log.jsonl` said `no_calls_observed`. The facade now writes one
   JSONL line per call (metered usage, requested tool names, start/end) when
   the deployment sets `AEVAL_FACADE_CALL_LOG`, and the collector reads that
   file out of the live sandbox, seals it into `mock_call_log.jsonl`, and folds
   totals/per-step usage/timestamps into the canonical transcript. Opt-in by
   declaration: `DcodeAgent.SANDBOX_MODEL_CALL_LEDGER` is the only adapter that
   declares the path, so every other agent's collection path is unchanged.
