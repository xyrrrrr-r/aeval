# aeval-control — the agent-neutral control plane

The host-side **model broker** (`aeval-model-broker/3`: `GET /info`, `POST /stream`,
OpenAI chat-completions upstream) and the **gateway-lease client surface** every
agent control stack shares. This is the source of truth; agent packages
(`dsh-eval-control`, `deepagents-eval-control`) compose their own deployment
dists from here.

## Provenance

Moved byte-identical from `dsh-eval-control` @ commit `4bc6931` (the closure of
`broker_main`: `broker_main`, `host_broker`, `upstream`, `gateway_lease`,
`token_bound`, `config`, `stop_reason`, plus their tests). No behavior change.

## Invariants (enforced by aeval's test suite)

1. **Version pins must match the lock**: the `@deepseek-ai/*` dependency versions
   in `package.json` are asserted equal to `DSH_NPM_SLICE` in
   `src/aeval/provenance.py` — the build pin and the trial lock can never drift
   apart silently.
2. **The dist digest binds trials**: `aeval run` fingerprints whatever
   `controlDist` the operator's broker spec provides into the `RuntimeLock`
   (`ControlDistLock`), so a changed control build breaks comparability loudly.

## Build & test

```sh
npm install
npm run build   # tsc -> dist/
npm test        # node --test
```

The operator's broker spec (`AEVAL_BROKER_JSON`) may point `controlDist` at this
directory: the launcher resolves `<controlDist>/dist/broker_main.js`.
