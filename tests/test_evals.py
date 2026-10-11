"""The eval pack is part of the test gate: the golden cases, the
leakage sweep, and the citation check must pass on the tree."""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_runner():
    path = Path(__file__).resolve().parents[1] / "evals" / "run_evals.py"
    spec = importlib.util.spec_from_file_location("run_evals", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_eval_pack_gates_pass():
    runner = _load_runner()
    payload = runner.run_offline()
    assert payload["leakage_violations_total"] == 0
    assert payload["citations_complete_all"] is True
    assert payload["brief_correctness"] >= 0.95
    assert payload["temporal_correctness"] >= 0.95
    assert payload["passed"] is True
