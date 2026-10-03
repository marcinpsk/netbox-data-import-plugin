# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Every policy write renders a deleted profile as a refusal, never as a server error.

`save_permission_scoped_object()` and `locked_profile_policy()` both raise
`ImportProfile.DoesNotExist` when the profile row is gone by the time the lock is taken. A preview
command takes that lock inside `apply_preview_command()`, which raises the same exception, so a
delete that lands before the lock leaves the view. A view that does not answer it returns HTTP 500.

`_PreviewCommandMixin` answers it once for every view that inherits it, directly or through another
mixin. A view that does not inherit it has to catch the exception itself. A write inside a Preview
Command runs under `apply_preview_command()`, so the view that applies the command answers for it.
"""

import ast
import pathlib

from django.contrib.auth import get_user_model
from django.test import Client, SimpleTestCase, TransactionTestCase
from django.urls import reverse

from netbox_data_import.models import ColumnMapping, ImportProfile, SourceResolution
from netbox_data_import.tests.helpers import (
    preview_claim,
    profile_deleted_at_the_policy_lock,
    upload_preview,
    workbook_bytes,
)

VIEWS = pathlib.Path(__file__).resolve().parents[1] / "views.py"
#: A mixin whose `dispatch()` answers the exception for every view that inherits it.
ANSWERING_MIXINS = frozenset({"_PreviewCommandMixin"})
#: A command's writes raise through the `apply_preview_command()` call that runs it.
COMMAND_BASES = frozenset({"PreviewCommand", "QueueImport"})
RAISING_CALLS = frozenset({"save_permission_scoped_object", "locked_profile_policy", "apply_preview_command"})
HANDLED = "ImportProfile.DoesNotExist"


def _inheriting(tree, roots):
    """Return *roots* and every class in *tree* that inherits one of them, directly or not."""
    bases = {
        node.name: {ast.unparse(base) for base in node.bases}
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
    }
    found = set(roots)
    while more := {name for name, parents in bases.items() if parents & found} - found:
        found |= more
    return found


def _caught_by(node):
    """Return every exception expression the `except` clauses of *node* name."""
    names = set()
    for handler in node.handlers:
        if handler.type is None:
            names.add("<bare>")
            continue
        parts = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
        names.update(ast.unparse(part) for part in parts)
    return names


def _scan(tree, raising):
    """Return unanswered call sites, and the module-level helpers that hold one.

    A helper that carries an unanswered write raises through its own callers, so the next pass
    treats a call to it as a write too. That is how a view stays accountable for a helper it calls.
    """
    unanswered, carriers = [], set()
    answering = _inheriting(tree, ANSWERING_MIXINS)
    commands = _inheriting(tree, COMMAND_BASES)

    def walk(node, tries, owner, function):
        if isinstance(node, ast.ClassDef):
            owner = node
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            function = node if owner is None else function
        if isinstance(node, ast.Try):
            for child in node.body:
                walk(child, [*tries, node], owner, function)
            for child in [*node.handlers, *node.orelse, *node.finalbody]:
                walk(child, tries, owner, function)
            return
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in raising:
            answered = owner is not None and (owner.name in answering or owner.name in commands)
            caught = {name for block in tries for name in _caught_by(block)}
            if not answered and HANDLED not in caught and "<bare>" not in caught:
                if owner is not None:
                    unanswered.append(f"{owner.name}:{node.lineno}")
                elif function is not None:
                    carriers.add(function.name)
                else:
                    unanswered.append(f"<module>:{node.lineno}")
        for child in ast.iter_child_nodes(node):
            walk(child, tries, owner, function)

    walk(tree, [], None, None)
    return unanswered, carriers


def _unanswered_policy_writes(tree):
    """Return every view call site that leaves a deleted profile unanswered."""
    raising = set(RAISING_CALLS)
    while True:
        unanswered, carriers = _scan(tree, raising)
        if carriers <= raising:
            return sorted(unanswered)
        raising |= carriers


class PolicyWriteRefusalTest(SimpleTestCase):
    """A deleted import profile is an answered refusal at every policy write."""

    def test_every_policy_write_answers_a_deleted_profile(self):
        unanswered = _unanswered_policy_writes(ast.parse(VIEWS.read_text(encoding="utf-8")))

        self.assertEqual(unanswered, [], "these policy writes would return HTTP 500 for a deleted profile")


class DeletedProfileIsNotFoundTest(TransactionTestCase):
    """The scanner proves a handler exists; these prove what the HTTP 404 handlers render.

    The two profile-child bases and the resolution delete view answer with `Http404`, which no other
    test exercises. The `messages` and JSON renderings are covered by the preview and proposal
    suites, so one real request per uncovered rendering is enough.
    """

    def setUp(self):
        """Create the profile, the child rows the requests name, and the operator who posts them."""
        self.profile = ImportProfile.objects.create(name="Vanishing Child Profile", adapter_config={})
        self.mapping = ColumnMapping.objects.create(
            profile=self.profile, source_column="Hostname", target_field="device_name"
        )
        self.resolution = SourceResolution.objects.create(
            profile=self.profile,
            source_id="RES-1",
            source_column="device_name",
            original_value="pristine",
            resolved_fields={"device_name": "decided"},
        )
        self.user = get_user_model().objects.create_superuser("vanish-child", "v@example.invalid", "testpass")
        self.client = Client()
        self.client.force_login(self.user)

    def _post_while_the_profile_vanishes(self, url, data, **extra):
        """POST to *url*, deleting the profile the moment the policy lock statement runs."""
        with profile_deleted_at_the_policy_lock(self.profile.pk) as deleted:
            response = self.client.post(url, data, **extra)
        self.assertEqual(deleted, [True], "the policy lock statement never ran, so no race was exercised")
        return response

    def test_editing_a_profile_child_reports_the_deleted_profile_as_not_found(self):
        response = self._post_while_the_profile_vanishes(
            reverse("plugins:netbox_data_import:columnmapping_edit", kwargs={"pk": self.mapping.pk}),
            {"profile": self.profile.pk, "source_column": "Renamed", "target_field": "device_name"},
        )

        self.assertEqual(response.status_code, 404, response.content[:300])
        # The delete cascades, so the edit had nothing left to rename either.
        self.assertFalse(ColumnMapping.objects.filter(source_column="Renamed").exists())

    def test_deleting_a_profile_child_reports_the_deleted_profile_as_not_found(self):
        response = self._post_while_the_profile_vanishes(
            reverse("plugins:netbox_data_import:columnmapping_delete", kwargs={"pk": self.mapping.pk}),
            {"confirm": "true"},
        )

        self.assertEqual(response.status_code, 404, response.content[:300])

    def test_deleting_a_resolution_reports_the_deleted_profile_as_not_found(self):
        response = self._post_while_the_profile_vanishes(
            reverse("plugins:netbox_data_import:source_resolution_delete", kwargs={"pk": self.resolution.pk}),
            {"confirm": "true"},
        )

        self.assertEqual(response.status_code, 404, response.content[:300])

    def _setup_data(self):
        """Return a setup form for one small workbook this profile can plan."""
        from dcim.models import Site
        from django.core.files.uploadedfile import SimpleUploadedFile

        site = Site.objects.create(name="Vanishing Site", slug="vanishing-site")
        upload = SimpleUploadedFile("vanishing.xlsx", workbook_bytes(["Hostname"], [["vanish-01"]]))
        return {"profile": self.profile.pk, "site": site.pk, "excel_file": upload}

    def test_a_preview_command_reports_the_deleted_profile_as_not_found(self):
        """A command takes the profile lock inside the coordinator, so the claim path answers it too."""
        upload = upload_preview(self.client, self._setup_data())
        self.assertEqual(upload.status_code, 302, upload.content[:300])

        response = self._post_while_the_profile_vanishes(
            reverse("plugins:netbox_data_import:preview_reread"),
            preview_claim(self.client),
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 404, response.content[:300])
        self.assertEqual(response.json(), {"ok": False, "error": "The import profile is no longer available."})

    def test_a_setup_reports_the_deleted_profile_as_not_found(self):
        """The setup command takes the same lock after its form validated the profile."""
        setup_url = reverse("plugins:netbox_data_import:import_setup")
        self.client.get(setup_url)

        response = self._post_while_the_profile_vanishes(
            setup_url, {**preview_claim(self.client), **self._setup_data()}
        )

        self.assertEqual(response.status_code, 404, response.content[:300])
