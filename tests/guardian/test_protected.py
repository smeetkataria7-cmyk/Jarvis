"""
Tests for the protected-path list.

These matter more than they look. If is_protected() ever returns False for
something in guardian/, the entire approval design silently stops existing —
and it stops existing quietly, with everything still appearing to work. So
every one of these is a regression test against a failure with no symptoms.
"""

import pytest

from guardian.protected import (
    ProtectedPathError,
    assert_all_allowed,
    is_protected,
    verify_integrity,
)


class TestGuardianIsUntouchable:
    @pytest.mark.parametrize("path", [
        "guardian/protected.py",
        "guardian/auth.py",
        "guardian/patcher.py",
        "guardian/server.py",
        "guardian/__init__.py",
        "guardian/anything_added_later.py",
    ])
    def test_guardian_files_are_protected(self, path):
        assert is_protected(path)

    def test_the_protected_list_cannot_edit_itself(self):
        # The specific attack this blocks: patch #1 removes the check,
        # patch #2 does whatever it likes.
        assert is_protected("guardian/protected.py")

    def test_guardian_tests_are_protected(self):
        # Deleting the tests that prove the gate works is equivalent to
        # deleting the gate, just slower.
        assert is_protected("tests/guardian/test_protected.py")


class TestSecretsAndConfig:
    @pytest.mark.parametrize("path", [
        "config.yaml",
        "config.example.yaml",
        ".gitignore",
        "requirements.txt",
        "run.py",
    ])
    def test_config_and_entrypoints_are_protected(self, path):
        assert is_protected(path)

    def test_root_dotfiles_are_protected(self):
        assert is_protected(".env")
        assert is_protected(".github")


class TestDangerousFileTypes:
    @pytest.mark.parametrize("path", [
        "core/helper.sh",
        "core/setup.bash",
        "core/workflow.yml",
        "core/config.yaml",
        "core/daemon.service",
        "core/payload.so",
        "core/thing.exe",
    ])
    def test_executable_and_config_types_are_protected(self, path):
        # A self-editing assistant has no business writing shell scripts:
        # they run outside the review path and with wider authority than the
        # patch that introduced them.
        assert is_protected(path)


class TestPathTraversal:
    @pytest.mark.parametrize("path", [
        "core/../guardian/auth.py",
        "core/../../etc/passwd",
        "../../../root/.ssh/authorized_keys",
        "core/./../guardian/protected.py",
    ])
    def test_traversal_is_blocked(self, path):
        assert is_protected(path)

    def test_absolute_paths_outside_repo_are_blocked(self):
        assert is_protected("/etc/passwd")
        assert is_protected("/root/.bashrc")

    def test_unresolvable_paths_fail_closed(self):
        # Anything we can't reason about confidently must come back True.
        assert is_protected("\x00nonsense")


class TestEditableSurface:
    @pytest.mark.parametrize("path", [
        "core/brain.py",
        "core/memory.py",
        "core/phone.py",
        "core/skills/weather.py",
        "tests/test_phone.py",
        "docs/notes.md",
    ])
    def test_core_remains_editable(self, path):
        # The gate is worthless if it blocks everything — Jarvis has to be
        # able to actually change something for self-editing to mean anything.
        assert not is_protected(path)


class TestAssertAllAllowed:
    def test_passes_when_all_paths_are_fine(self):
        assert_all_allowed(["core/brain.py", "core/skills/new.py"])

    def test_raises_on_a_single_bad_path(self):
        with pytest.raises(ProtectedPathError) as exc:
            assert_all_allowed(["core/brain.py", "guardian/auth.py"])
        assert "guardian/auth.py" in str(exc.value)

    def test_reports_every_bad_path(self):
        with pytest.raises(ProtectedPathError) as exc:
            assert_all_allowed(["guardian/auth.py", "config.yaml", "core/ok.py"])
        message = str(exc.value)
        assert "guardian/auth.py" in message
        assert "config.yaml" in message

    def test_empty_list_is_allowed(self):
        assert_all_allowed([])


class TestIntegrity:
    def test_guardian_is_intact(self):
        assert verify_integrity() == []
