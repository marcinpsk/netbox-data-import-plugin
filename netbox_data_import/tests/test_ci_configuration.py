# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Regression tests for repository CI configuration."""

import re
import shlex

from pathlib import Path
from unittest import TestCase

import yaml

_XDIST_OPTIONS = ("-n", "--numprocesses")
_SHELL_SEPARATORS = {"&&", "||", "|", ";", "&"}


def _is_pytest(token):
    """Return whether *token* starts a pytest run, under either entry-point name."""
    return token.rsplit("/", 1)[-1] in ("pytest", "py.test") or token == "-mpytest"


def _worker_count_at(tokens, index):
    """Return the value the xdist option at *index* carries, or None when it is not one."""
    token = tokens[index]
    if token in _XDIST_OPTIONS:
        return tokens[index + 1] if index + 1 < len(tokens) else None
    for option in _XDIST_OPTIONS:
        if token.startswith(f"{option}="):
            return token[len(option) + 1 :]
    # xdist takes the count attached to the short option, as `-n4`. Options that merely contain
    # an `n` are left alone: pytest reads `-kn4` as `-k n4`, which selects tests and sets nothing.
    return token[2:] if token.startswith("-n") and len(token) > 2 else None


def fixed_xdist_worker_counts(command):
    """Return every fixed worker count *command* passes to pytest, ignoring `auto`.

    The reading is deliberately conservative. conftest.py refuses a count above the isolation
    ceiling at run time, so a spelling this misses still cannot break a run, while a false
    positive would fail the workflow over a command that never reaches xdist.
    """
    counts = []
    # A command may wrap over several lines, which puts the option and its value apart.
    for line in re.sub(r"\\\s*\n", " ", command).splitlines():
        try:
            # Tokenize before looking for separators, so a quoted `|` stays inside its argument.
            tokens = shlex.split(line)
        except ValueError:
            tokens = line.split()
        running_pytest = False
        for index, token in enumerate(tokens):
            if token in _SHELL_SEPARATORS:
                running_pytest = False
            elif _is_pytest(token):
                running_pytest = True
            elif running_pytest:
                # An option belongs to the command it follows, so a wrapper's `-n` is not pytest's.
                try:
                    counts.append(str(int(_worker_count_at(tokens, index))))
                except (TypeError, ValueError):
                    continue
    return counts


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


class TestWorkflowTest(TestCase):
    """Keep the test workflows on the worker count and the version pairing the repository decides."""

    WORKFLOWS = ["test.yaml", "test-netbox-main.yaml"]

    def _workflow(self, name):
        return Path(__file__).resolve().parents[2] / ".github" / "workflows" / name

    def _run_commands(self, name):
        """Return every shell command the workflow runs, so a comment cannot answer for one."""
        parsed = yaml.safe_load(self._workflow(name).read_text())
        return [
            step["run"]
            for job in parsed["jobs"].values()
            for step in job.get("steps", [])
            if isinstance(step.get("run"), str)
        ]

    def test_the_worker_count_matcher_reads_every_xdist_spelling(self):
        """The guard below is only worth as much as what it recognizes."""
        for command in (
            "pytest -n 4",
            "pytest -n=4",
            "pytest -n4",
            "pytest --numprocesses 4",
            "pytest --numprocesses=4",
            "pytest \\\n  -n 4",
            'pytest -n "4"',
            "pytest --numprocesses='4'",
            "pytest -n +4",
            'pytest "-n" 4',
            "python -m pytest -n 4",
            "uv run pytest -n 4",
            "python -mpytest -n4",
            "py.test -n4",
            "/usr/bin/pytest -n 4",
            "pytest --log-format='%(levelname)s | %(message)s' -n4",
        ):
            with self.subTest(command=command):
                self.assertEqual(fixed_xdist_worker_counts(command), ["4"])

        # `-n` belongs to many commands, and failing the guard on one of those helps nobody.
        for command in (
            "pytest -n auto",
            "pytest --numprocesses=auto",
            "pytest --dist loadscope",
            "pytest -k n4",
            "grep -name 4",
            "head -n 4 test.log",
            "head -n 4 test.log\npytest --dist loadscope",
            "nice -n 4 pytest",
            "pytest -p no:cacheprovider",
            "pytest --no-cov",
            "pytest -kn4",
            "pytest --dist loadscope && head -n 4 test.log",
        ):
            with self.subTest(command=command):
                self.assertEqual(fixed_xdist_worker_counts(command), [])

    def test_no_workflow_hardcodes_a_pytest_worker_count(self):
        """`conftest.py` caps `-n auto` at the workers that get private Redis databases.

        A number on the command line overrides the addopts and skips that reasoning.
        """
        for name in self.WORKFLOWS:
            for command in self._run_commands(name):
                with self.subTest(workflow=name, command=command):
                    self.assertEqual(fixed_xdist_worker_counts(command), [])

    def test_the_matrix_does_not_cap_the_parallel_jobs(self):
        """GitHub allocates the runners, so a fixed ceiling only makes the run longer."""
        strategy = yaml.safe_load(self._workflow("test.yaml").read_text())["jobs"]["test-netbox"]["strategy"]

        self.assertNotIn("max-parallel", strategy)

    def test_each_netbox_version_is_tested_on_one_python_version(self):
        """NetBox pins the Python versions it supports, so the plugin does not test them again."""
        matrix = yaml.safe_load(self._workflow("test.yaml").read_text())["jobs"]["test-netbox"]["strategy"]["matrix"]
        pythons_by_netbox = {}
        for leg in matrix["include"]:
            pythons_by_netbox.setdefault(leg["netbox-version"], set()).add(leg["python-version"])

        repeated = {version: sorted(pythons) for version, pythons in pythons_by_netbox.items() if len(pythons) > 1}
        self.assertEqual(repeated, {})


class ReleaseWorkflowTest(TestCase):
    """Keep the release workflow unprivileged where it runs pull-request code, and serial."""

    def _jobs(self):
        workflow = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "release.yaml"

        return yaml.safe_load(workflow.read_text())["jobs"]

    def _build_command_job(self):
        return self._jobs()["build-command"]

    def test_the_release_job_ignores_its_own_release_commit(self):
        """A token pushes the release commit, so the push starts this workflow again."""
        self.assertIn(
            "!startsWith(github.event.head_commit.message, 'chore(release):')",
            self._jobs()["semantic-release"]["if"],
        )

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

        self.assertTrue(any("pre-commit run --all-files zizmor" in str(step.get("run", "")) for step in steps))


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
