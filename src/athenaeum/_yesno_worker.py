#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Self-contained ``YesNoScorer`` worker subprocess (issue athenaeum#2063).

Run as ``<interpreter> -I _yesno_worker.py <weights_dir>`` -- a SEPARATE
interpreter athenaeum does not control the environment of (the Vitruvius
design's out-of-process adapter: a 1.7 GB model load stays out of the
deploy venv, CI, and the librarian's/lint's own process). Reads JSON-Lines
``{"q": ..., "t": ...}`` rows on stdin and writes JSON-Lines rows on
stdout: first ONE meta row ``{"load_secs": <float>}``, then exactly one
``{"p": <float in [0, 1]>}`` row per input row, in order. Any failure
(missing weights, missing packages, a bad row) is a non-zero exit with one
line on stderr -- never a partial score stream.

**No ``athenaeum`` import, anywhere in this file** -- checked by an AST scan
in ``tests/test_yesno_worker.py`` (issue athenaeum#2063 AC8). This file must
run standalone in an operator-named interpreter athenaeum itself may not be
installed into.

**Offline, always.** ``HF_HUB_OFFLINE``/``TRANSFORMERS_OFFLINE`` are set
BEFORE any ``transformers`` import (module scope, below), and every model
load passes ``local_files_only=True``. A missing local file raises; this
worker never fetches from the network (issue athenaeum#2063 AC8).

**One shared transform.** :func:`p_yes_from_logits` is the ONE place the
two-logit-to-probability transform is defined. ``scripts/eval_maya_pii_hazard.py``
imports it directly -- see issue athenaeum#2063's Vitruvius design, "two
findings that change what the eval numbers mean": the un-fixed eval script
used a plain softmax (temperature 1), but the weights' own
``maya_config.json`` defines ``P(yes)`` at temperature 1.65. Importing the
SAME function from this module is what makes eval and production incapable
of drifting on which transform is applied (issue athenaeum#2063 AC9).
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
from pathlib import Path
from typing import Any

# Must happen before any `transformers` import, anywhere in this process --
# issue athenaeum#2063 AC8 ("sets HF_HUB_OFFLINE / TRANSFORMERS_OFFLINE
# before importing transformers").
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")


class ModelUnavailable(RuntimeError):
    """Raised when the local model cannot be loaded at all."""


def p_yes_from_logits(logit_yes: float, logit_no: float, temperature: float = 1.0) -> float:
    """Maya's calibrated ``P(yes)``: ``sigmoid((logit_yes - logit_no) / temperature)``.

    At ``temperature=1.0`` this is exactly a two-class softmax's yes-column
    (the un-fixed eval script's prior behaviour); at ``temperature=1.65``
    (the weights' own ``maya_config.json`` value) it is the model's own
    calibration. Pure stdlib math -- no torch/transformers import -- so both
    this worker and ``scripts/eval_maya_pii_hazard.py`` can call it with
    neither package installed (issue athenaeum#2063 AC9).
    """
    if temperature <= 0:
        raise ValueError(f"temperature must be positive, got {temperature!r}")
    z = (logit_yes - logit_no) / temperature
    return 1.0 / (1.0 + math.exp(-z))


def read_temperature(weights_dir: "os.PathLike[str] | str") -> float:
    """Read the transform temperature from ``<weights_dir>/maya_config.json``.

    Defaults to ``1.0`` (no transform, i.e. a plain softmax) when the file
    is absent, unreadable, or does not declare a positive numeric
    ``temperature`` -- a missing/malformed file means "no custom
    temperature", never "scorer unavailable" (issue athenaeum#2063 AC9).
    """
    path = Path(weights_dir) / "maya_config.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return 1.0
    if not isinstance(raw, dict):
        return 1.0
    temp = raw.get("temperature")
    if isinstance(temp, (int, float)) and not isinstance(temp, bool) and temp > 0:
        return float(temp)
    return 1.0


def _resolve_yes_index(id2label: dict[Any, Any]) -> int:
    for idx, label in id2label.items():
        if str(label).strip().lower() == "yes":
            return int(idx)
    raise ModelUnavailable(
        f"model declares no 'yes' label in config.id2label ({id2label!r}); "
        "refusing to guess which output index is the yes-class"
    )


def load_model(weights_dir: str) -> tuple[Any, Any, Any, int | None]:
    """Load the tokenizer + model from *weights_dir*, local files only.

    Returns ``(torch, tokenizer, model, yes_index)``. ``yes_index`` is
    ``None`` for a single-logit head (``num_labels == 1``), which carries no
    yes/no label pair and is scored via a lone sigmoid instead.
    """
    try:
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer
    except ImportError as exc:
        raise ModelUnavailable(f"import failed: {exc.__class__.__name__}") from exc

    try:
        tokenizer = AutoTokenizer.from_pretrained(weights_dir, local_files_only=True)
        model = AutoModelForSequenceClassification.from_pretrained(
            weights_dir, local_files_only=True
        )
    except Exception as exc:
        raise ModelUnavailable(f"model load failed: {exc.__class__.__name__}") from exc
    model.eval()

    yes_index: int | None = None
    num_labels = getattr(model.config, "num_labels", None)
    if num_labels != 1:
        id2label = getattr(model.config, "id2label", None) or {}
        yes_index = _resolve_yes_index(id2label)
    return torch, tokenizer, model, yes_index


def score_one(
    torch: Any,
    tokenizer: Any,
    model: Any,
    yes_index: int | None,
    temperature: float,
    question: str,
    text: str,
) -> float:
    inputs = tokenizer(question, text, return_tensors="pt", truncation=True)
    with torch.no_grad():
        logits = model(**inputs).logits
    num_labels = logits.shape[-1]
    if num_labels == 1:
        return float(torch.sigmoid(logits)[0, 0])
    assert yes_index is not None
    logit_yes = float(logits[0, yes_index])
    others = [float(logits[0, i]) for i in range(num_labels) if i != yes_index]
    logit_no = max(others)
    return p_yes_from_logits(logit_yes, logit_no, temperature)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: _yesno_worker.py <weights_dir>", file=sys.stderr)
        return 2
    weights_dir = argv[1]
    temperature = read_temperature(weights_dir)

    start = time.perf_counter()
    try:
        torch, tokenizer, model, yes_index = load_model(weights_dir)
    except ModelUnavailable as exc:
        print(f"yesno worker: {exc}", file=sys.stderr)
        return 1
    load_secs = time.perf_counter() - start

    out_lines = [json.dumps({"load_secs": load_secs})]
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
            question = str(row["q"])
            text = str(row["t"])
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            print(f"yesno worker: malformed input row: {exc.__class__.__name__}", file=sys.stderr)
            return 1
        try:
            p_yes = score_one(torch, tokenizer, model, yes_index, temperature, question, text)
        except Exception as exc:  # noqa: BLE001 -- never let a scoring exception echo input
            print(f"yesno worker: scoring failed: {exc.__class__.__name__}", file=sys.stderr)
            return 1
        out_lines.append(json.dumps({"p": p_yes}))

    sys.stdout.write("\n".join(out_lines) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
