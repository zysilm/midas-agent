"""Unit tests for the HTA patch action classifier (issue #46 Fix 2).

Validates against the four real smoke-run patches plus a few synthetic
edge cases. If a real-patch classification regresses, refine the
heuristic — don't update the test to match.
"""
from pathlib import Path

import pytest

from midas_agent.workspace.hta.analysis.patch_classifier import (
    action_family, classify,
)

ROOT = Path(__file__).resolve().parents[2]
SMOKE_V1 = ROOT / ".midas/train/hta_fix_smoke/log/patches/ws-0"
SMOKE_V2 = ROOT / ".midas/train/hta_fix_smoke_v2/log/patches/ws-0"


def _read_patch(d: Path, needle: str) -> str:
    """Return the first patch file in ``d`` whose body mentions ``needle``."""
    if not d.exists():
        pytest.skip(f"smoke run dir missing: {d}")
    for p in sorted(d.glob("*.patch")):
        text = p.read_text()
        if needle in text:
            return text
    pytest.skip(f"no patch mentioning {needle!r} in {d}")


@pytest.mark.unit
class TestRealSmokePatches:
    def test_ep1_v2_12907_modify(self):
        patch = _read_patch(SMOKE_V2, "modeling/separable.py")
        cat = classify(patch)
        # The canonical fix is a one-line correction (= 1 -> = right).
        # Acceptable: modify_logic or modify_message.
        assert cat in ("modify_logic", "modify_message"), cat
        assert action_family(cat) == "modify"

    def test_ep3_v1_13236_remove(self):
        patch = _read_patch(SMOKE_V1, "table/table.py")
        cat = classify(patch)
        assert cat == "remove_behavior", cat
        assert action_family(cat) == "remove"

    def test_ep3_v2_13236_add_warning(self):
        patch = _read_patch(SMOKE_V2, "table/table.py")
        cat = classify(patch)
        assert cat == "add_warning", cat
        assert action_family(cat) == "add"

    def test_ep2_v2_13033_add_guard(self):
        patch = _read_patch(SMOKE_V2, "timeseries/core.py")
        cat = classify(patch)
        # The smoke v2 patch adds an "if len(self.colnames) < len(required_columns):"
        # branch with a raise inside it -> two new branch openers, no removals.
        # add_guard is the right call; modify_logic also acceptable as a fallback.
        assert cat in ("add_guard", "modify_logic", "add_branch"), cat


@pytest.mark.unit
class TestSyntheticEdgeCases:
    def test_empty_patch(self):
        assert classify("") == "mixed"
        assert classify("   \n") == "mixed"

    def test_pure_addition_of_warning(self):
        diff = """diff --git a/x.py b/x.py
--- a/x.py
+++ b/x.py
@@ -1,2 +1,4 @@
 def f():
+    import warnings
+    warnings.warn('soon', FutureWarning, stacklevel=2)
     return 1
"""
        assert classify(diff) == "add_warning"

    def test_block_deletion(self):
        diff = """diff --git a/x.py b/x.py
--- a/x.py
+++ b/x.py
@@ -1,7 +1,1 @@
 def f():
-    x = 1
-    y = 2
-    z = 3
-    if x:
-        return y
-    return z
+    pass
"""
        assert classify(diff) == "remove_behavior"

    def test_new_function(self):
        diff = """diff --git a/x.py b/x.py
--- a/x.py
+++ b/x.py
@@ -1,1 +1,3 @@
 # mod
+def helper(x):
+    return x
"""
        assert classify(diff) == "add_branch"

    def test_message_tweak(self):
        diff = """diff --git a/x.py b/x.py
--- a/x.py
+++ b/x.py
@@ -1,1 +1,1 @@
-    raise ValueError("old message")
+    raise ValueError("new message")
"""
        # +/- single line of equal shape; in a single file -> modify_logic.
        assert classify(diff) in ("modify_logic", "modify_message")


@pytest.mark.unit
class TestActionFamily:
    def test_remove_maps_to_remove(self):
        assert action_family("remove_behavior") == "remove"

    def test_adds_map_to_add(self):
        for c in ("add_warning", "add_guard", "add_branch"):
            assert action_family(c) == "add"

    def test_modifies_and_mixed_map_to_modify(self):
        for c in ("modify_message", "modify_logic", "mixed"):
            assert action_family(c) == "modify"
