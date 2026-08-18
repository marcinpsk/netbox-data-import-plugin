# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Regression tests for repository CI configuration."""

from pathlib import Path
from unittest import TestCase

import yaml


class NetBoxMainWorkflowTest(TestCase):
    """Keep the NetBox main canary in validation mode."""

    def test_query_count_baselines_are_not_updated(self):
        workflow = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "test-netbox-main.yaml"

        self.assertNotIn("UPDATE_QUERY_COUNTS", workflow.read_text())

    def test_agent_instructions_start_with_a_level_one_heading(self):
        """Keep the repository agent instructions valid as standalone Markdown."""
        agent_instructions = Path(__file__).resolve().parents[2] / "AGENTS.md"

        self.assertTrue(agent_instructions.read_text().startswith("# Agent instructions\n"))

    def test_agent_instruction_sections_use_level_two_headings(self):
        """Do not skip a heading level below the document title."""
        agent_instructions = Path(__file__).resolve().parents[2] / "AGENTS.md"

        self.assertNotRegex(agent_instructions.read_text(), r"(?m)^#{3,}\s")

    def test_agent_instructions_use_shell_safe_test_values(self):
        """Keep the documented test command safe to paste into a shell."""
        agent_instructions = Path(__file__).resolve().parents[2] / "AGENTS.md"

        self.assertIn(
            "TEST_DB_NAME=test_unique_task TEST_REDIS_HOST=redis-sidecar-task netbox-test",
            agent_instructions.read_text(),
        )

    def test_tool_specific_instructions_point_at_the_shared_file(self):
        """Keep one source of truth so tool-specific files cannot drift."""
        root = Path(__file__).resolve().parents[2]

        self.assertIn("AGENTS.md", (root / "CLAUDE.md").read_text())
        self.assertFalse((root / ".github" / "copilot-instructions.md").exists())

    def test_licensing_guidance_does_not_hardcode_a_year(self):
        """A fixed example year makes every new file look like a mismatch."""
        licensing = (Path(__file__).resolve().parents[2] / "docs" / "agents" / "licensing.md").read_text()

        self.assertIn("SPDX-FileCopyrightText: <year>", licensing)
        self.assertNotRegex(licensing, r"SPDX-FileCopyrightText: 20\d\d")
        self.assertNotRegex(licensing, r"Copyright \(C\) 20\d\d")

    def test_devcontainer_setup_installs_playwright_and_chromium(self):
        """Keep the standalone browser helpers runnable after setup."""
        setup_script = Path(__file__).resolve().parents[2] / ".devcontainer" / "scripts" / "setup.sh"
        setup = setup_script.read_text()

        self.assertIn("pytest-xdist ruff pre-commit playwright", setup)
        self.assertIn("python -m playwright install --with-deps chromium", setup)

    def test_javascript_workflow_does_not_persist_checkout_credentials(self):
        """Do not expose the workflow token to pull-request JavaScript."""
        workflow = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "js-test.yaml"
        steps = yaml.safe_load(workflow.read_text())["jobs"]["test-js"]["steps"]
        checkout = next(step for step in steps if str(step.get("uses", "")).startswith("actions/checkout@"))

        self.assertIs(checkout.get("with", {}).get("persist-credentials"), False)


class ReleaseWorkflowTest(TestCase):
    """Keep the release workflow unprivileged where it runs pull-request code, and serial."""

    def _jobs(self):
        workflow = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "release.yaml"

        return yaml.safe_load(workflow.read_text())["jobs"]

    def _build_command_job(self):
        return self._jobs()["build-command"]

    def test_the_release_job_ignores_its_own_release_commit(self):
        """A token pushes the release commit, so the push starts this workflow again."""
        self.assertIn("chore(release):", self._jobs()["semantic-release"]["if"])

    def test_the_release_job_runs_one_at_a_time(self):
        """Two merges close together must not race two releases onto the same tag."""
        concurrency = self._jobs()["semantic-release"]["concurrency"]

        self.assertEqual(concurrency["group"], "release")
        self.assertIs(concurrency["cancel-in-progress"], False)

    def test_the_build_command_job_only_reads(self):
        """The job runs the build command the pull request itself writes."""
        self.assertEqual(self._build_command_job()["permissions"], {"contents": "read"})

    def test_the_build_command_job_does_not_persist_checkout_credentials(self):
        """Do not leave the workflow token in the Git configuration for pull-request code."""
        steps = self._build_command_job()["steps"]
        checkout = next(step for step in steps if str(step.get("uses", "")).startswith("actions/checkout@"))

        self.assertIs(checkout.get("with", {}).get("persist-credentials"), False)


class WorkflowAuditTest(TestCase):
    """Keep the GitHub Actions audit gating pull requests, not local commits alone."""

    def test_the_lint_workflow_audits_the_workflows(self):
        workflow = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "lint-format.yaml"
        steps = yaml.safe_load(workflow.read_text())["jobs"]["format-and-lint"]["steps"]

        self.assertTrue(any("zizmor" in str(step.get("run", "")) for step in steps))


class StackedPullRequestTest(TestCase):
    """A pull request that targets a working branch must still run the gating checks."""

    GATING_WORKFLOWS = ["test.yaml", "codeql.yml", "js-test.yaml", "lint-format.yaml"]

    def test_the_gating_workflows_accept_any_base_branch(self):
        """A stack targets working branches, so a base-branch filter leaves it untested."""
        for name in self.GATING_WORKFLOWS:
            with self.subTest(workflow=name):
                workflow = Path(__file__).resolve().parents[2] / ".github" / "workflows" / name
                parsed = yaml.safe_load(workflow.read_text())
                triggers = parsed.get("on", parsed.get(True))

                self.assertNotIn("branches", triggers["pull_request"] or {})
