"""Protect coverage reporting and the external-program boundary of frozen builds."""
import os
import sys

import pytest

from desktop import runtime
from scripts.update_coverage_badge import badge


def test_coverage_badge_uses_line_totals_for_all_runtime_modules():
    report = {"files": {name: {"summary": {"covered_lines": 9, "num_statements": 10}}
                        for name in ("priorfolio.py", "priorfolio_gui.py", "priorfolio_exports.py")}}
    assert badge(report)["message"] == "90.00%"
    del report["files"]["priorfolio_gui.py"]
    with pytest.raises(ValueError, match="GUI"):
        badge(report)


def test_unfrozen_runtime_keeps_the_users_environment(monkeypatch):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setenv("LD_LIBRARY_PATH", "/custom/lib")
    original = dict(os.environ)
    runtime.prepare()
    assert dict(os.environ) == original


@pytest.mark.parametrize("original", [None, "/system/custom/lib"])
def test_frozen_linux_restores_the_external_library_path(monkeypatch, original):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/frozen/bundle")
    if original is None:
        monkeypatch.delenv("LD_LIBRARY_PATH_ORIG", raising=False)
    else:
        monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", original)
    runtime.prepare()
    assert os.environ.get("LD_LIBRARY_PATH") == original
