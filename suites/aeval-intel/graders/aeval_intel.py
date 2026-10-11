"""Metric library for the aeval-intel quality families (v5).

Not an engine: this module holds **identity + the metric registry**. The
same library serves the ``intelligence``, ``redline`` and ``memory``
families — each family's declaration lists (and orders) the metrics it
judges, its own threshold and its own case set, so one module no longer
implies one rubric or one spec.

Inputs are per task and come from the sealed rubric copy
(``rubric/task_anchors.json``), bound **by metric id** in the family
declaration (``cases[task].inputs[<metric id>]``). The assembler hands
each factory exactly that metric's own inputs — this module keeps no
anchor vocabulary and no anchor parser, so there is no second place where
"which key feeds which metric" could drift. A task whose family declares
no inputs is ``cannot_judge`` at that layer (never a guessed score).
Rubric changes bump the family declaration (a run-identity change), so
``GRADER_VERSION`` tracks the ENGINE contract, not the data.

Identity contract: GRADER_ID / GRADER_VERSION / LAYER must match the
family declarations (``graders/aeval_intel.py@v5`` → now ``graders/aeval_intel.py@v6``,
trajectory). The metric list, threshold, veto and per-task inputs are all
declared; nothing about the rubric is mirrored in this module.
"""

from __future__ import annotations


from aeval.verdict.trajectory.presets import quality_metric_registry

GRADER_ID = "aeval-intel"
GRADER_VERSION = "v6"
LAYER = "trajectory"
REQUIRED_FIELDS = ["events", "token_usage"]


#: metric id → factory(该指标自己的输入).
METRICS = quality_metric_registry()

