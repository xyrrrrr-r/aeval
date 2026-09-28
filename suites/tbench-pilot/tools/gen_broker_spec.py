#!/usr/bin/env python3
"""Generate the pilot's broker spec from the suite's declared budgets.

The broker spec is operator input (``AEVAL_BROKER_JSON``), not suite
content — it carries the TLS material paths and the upstream credential
NAME. Everything budget-shaped, however, is declared once in
``budgets.yaml`` and injected here, because the broker's ``limits`` and
the trajectory grader's thresholds must be the same numbers:

  * ``limits.maxSteps`` / ``limits.maxTokens`` — enforced by the broker
    during the trial AND mirrored into the control config, so the
    sandbox adapter's lease identity check compares like with like;
  * ``maxOutputTokens`` — single-response cap;
  * ``auxiliaryPolicy`` — D47: compaction allowed (metered + ledgered),
    session-title left at the fail-closed default.

Usage::

    python3 tools/gen_broker_spec.py \
        --budgets budgets.yaml \
        --base /root/e2e/broker-spec-real.json \
        --out /root/e2e/broker-spec-tbench.json

The trusted token-count source is NOT carried over from the base spec.
The broker ties every request to it: with ``limits.maxTokens`` set, an
input bound that undercounts is worse than no bound at all, because the
post-dispatch check ("reported input tokens must not exceed the bound")
then fails closed and kills the trial even though nothing was over
budget — which is exactly what the first pilot run did: the base spec
pointed at the offline stub counter (a fixed 64 tokens for every
request), so every real provider call died with
``AEVAL_TOKEN_BOUND_VIOLATED``. The endpoint is therefore a required
operator input here whenever ``maxTokens`` is set.

The output is written 0600 because the spec names the credential
environment variable and the TLS material locations. Only the keys
listed above plus ``tokenCount.endpoint`` are overridden; every other
field (listen host/port, TLS, upstream) is carried over from the
verified base spec verbatim.
"""

from __future__ import annotations

import argparse
import json
import os
import tomllib
from pathlib import Path

OVERRIDABLE = {"limits", "maxOutputTokens", "auxiliaryPolicy", "tokenCount"}


def _load_budgets(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(text)
    except ImportError:  # pragma: no cover - the cluster ships PyYAML
        data = tomllib.loads(text) if path.suffix == ".toml" else None
        if data is None:
            raise SystemExit("PyYAML is required to read budgets.yaml")
    if not isinstance(data, dict) or "broker" not in data:
        raise SystemExit(f"{path} does not declare a broker budget block")
    return data


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--budgets", type=Path, required=True)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--token-count-endpoint",
        type=str,
        default=None,
        help=(
            "trusted input-token counter for the pinned upstream; REQUIRED "
            "when the budget sets maxTokens. It must never undercount the "
            "exact request body (an offline fixed-count stub does)."
        ),
    )
    args = parser.parse_args()

    budgets = _load_budgets(args.budgets)["broker"]
    base = json.loads(args.base.read_text(encoding="utf-8"))

    spec = json.loads(json.dumps(base))  # deep copy
    limits = dict(spec.get("limits") or {})
    limits["maxSteps"] = int(budgets["maxSteps"])
    limits["maxTokens"] = int(budgets["maxTokens"])
    spec["limits"] = limits
    spec["maxOutputTokens"] = int(budgets["maxOutputTokens"])
    if limits["maxTokens"] is not None:
        if not args.token_count_endpoint:
            raise SystemExit(
                "budgets set limits.maxTokens but no --token-count-endpoint was "
                "given: a counter has to be trusted for THIS upstream, and the "
                "base spec's (an offline stub with a fixed count) is not. Pass "
                "the operator's counter endpoint — tools/token_count.py serves "
                "one that cannot undercount."
            )
        existing = dict(spec.get("tokenCount") or {})
        existing["endpoint"] = str(args.token_count_endpoint)
        spec["tokenCount"] = existing
    policy = budgets.get("auxiliaryPolicy")
    if policy:
        spec["auxiliaryPolicy"] = {str(k): str(v) for k, v in policy.items()}
    else:
        spec.pop("auxiliaryPolicy", None)

    changed = {
        key: (base.get(key), spec.get(key))
        for key in OVERRIDABLE
        if base.get(key) != spec.get(key)
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n", "utf-8")
    os.chmod(args.out, 0o600)
    print(f"wrote {args.out}")
    for key, (before, after) in changed.items():
        print(f"  {key}: {before!r} -> {after!r}")
    if not changed:
        print("  (no change: the base spec already carries the pilot budget)")


if __name__ == "__main__":
    main()
