# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""With netbox-branching installed, a core delete in a branch changes only the branch copy of plugin data.

A revert of a merged branch that cannot restore plugin data is refused.
"""

import importlib
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest

from netbox_data_import import branching

if not branching.installed():
    pytest.skip("netbox-branching is not an installed app", allow_module_level=True)

from core.models import ObjectChange, ObjectType
from dcim.choices import RackFormFactorChoices
from dcim.models import Cable, Device, Interface, RackType
from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured, MiddlewareNotUsed
from django.core.management import call_command
from django.db import connection, models
from django.db.migrations.loader import MigrationLoader
from django.test import RequestFactory, SimpleTestCase, TransactionTestCase
from extras.models import Tag, TaggedItem
from netbox.context_managers import event_tracking
from netbox_branching import utilities as branching_utilities
from netbox_branching.choices import BranchStatusChoices
from netbox_branching.models import Branch
from netbox_branching.models.branches import _fake_for_branch
from netbox_branching.utilities import BranchActionIndicator, activate_branch, supports_branching
from utilities.exceptions import AbortTransaction

from netbox_data_import.models import (
    CableImportSource,
    ClassRoleMapping,
    DeviceImportSource,
    ImportProfile,
)
from netbox_data_import.tests.helpers import make_dcim_objects, provision_branch
from netbox_data_import.tests.plugins_config import override_plugins_config

APP_LABEL = "netbox_data_import"
# Changing this set is a design decision: branches opened before the change lack the new table.
BRANCHABLE_MODELS = {
    "netbox_data_import.CableImportSource",
    "netbox_data_import.ClassRoleMapping",
    "netbox_data_import.DeviceImportSource",
}


def _plugin_models():
    return apps.get_app_config(APP_LABEL).get_models()


def _references_a_branchable_model(model) -> bool:
    """Restate the rule: a concrete foreign key to a branchable model outside the plugin."""
    return any(
        isinstance(field, models.ForeignKey)
        and field.related_model._meta.app_label != APP_LABEL
        and supports_branching(field.related_model)
        for field in model._meta.concrete_fields
    )


class BranchabilityRuleTest(SimpleTestCase):
    """Guard 2: the branchable plugin models follow the foreign-key rule and match the pinned set."""

    def test_supports_branching_follows_the_rule_for_every_plugin_model(self):
        for model in _plugin_models():
            with self.subTest(model=model._meta.label):
                self.assertEqual(supports_branching(model), _references_a_branchable_model(model))

    def test_the_branchable_set_is_the_pinned_set(self):
        branchable = {model._meta.label for model in _plugin_models() if _references_a_branchable_model(model)}

        self.assertEqual(
            branchable,
            BRANCHABLE_MODELS,
            "The branchable plugin models changed. Open branches lack the table of a newly branchable model, "
            "and keep the table of a model that stopped being branchable, so this change needs a design decision "
            "(docs/design/netbox-branching.md).",
        )

    def test_no_plugin_model_references_a_branchable_plugin_model(self):
        references = sorted(
            f"{model._meta.label}.{field.name}"
            for model in _plugin_models()
            for field in model._meta.concrete_fields
            if isinstance(field, models.ForeignKey)
            and field.related_model._meta.app_label == APP_LABEL
            and supports_branching(field.related_model)
        )

        self.assertEqual(
            references,
            [],
            "These fields reference a branchable plugin model, and the rule does not follow them: a delete in "
            "a branch cascades through the branch copy into main's table of the referencing model. Making that "
            "model branchable leaves open branches without its table, so this needs a design decision "
            "(docs/design/netbox-branching.md).",
        )

    def test_the_resolver_defers_for_other_apps(self):
        self.assertIsNone(branching.is_branchable(Device))

    def test_the_active_branch_is_the_branching_context(self):
        branch = Branch(name="context only")

        with activate_branch(branch):
            self.assertIs(branching.active_branch(), branch)
        self.assertIsNone(branching.active_branch())


class SuiteConfigurationTest(SimpleTestCase):
    """The test settings keep the base configuration's netbox-branching settings."""

    def test_the_suite_keeps_the_base_netbox_branching_settings(self):
        from django.conf import settings
        from netbox import configuration as base

        base_settings = getattr(base, "PLUGINS_CONFIG", {}).get("netbox_branching", {})
        suite_settings = settings.PLUGINS_CONFIG["netbox_branching"]

        self.assertTrue(
            base_settings, "Set one netbox_branching setting in the base configuration, or this passes empty."
        )
        self.assertEqual({key: suite_settings.get(key) for key in base_settings}, base_settings)


class StartupValidationTest(SimpleTestCase):
    """register() refuses a configuration or a release that breaks the resolver."""

    def setUp(self):
        self.resolvers = branching_utilities._branching_resolvers
        self.addCleanup(self.resolvers.__setitem__, slice(None), list(self.resolvers))

    @override_plugins_config(netbox_branching={"exempt_models": ["netbox_data_import.*"]})
    def test_an_exempt_plugin_fails_startup(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "netbox_data_import.DeviceImportSource"):
            branching.register()

    def test_a_release_older_than_1_2_fails_startup(self):
        config = apps.get_app_config("netbox_branching")
        self.addCleanup(setattr, config, "version", config.version)
        config.version = "1.1.1"

        with self.assertRaisesMessage(ImproperlyConfigured, "1.1.1"):
            branching.register()

    def test_a_1_2_release_candidate_starts(self):
        config = apps.get_app_config("netbox_branching")
        self.addCleanup(setattr, config, "version", config.version)
        config.version = "1.2.0rc1"

        branching.register()

    def test_without_the_installed_app_nothing_is_active_or_registered(self):
        registered = list(self.resolvers)
        apps.set_available_apps(
            [config.name for config in apps.get_app_configs() if config.label != "netbox_branching"]
        )
        self.addCleanup(apps.unset_available_apps)

        with activate_branch(Branch(name="not installed")):
            self.assertIsNone(branching.active_branch())
            branching.refuse_branch()
            self.assertIsNone(branching.request_refusal(RequestFactory().get("/", {"_branch": "any"})))
        branching.register()

        self.assertEqual(self.resolvers, registered)
        with self.assertRaises(MiddlewareNotUsed):
            branching.BranchRefusalMiddleware(lambda request: None)


class StoredFeaturesTest(TransactionTestCase):
    """migrate stores the resolver's answer, which provisioning and the router read."""

    def test_migrate_stores_branching_for_the_branchable_models_only(self):
        call_command("migrate", verbosity=0)

        for model in _plugin_models():
            with self.subTest(model=model._meta.label):
                features = ObjectType.objects.filter(app_label=APP_LABEL, model=model._meta.model_name).values_list(
                    "features", flat=True
                )
                self.assertEqual("branching" in features.get(), model._meta.label in BRANCHABLE_MODELS)


class BranchMigrateTest(SimpleTestCase):
    """netbox-branching obeys each plugin migration's `fake_on_branch`, as guard 3 assumes."""

    def test_a_branch_migrate_obeys_each_flag(self):
        loader = MigrationLoader(None, load=False)
        loader.load_disk()
        flags = {
            name: getattr(importlib.import_module(f"{APP_LABEL}.migrations.{name}"), "fake_on_branch", None)
            for app_label, name in loader.disk_migrations
            if app_label == APP_LABEL
        }
        flagged = sorted(name for name, flag in flags.items() if flag is not None)
        self.assertTrue(flagged)

        for name in flagged:
            with self.subTest(migration=name):
                self.assertIs(_fake_for_branch(loader.disk_migrations[(APP_LABEL, name)]), flags[name])


@dataclass
class CascadeCase:
    """One core object a plugin row references, and how the plugin row shows its delete."""

    name: str
    delete: Callable[[], None]
    observe: Callable[[], Any]
    deleted: Any
    kept: Any


class _BranchLifecycleTest(TransactionTestCase):
    """Provisioned branches and the core deletes that reach plugin data."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # A deployment runs migrate after the upgrade, which stores the resolver's answer.
        call_command("migrate", verbosity=0)

    def setUp(self):
        self.user = get_user_model().objects.create_user("branch-user")
        self.profile = ImportProfile.objects.create(name="Branch profile")

    def _logged(self, action):
        request = RequestFactory().get("/")
        request.user = self.user
        request.id = uuid.uuid4()
        with event_tracking(request):
            return action()

    def _in_branch(self, branch, action):
        with activate_branch(branch):
            return self._logged(action)

    def _device_case(self):
        site, _manufacturer, device_type, role = make_dcim_objects("Cascade")
        device = Device.objects.create(name="cascade-device", site=site, device_type=device_type, role=role)
        DeviceImportSource.objects.create(device=device, profile=self.profile, source_id="row-1")
        return CascadeCase(
            name="device",
            delete=lambda: Device.objects.get(pk=device.pk).delete(),
            observe=lambda: DeviceImportSource.objects.filter(device_id=device.pk).exists(),
            deleted=False,
            kept=True,
        )

    def _cable_case(self):
        site, _manufacturer, device_type, role = make_dcim_objects("Cable")
        ends = []
        for name in ("cable-a", "cable-b"):
            device = Device.objects.create(name=name, site=site, device_type=device_type, role=role)
            ends.append(Interface.objects.create(device=device, name="eth0", type="1000base-t"))
        cable = Cable(a_terminations=[ends[0]], b_terminations=[ends[1]])
        cable.save()
        CableImportSource.objects.create(cable=cable, profile=self.profile, trace_identity='["t"]', segment_index=0)
        return CascadeCase(
            name="cable",
            delete=lambda: Cable.objects.get(pk=cable.pk).delete(),
            observe=lambda: CableImportSource.objects.filter(cable_id=cable.pk).exists(),
            deleted=False,
            kept=True,
        )

    def _rack_type_case(self):
        _site, manufacturer, _device_type, _role = make_dcim_objects("Rack")
        rack_type = RackType.objects.create(
            manufacturer=manufacturer,
            model="Cascade rack",
            slug="cascade-rack",
            form_factor=RackFormFactorChoices.TYPE_4POST,
        )
        mapping = ClassRoleMapping.objects.create(
            profile=self.profile, source_class="Cabinet", creates_rack=True, rack_type=rack_type
        )
        return CascadeCase(
            name="rack type",
            delete=lambda: RackType.objects.get(pk=rack_type.pk).delete(),
            observe=lambda: ClassRoleMapping.objects.get(pk=mapping.pk).rack_type_id,
            deleted=None,
            kept=rack_type.pk,
        )

    def _tag_case(self):
        tag = Tag.objects.create(name="Cascade", slug="cascade")
        self.profile.tags.add(tag)
        assignment = TaggedItem.objects.filter(
            content_type=ObjectType.objects.get_for_model(ImportProfile), object_id=self.profile.pk, tag_id=tag.pk
        )
        return CascadeCase(
            name="tag",
            delete=lambda: Tag.objects.get(pk=tag.pk).delete(),
            observe=assignment.exists,
            deleted=False,
            kept=True,
        )

    def _cases(self):
        return (self._device_case, self._cable_case, self._rack_type_case, self._tag_case)


class BranchCascadeTest(_BranchLifecycleTest):
    """A Device, Cable, RackType or Tag delete in a branch reaches main only through a merge."""

    def test_a_provisioned_branch_holds_the_plugin_tables(self):
        branch = provision_branch(self, "tables")
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = %s", [branch.schema_name]
            )
            tables = {row[0] for row in cursor.fetchall()}

        self.assertIn("extras_taggeditem", tables)
        self.assertEqual(
            {table for table in tables if table.startswith(f"{APP_LABEL}_")},
            {apps.get_model(label)._meta.db_table for label in BRANCHABLE_MODELS},
        )

    def test_a_delete_in_a_branch_changes_only_the_branch_copy(self):
        for make_case in self._cases():
            case = make_case()
            with self.subTest(case=case.name):
                branch = provision_branch(self, f"delete {case.name}")

                self._in_branch(branch, case.delete)

                self.assertEqual(self._in_branch(branch, case.observe), case.deleted, "the branch copy is unchanged")
                self.assertEqual(case.observe(), case.kept, "the delete in the branch reached main")

    def test_a_merge_applies_the_delete_to_main(self):
        for make_case in self._cases():
            case = make_case()
            with self.subTest(case=case.name):
                branch = provision_branch(self, f"merge {case.name}")
                self._in_branch(branch, case.delete)
                self.assertEqual(case.observe(), case.kept, "the delete in the branch reached main before the merge")

                branch.merge(user=self.user)

                self.assertEqual(case.observe(), case.deleted, "the merge did not apply the delete to main")

    def test_a_discard_keeps_main(self):
        for make_case in self._cases():
            case = make_case()
            with self.subTest(case=case.name):
                branch = provision_branch(self, f"discard {case.name}")
                self._in_branch(branch, case.delete)
                self.assertEqual(self._in_branch(branch, case.observe), case.deleted, "the branch copy is unchanged")

                branch.delete()

                self.assertEqual(case.observe(), case.kept, "the discarded delete reached main")


class BranchRevertTest(_BranchLifecycleTest):
    """A revert that cannot restore plugin data is refused, and any other revert proceeds."""

    def _assert_revert_refused(self, branch, deleted_type):
        changes = ObjectChange.objects.count()

        indicator = branch.can_revert

        self.assertFalse(indicator.permitted, "the revert is permitted")
        self.assertEqual(
            indicator.message,
            f"NetBox Data Import data cannot be restored by a revert, and this branch deleted objects of these "
            f"types: {deleted_type}.",
        )
        with self.assertRaisesMessage(Exception, "Reverting this branch is not permitted."):
            branch.revert(user=self.user, commit=True)
        branch.refresh_from_db()
        self.assertEqual(branch.status, BranchStatusChoices.MERGED)
        self.assertEqual(ObjectChange.objects.count(), changes, "the refused revert changed main")

    def test_a_revert_after_a_merged_delete_is_refused(self):
        for make_case in self._cases():
            case = make_case()
            with self.subTest(case=case.name):
                branch = provision_branch(self, f"revert {case.name}")
                self._in_branch(branch, case.delete)
                branch.merge(user=self.user)

                self._assert_revert_refused(branch, case.name)

                self.assertEqual(case.observe(), case.deleted, "the refused revert changed main's plugin data")
                # From NetBox 4.7.2 a cable delete logs each end's disconnect, which branching undoes before the Cable.
                dry_run_ends = (AbortTransaction, Cable.DoesNotExist) if case.name == "cable" else AbortTransaction
                with self.assertRaises(dry_run_ends, msg="the dry run was refused"):
                    branch.revert(user=self.user, commit=False)

    def test_a_revert_after_a_synced_plugin_row_delete_is_refused(self):
        # Main deletes the core object, so the sync records a synthetic delete of the branch copy.
        for make_case, synthetic in ((self._device_case, "Device Import Source"), (self._tag_case, "tagged item")):
            case = make_case()
            with self.subTest(case=case.name):
                branch = provision_branch(self, f"sync {case.name}")
                self._logged(case.delete)
                branch.sync(user=self.user)
                self.assertEqual(self._in_branch(branch, case.observe), case.deleted, "the sync kept the branch copy")
                branch.merge(user=self.user)

                self._assert_revert_refused(branch, synthetic)

    def test_a_revert_after_a_merged_edit_restores_main(self):
        site, _manufacturer, device_type, role = make_dcim_objects("Edit")
        device = Device.objects.create(
            name="edited-device", site=site, device_type=device_type, role=role, description="before"
        )
        DeviceImportSource.objects.create(device=device, profile=self.profile, source_id="row-1")
        branch = provision_branch(self, "revert edit")

        def edit():
            edited = Device.objects.get(pk=device.pk)
            edited.snapshot()
            edited.description = "after"
            edited.save()

        self._in_branch(branch, edit)
        branch.merge(user=self.user)
        self.assertEqual(Device.objects.get(pk=device.pk).description, "after")

        self.assertEqual(branch.can_revert, BranchActionIndicator(True))
        branch.revert(user=self.user, commit=True)

        self.assertEqual(Device.objects.get(pk=device.pk).description, "before")
        self.assertTrue(DeviceImportSource.objects.filter(device_id=device.pk).exists())
