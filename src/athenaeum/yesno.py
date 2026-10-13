# SPDX-License-Identifier: Apache-2.0
"""``YesNoScorer`` seam -- a narrow, local yes/no classifier contract (issue athenaeum#2063).

**Why a separate seam, not a widened ``DecisionBackend``.** Jev's
:class:`athenaeum.decision_provider.DecisionBackend` already answers typed
yes/no ("noul") questions, but over HTTP against a hosted model. Maya runs
locally and answers exactly one shape of question: "is this string a way to
contact a specific human?", batched. Folding Maya into ``DecisionBackend``
would let a misconfigured ``choice``/``score`` call site be pointed at a
model that cannot answer either question and cannot refuse -- a
configurable-but-broken knob, which :mod:`athenaeum.decision_provider`'s own
module docstring already rejects for Jev. See the issue athenaeum#2063 issue
body's "Seam decision" section for the full rejected-alternatives list. A
future ``DecisionBackendYesNo`` adapter may wrap a ``DecisionBackend`` as a
:class:`YesNoScorer` (narrowing a wide contract is safe); the reverse is not
built, and no call site needs it today.

**Contract.** :class:`YesNoScorer` is a ``Protocol`` with one method,
``score(question, texts) -> list[float]`` -- batch-first because the real
adapter is a subprocess and a per-string launch would pay a model-load cost
per string. A test substitutes a fake implementing this protocol; no test in
this repo imports torch or transformers.

**The real adapter runs out of process.** :class:`SubprocessYesNoScorer`
launches an operator-named interpreter (one with ``torch``/``transformers``
installed) against ``_yesno_worker.py``, which has no ``athenaeum`` import
at all and is checked by AST scan (``tests/test_yesno_worker.py``). Token
TEXT travels only on the subprocess's stdin, never in argv (argv is visible
to ``ps`` on a shared host) and never logged.

**Fail closed, every time.** Any gap -- unset/missing weights or
interpreter, a worker that crashes, times out, or returns a malformed or
out-of-range row, or the wrong row count -- raises
:class:`YesNoScorerUnavailable`. The WHOLE batch is discarded; there is no
partial-batch path. :func:`preflight_yesno_scorer` is the loud, non-raising
startup check a caller runs BEFORE constructing a real adapter, mirroring
:func:`athenaeum.decision_provider.preflight_decision_provider`'s "a loud
message, never a silent fallback" contract.

**Layering:** L3 service, sibling of :mod:`athenaeum.decision_provider`.
Module scope imports only the stdlib, and it never imports ``torch`` or
``transformers`` -- those live only inside the separate worker process this
module launches.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol, runtime_checkable

log = logging.getLogger(__name__)

#: Default timeout (seconds) for one whole-batch worker invocation.
DEFAULT_TIMEOUT_SECS = 300.0

#: The worker module this adapter launches, as a filesystem path -- resolved
#: at call time (not import time) so a test can override it via the
#: ``worker_path`` constructor argument without ever importing this file's
#: module-scope torch-free guarantee into question.
_DEFAULT_WORKER_PATH = Path(__file__).resolve().with_name("_yesno_worker.py")


class YesNoScorerUnavailable(RuntimeError):
    """Raised when a :class:`YesNoScorer` cannot answer this batch at all.

    Every raise site names a reason class (never embeds token text) --
    issue athenaeum#2063's "exception text never embeds a scored token" rule,
    mirroring :mod:`athenaeum.sensitivity_routing`'s AC10.
    """


@runtime_checkable
class YesNoScorer(Protocol):
    """A local yes/no classifier, batched.

    A real implementation never raises for an ordinary "no" answer --
    :class:`YesNoScorerUnavailable` is reserved for "this scorer cannot
    answer at all right now", never for a low probability.
    """

    def score(self, question: str, texts: Sequence[str]) -> list[float]:
        """Return ``P(yes)`` in ``[0, 1]`` for each of *texts*, in order."""
        ...


class SubprocessYesNoScorer:
    """:class:`YesNoScorer` over an out-of-process worker (issue athenaeum#2063).

    Launches ``<interpreter> -I <worker_path> <weights_path>``, writes one
    JSON-Lines row per text to the child's stdin, and reads back one meta
    row (``{"load_secs": ...}``) followed by exactly ``len(texts)`` score
    rows. Any shape violation raises :class:`YesNoScorerUnavailable` with no
    partial use of what WAS returned. The child's stderr is captured and
    discarded -- never relayed -- because a model traceback can echo its
    input text, which is exactly the exception-text leak this seam forbids.
    """

    def __init__(
        self,
        *,
        interpreter: str,
        weights_path: Path,
        worker_path: Path | None = None,
        timeout_secs: float = DEFAULT_TIMEOUT_SECS,
    ) -> None:
        self._interpreter = interpreter
        self._weights_path = weights_path
        self._worker_path = worker_path or _DEFAULT_WORKER_PATH
        self._timeout_secs = timeout_secs
        #: Seconds the worker spent loading the model on the most recent
        #: `score()` call -- instrumentation only, not part of the
        #: `YesNoScorer` protocol return shape.
        self.last_load_secs: float = 0.0

    def score(self, question: str, texts: Sequence[str]) -> list[float]:
        texts = list(texts)
        if not texts:
            self.last_load_secs = 0.0
            return []

        argv = [self._interpreter, "-I", str(self._worker_path), str(self._weights_path)]
        payload = "".join(json.dumps({"q": question, "t": text}) + "\n" for text in texts)

        try:
            proc = subprocess.run(
                argv,
                input=payload,
                capture_output=True,
                text=True,
                timeout=self._timeout_secs,
            )
        except subprocess.TimeoutExpired as exc:
            raise YesNoScorerUnavailable(
                f"yesno worker timed out after {self._timeout_secs}s"
            ) from exc
        except OSError as exc:
            raise YesNoScorerUnavailable(
                f"yesno worker failed to start: {exc.__class__.__name__}"
            ) from exc

        if proc.returncode != 0:
            raise YesNoScorerUnavailable(f"yesno worker exited {proc.returncode}")

        lines = [line for line in proc.stdout.splitlines() if line.strip()]
        if not lines:
            raise YesNoScorerUnavailable("yesno worker produced no output")

        meta_line, *score_lines = lines
        try:
            meta = json.loads(meta_line)
            self.last_load_secs = float(meta["load_secs"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise YesNoScorerUnavailable(
                f"yesno worker returned a malformed meta row: {exc.__class__.__name__}"
            ) from exc

        if len(score_lines) != len(texts):
            raise YesNoScorerUnavailable(
                f"yesno worker returned {len(score_lines)} row(s) for {len(texts)} input(s)"
            )

        out: list[float] = []
        for line in score_lines:
            try:
                row = json.loads(line)
                p_yes = float(row["p"])
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                raise YesNoScorerUnavailable(
                    f"yesno worker returned a non-float row: {exc.__class__.__name__}"
                ) from exc
            if not (0.0 <= p_yes <= 1.0):
                raise YesNoScorerUnavailable("yesno worker returned an out-of-range P(yes)")
            out.append(p_yes)
        return out


def preflight_yesno_scorer(*, interpreter: str | None, weights_path: str | None) -> str | None:
    """Return a loud, non-raising startup message if the real scorer cannot
    run, else ``None``. Never logs or embeds a scored value -- there is none
    at this point.

    Checked BEFORE constructing :class:`SubprocessYesNoScorer`, mirroring
    :func:`athenaeum.decision_provider.preflight_decision_provider`. Any gap
    here means "fall back to regex-only", never an exception into the
    calling command (issue athenaeum#2063's fail-closed table).
    """
    if not weights_path:
        return (
            "pii.hazard_classifier.weights_path (env ATHENAEUM_MAYA_WEIGHTS_PATH) is "
            "not set; PII hazard classifier falls back to regex-only"
        )
    if not Path(weights_path).expanduser().is_dir():
        return (
            f"pii.hazard_classifier.weights_path {weights_path!r} is not a directory; "
            "PII hazard classifier falls back to regex-only"
        )
    if not interpreter:
        return (
            "pii.hazard_classifier.interpreter (env ATHENAEUM_MAYA_PYTHON) is not set; "
            "PII hazard classifier falls back to regex-only"
        )
    resolved = shutil.which(interpreter)
    if resolved is None:
        candidate = Path(interpreter).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            resolved = str(candidate)
    if resolved is None:
        return (
            f"pii.hazard_classifier.interpreter {interpreter!r} was not found; "
            "PII hazard classifier falls back to regex-only"
        )
    return None


__all__ = [
    "YesNoScorer",
    "YesNoScorerUnavailable",
    "SubprocessYesNoScorer",
    "preflight_yesno_scorer",
    "DEFAULT_TIMEOUT_SECS",
]
