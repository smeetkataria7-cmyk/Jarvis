"""
Tests for the self-edit proposer.

The behaviour worth protecting here is restraint. It is easy to build something
that proposes a change every time it is asked, and that would be actively
harmful: each proposal spends the user's willingness to read the next diff, and
once that runs out the fingerprint prompt becomes a reflex. So most of these
tests assert that nothing is proposed.
"""

import pytest

from core.memory import Memory
from core.selfedit import (
    FAILURE_THRESHOLD,
    MIN_ATTEMPTS,
    find_worst_action,
    gather_evidence,
    looks_like_unified_diff,
    propose_improvement,
)


@pytest.fixture
def memory(tmp_path):
    return Memory(tmp_path / "test.db")


def record(memory, action, ok_count, fail_count):
    for _ in range(ok_count):
        memory.record_episode(action, {}, "ok")
    for _ in range(fail_count):
        memory.record_episode(action, {"target": "Send"}, "failed", "couldn't find it")


class FakeBrain:
    """A brain that returns whatever it was handed."""

    def __init__(self, response):
        self.response = response
        self.calls = 0

    def chat(self, messages, system):
        self.calls += 1
        return self.response

    def intent(self, utterance, actions, context=""):
        raise NotImplementedError


class TestWhenToPropose:
    def test_silent_when_nothing_has_happened(self, memory):
        assert find_worst_action(memory) is None

    def test_silent_below_the_attempt_floor(self, memory):
        # Two failures out of two is a 100% failure rate and still not
        # evidence. One bad afternoon must not trigger a code change.
        record(memory, "ui_tap", ok_count=0, fail_count=2)
        assert find_worst_action(memory) is None

    def test_silent_when_mostly_working(self, memory):
        record(memory, "ui_tap", ok_count=9, fail_count=1)
        assert find_worst_action(memory) is None

    def test_speaks_up_on_sustained_failure(self, memory):
        record(memory, "ui_tap", ok_count=2, fail_count=8)
        worst = find_worst_action(memory)
        assert worst is not None
        assert worst["action"] == "ui_tap"
        assert worst["rate"] >= FAILURE_THRESHOLD

    def test_picks_only_the_single_worst(self, memory):
        # Several fixes at once produce a diff too long to read on a phone,
        # and a diff too long to read is a diff approved unread.
        record(memory, "ui_tap", ok_count=1, fail_count=9)
        record(memory, "send_sms", ok_count=4, fail_count=6)
        assert find_worst_action(memory)["action"] == "ui_tap"

    def test_threshold_boundaries_are_sane(self):
        assert MIN_ATTEMPTS >= 3
        assert 0 < FAILURE_THRESHOLD < 1


class TestEvidence:
    def test_evidence_names_the_action(self, memory):
        record(memory, "ui_tap", ok_count=0, fail_count=3)
        assert "ui_tap" in gather_evidence(memory, "ui_tap")

    def test_evidence_includes_failure_detail(self, memory):
        record(memory, "ui_tap", ok_count=0, fail_count=3)
        assert "couldn't find it" in gather_evidence(memory, "ui_tap")

    def test_successes_are_not_offered_as_evidence(self, memory):
        record(memory, "ui_tap", ok_count=5, fail_count=0)
        assert gather_evidence(memory, "ui_tap") == ""


class TestDiffRecognition:
    def test_accepts_a_real_diff(self):
        assert looks_like_unified_diff(
            "--- a/core/phone.py\n+++ b/core/phone.py\n@@ -1,3 +1,3 @@\n-old\n+new\n"
        )

    @pytest.mark.parametrize("text", [
        "Here's how I'd fix it: change line 40.",
        "",
        "NO_PATCH",
        "--- a/core/phone.py\n+++ b/core/phone.py\n",  # no hunk header
    ])
    def test_rejects_things_that_are_not_diffs(self, text):
        assert not looks_like_unified_diff(text)


class TestProposal:
    def test_proposes_nothing_when_healthy(self, memory):
        record(memory, "ui_tap", ok_count=10, fail_count=0)
        brain = FakeBrain("--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n")

        assert propose_improvement(memory, brain) is None
        # The expensive call should not even be made.
        assert brain.calls == 0

    def test_honours_no_patch(self, memory):
        # The model declining to change anything is a correct answer, not a
        # failure to be worked around.
        record(memory, "ui_tap", ok_count=1, fail_count=9)
        assert propose_improvement(memory, FakeBrain("NO_PATCH")) is None

    def test_discards_prose(self, memory):
        record(memory, "ui_tap", ok_count=1, fail_count=9)
        brain = FakeBrain("I think the problem is the tap selector logic.")
        assert propose_improvement(memory, brain) is None

    def test_returns_a_usable_proposal(self, memory):
        record(memory, "ui_tap", ok_count=1, fail_count=9)
        diff = "--- a/core/phone.py\n+++ b/core/phone.py\n@@ -1 +1 @@\n-old\n+new\n"

        proposal = propose_improvement(memory, FakeBrain(diff))

        assert proposal is not None
        assert proposal["diff"] == diff
        assert "ui_tap" in proposal["summary"]
        # The rationale must carry the evidence — a reviewer on a phone needs
        # to see why without going and querying the database.
        assert "ui_tap" in proposal["rationale"]

    def test_strips_code_fences(self, memory):
        record(memory, "ui_tap", ok_count=1, fail_count=9)
        inner = "--- a/core/phone.py\n+++ b/core/phone.py\n@@ -1 +1 @@\n-old\n+new"
        brain = FakeBrain(f"```diff\n{inner}\n```")

        proposal = propose_improvement(memory, brain)
        assert proposal is not None
        assert not proposal["diff"].startswith("```")

    @pytest.mark.parametrize("raw", [
        "--- a/core/phone.py\n+++ b/core/phone.py\n@@ -1 +1 @@\n-old\n+new",
        "--- a/core/phone.py\n+++ b/core/phone.py\n@@ -1 +1 @@\n-old\n+new\n\n\n",
        "```diff\n--- a/core/phone.py\n+++ b/core/phone.py\n@@ -1 +1 @@\n-old\n+new\n```",
    ])
    def test_diff_always_ends_with_exactly_one_newline(self, memory, raw):
        # git apply treats a diff with no final newline as truncated and
        # refuses it, so this is the difference between a patch that applies
        # and one that mysteriously never does.
        record(memory, "ui_tap", ok_count=1, fail_count=9)

        proposal = propose_improvement(memory, FakeBrain(raw))
        assert proposal is not None
        assert proposal["diff"].endswith("\n")
        assert not proposal["diff"].endswith("\n\n")
