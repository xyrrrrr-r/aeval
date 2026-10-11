"""Metric library for the tbench-intel conversation-quality family (v5).

Not an engine: this module holds **identity + the metric registry** —
``METRICS`` maps a metric id to its factory, and the family declaration
(``families/conversation-quality.yaml`` → ``metrics:``) says which
of them this family judges, in judging order. The threshold and the veto
flag live in that declaration too; the framework builds the grader from
the declaration (``aeval.verdict.trajectory.assembly``), so nothing here
mirrors the family spec any more.

Inputs are per task and come from the sealed rubric copy
(``rubric/task_anchors.json``), bound **by metric id** in the family
declaration (``cases[task].inputs[<metric id>]``); the assembler hands
each factory exactly that metric's own inputs. This module keeps no anchor
vocabulary and no anchor parser. A task whose family declares no inputs is
``cannot_judge`` at this layer — never a guessed rubric.

Terminal tasks are single-instruction, so the family declares only the
four dimensions they actually feed (response brevity, tool selection,
hallucination screening, instruction following); the multi-turn
dimensions belong to families whose cases feed them.

Identity contract (aeval.verdict.loader): GRADER_ID / GRADER_VERSION /
LAYER must match the family declaration (``graders/quality.py@v4``,
trajectory). The metric list, threshold, veto and per-task inputs are all
declared; nothing about the rubric is mirrored here.
"""

from __future__ import annotations


from aeval.verdict.trajectory.presets import quality_metric_registry

GRADER_ID = "tbench-quality"
GRADER_VERSION = "v4"
LAYER = "trajectory"
REQUIRED_FIELDS = ["events", "token_usage"]


#: metric id → factory(该指标自己的输入). The family declaration selects
#: from this registry; the registry never selects the family.
METRICS = quality_metric_registry()

