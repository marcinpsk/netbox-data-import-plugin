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

    def test_agent_instructions_use_shell_safe_test_values(self):
        """Keep the documented test command safe to paste into a shell."""
        agent_instructions = Path(__file__).resolve().parents[2] / "CLAUDE.md"

        self.assertIn(
            "TEST_DB_NAME=test_unique_task TEST_REDIS_HOST=redis-sidecar-task netbox-test",
            agent_instructions.read_text(),
        )
