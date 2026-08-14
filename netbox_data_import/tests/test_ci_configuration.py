# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Regression tests for repository CI configuration."""

from pathlib import Path
from unittest import TestCase


class NetBoxMainWorkflowTest(TestCase):
    """Keep the NetBox main canary in validation mode."""

    def test_query_count_baselines_are_not_updated(self):
        workflow = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "test-netbox-main.yaml"

        self.assertNotIn("UPDATE_QUERY_COUNTS", workflow.read_text())

    def test_agent_instructions_start_with_a_level_one_heading(self):
        """Keep the repository agent instructions valid as standalone Markdown."""
        agent_instructions = Path(__file__).resolve().parents[2] / "CLAUDE.md"

        self.assertTrue(agent_instructions.read_text().startswith("# Agent skills\n"))

    def test_agent_instruction_sections_use_level_two_headings(self):
        """Do not skip a heading level below the document title."""
        agent_instructions = Path(__file__).resolve().parents[2] / "CLAUDE.md"

        self.assertNotIn("\n### ", agent_instructions.read_text())

    def test_agent_instructions_use_shell_safe_test_values(self):
        """Keep the documented test command safe to paste into a shell."""
        agent_instructions = Path(__file__).resolve().parents[2] / "CLAUDE.md"

        self.assertIn(
            "TEST_DB_NAME=test_unique_task TEST_REDIS_HOST=redis-sidecar-task netbox-test",
            agent_instructions.read_text(),
        )

    def test_devcontainer_setup_installs_playwright_and_chromium(self):
        """Keep the standalone browser helpers runnable after setup."""
        setup_script = Path(__file__).resolve().parents[2] / ".devcontainer" / "scripts" / "setup.sh"
        setup = setup_script.read_text()

        self.assertIn("pytest-xdist ruff pre-commit playwright", setup)
        self.assertIn("python -m playwright install --with-deps chromium", setup)
