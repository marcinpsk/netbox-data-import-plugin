# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""A quick action reads names and slugs from the request and writes them to bounded columns.

Every payload here is longer than the column it reaches. The view must refuse it. Letting the
value through raises `DataError: value too long` from the database, which is an HTTP 500.
"""

import pathlib

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from netbox_data_import.models import ImportProfile, ManufacturerMapping

LONG = "L" * 300
LONG_SLUG = "l" * 300

# One entry per posted value that reaches a bounded column, keyed by URL name.
OVERLENGTH_PAYLOADS = {
    "ignore_device": [
        {"source_id": LONG, "device_name": "widget-1"},
        {"source_id": "SRC-1", "device_name": LONG},
    ],
    "quick_add_class_mapping": [
        {"source_class": LONG, "mapping_action": "ignore"},
        {"source_class": "Controller", "mapping_action": "role", "role_slug": LONG_SLUG},
    ],
    "quick_add_column_mapping": [
        {"source_column": LONG, "target_field": "serial"},
        {"source_column": "Depth", "target_field": "extra_json:" + LONG},
    ],
    "quick_create_manufacturer": [
        {"mfg_name": LONG, "mfg_slug": "acme"},
        {"mfg_name": "Acme", "mfg_slug": LONG_SLUG},
    ],
    "quick_create_role": [
        {"name": LONG, "slug": "role-slug"},
        {"name": "Role", "slug": LONG_SLUG},
    ],
    "quick_resolve_device_type": [
        {"source_make": LONG, "source_model": "Widget"},
        {"source_make": "Acme", "source_model": LONG},
        {"source_make": "Acme", "source_model": "Widget", "netbox_mfg_slug": LONG_SLUG},
        {"source_make": "Acme", "source_model": "Widget", "netbox_dt_slug": LONG_SLUG},
        # The mapping holds 200 characters, but the NetBox manufacturer name holds 100.
        {"source_make": "M" * 150, "netbox_mfg_slug": "acme", "source_model": "Widget", "action": "create_now"},
        {"source_make": "Acme", "source_model": "Widget", "netbox_dt_name": "D" * 150, "action": "create_now"},
    ],
    "quick_resolve_manufacturer": [
        {"source_make": LONG, "netbox_mfg_slug": "acme"},
        {"source_make": "Acme", "netbox_mfg_slug": LONG_SLUG},
    ],
    # This action deletes a stored row but writes no bounded request value.
    "unignore_device": [],
}


# One entry per posted value that reaches a bounded column on a deferred row action.
ROW_ACTION_PAYLOADS = {
    "save_resolution": [
        {"source_id": LONG, "source_column": "_merge_serial", "resolved_fields": "{}"},
        {"source_id": "SRC-1", "source_column": "_merge_" + LONG, "resolved_fields": "{}"},
    ],
    "resolve_duplicate_name": [
        {"source_id": LONG, "row_number": "1", "new_name": "replacement"},
    ],
    "ignore_field_difference": [
        {"source_id": LONG, "row_number": "1", "target_field": "serial"},
    ],
    "match_existing_device": [
        {"source_id": LONG, "row_number": "1"},
    ],
    "sync_single_row": [
        {"row_number": "1"},
    ],
    # This one reads its source IDs from the stored preview rows, not from the request.
    "auto_match_devices": [
        {},
    ],
}


ROW_ACTION_CONTROL_PAYLOADS = {
    "save_resolution": {"source_id": "CONTROL-SAVE", "source_column": "serial", "resolved_fields": "{}"},
    "resolve_duplicate_name": {
        "source_id": "CONTROL-NAME",
        "row_number": "1",
        "new_name": "replacement-control",
    },
    "ignore_field_difference": {
        "source_id": "CONTROL-FIELD",
        "row_number": "1",
        "target_field": "serial",
    },
    "match_existing_device": {"source_id": "CONTROL-MATCH", "row_number": "1"},
    "sync_single_row": {"row_number": "1"},
    "auto_match_devices": {},
}


def _writer_database_state():
    """Return every row that the bounded request writers can affect."""
    from dcim.models import Device, DeviceRole, DeviceType, Manufacturer

    from netbox_data_import.models import (
        ClassRoleMapping,
        ColumnMapping,
        DeviceExistingMatch,
        DeviceImportSource,
        DeviceTypeMapping,
        IgnoredDevice,
        IgnoredFieldDifference,
        SourceResolution,
    )

    models = (
        ClassRoleMapping,
        ColumnMapping,
        DeviceExistingMatch,
        DeviceImportSource,
        DeviceTypeMapping,
        IgnoredDevice,
        IgnoredFieldDifference,
        ManufacturerMapping,
        SourceResolution,
        Device,
        DeviceRole,
        DeviceType,
        Manufacturer,
    )
    state = []
    for model in models:
        fields = [field.attname for field in model._meta.concrete_fields]
        rows = tuple(model.objects.order_by("pk").values_list(*fields))
        state.append((model._meta.label_lower, rows))
    return tuple(state)


def _permission_scoped_writer_url_names():
    """Return deferred writers, excluding the quick actions covered by their own ratchet."""
    from netbox_data_import import urls

    source = pathlib.Path(urls.__file__).with_name("views.py").read_text()
    writers = _permission_scoped_writer_class_names(source)
    names = set()
    for pattern in urls.urlpatterns:
        view_class = getattr(pattern.callback, "view_class", None)
        if view_class is not None and view_class.__name__ in writers:
            names.add(pattern.name)
    return names - _quick_action_url_names()


def _permission_scoped_writer_class_names(source):
    """Return classes whose methods call either permission-scoped write helper."""
    import ast

    helper_names = {"delete_permission_scoped_objects", "save_permission_scoped_object"}
    writers = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.ClassDef):
            continue
        if any(
            isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) and sub.func.id in helper_names
            for sub in ast.walk(node)
        ):
            writers.add(node.name)
    return writers


def _quick_action_routes():
    """Return routed preview writer class names keyed by their URL names."""
    from netbox_data_import import urls

    routes = {}
    for pattern in urls.urlpatterns:
        view_class = getattr(pattern.callback, "view_class", None)
        if view_class is None:
            continue
        if view_class.__name__.startswith("Quick") or view_class.__name__ in {
            "IgnoreDeviceView",
            "UnignoreDeviceView",
        }:
            routes[pattern.name] = view_class.__name__
    return routes


def _quick_action_url_names():
    """Return the URL name of every quick action the preview page can post."""
    return set(_quick_action_routes())


def _quick_action_write_seam_errors(source, routes):
    """Return quick-action classes that bypass the permission-scoped write seam."""
    import ast

    classes = {node.name: node for node in ast.parse(source).body if isinstance(node, ast.ClassDef)}
    scoped_writers = {"save_permission_scoped_object", "delete_permission_scoped_objects"}
    direct_mutations = {
        "save",
        "delete",
        "create",
        "update",
        "get_or_create",
        "update_or_create",
        "bulk_create",
        "bulk_update",
    }
    errors = []

    for url_name, class_name in sorted(routes.items()):
        view = classes[class_name]
        base_names = {getattr(base, "id", None) for base in view.bases}
        if "_PermissionScopedWriteMixin" not in base_names:
            errors.append(f"{url_name}: {class_name} lacks _PermissionScopedWriteMixin")

        post = next(
            (node for node in view.body if isinstance(node, ast.FunctionDef) and node.name == "post"),
            None,
        )
        if post is None:
            errors.append(f"{url_name}: {class_name} has no post()")
            continue

        post_calls = [node for node in ast.walk(post) if isinstance(node, ast.Call)]
        if not any(isinstance(call.func, ast.Name) and call.func.id in scoped_writers for call in post_calls):
            errors.append(f"{url_name}: {class_name}.post() does not call a scoped writer")
        for call in (node for node in ast.walk(view) if isinstance(node, ast.Call)):
            method = getattr(call.func, "attr", None)
            if method in direct_mutations:
                errors.append(f"{url_name}: {class_name} calls direct .{method}() at line {call.lineno}")

    return errors


class QuickActionInputBoundsTest(TestCase):
    """Each quick action writes request values straight to a column with a fixed width."""

    @classmethod
    def setUpTestData(cls):
        from dcim.models import DeviceRole, DeviceType, Manufacturer, Site

        from netbox_data_import.models import ClassRoleMapping

        cls.user = get_user_model().objects.create_superuser(
            username="quick-bounds-user", email="bounds@example.invalid", password="testpass"
        )
        cls.site = Site.objects.create(name="Quick Bounds Site", slug="quick-bounds-site")
        cls.manufacturer = Manufacturer.objects.create(
            name="Quick Bounds Vendor",
            slug="quick-bounds-vendor",
        )
        cls.device_type = DeviceType.objects.create(
            manufacturer=cls.manufacturer,
            model="Quick Bounds Model",
            slug="quick-bounds-vendor-quick-bounds-model",
            u_height=1,
        )
        cls.role = DeviceRole.objects.create(name="Quick Bounds Role", slug="quick-bounds-role")
        cls.profile = ImportProfile.objects.create(
            name="Quick Bounds Profile",
            adapter_config={
                "sheet_name": "Data",
                "source_id_column": "Id",
                "update_existing": True,
                "create_missing_device_types": False,
            },
        )
        ClassRoleMapping.objects.create(
            profile=cls.profile,
            source_class="Server",
            role_slug=cls.role.slug,
        )
        cls.existing_mapping = ManufacturerMapping.objects.create(
            profile=cls.profile,
            source_make="Acme",
            netbox_manufacturer_slug="before",
        )

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)

    def _device_row(self, source_id, device_name):
        """Return one complete canonical source row."""
        return {
            "_row_number": 1,
            "source_id": source_id,
            "device_name": device_name,
            "device_class": "Server",
            "make": self.manufacturer.name,
            "model": self.device_type.model,
            "u_height": "1",
            "rack_name": "",
            "u_position": "",
            "face": "",
            "serial": "",
            "asset_tag": "",
            "status": "active",
        }

    def _store_active_import(self, rows, result=None):
        """Store the source rows and active preview state used by row actions."""
        from netbox_data_import.views import _serialize_rows

        session = self.client.session
        session["import_rows"] = _serialize_rows(rows)
        session["import_context"] = {
            "profile_id": self.profile.pk,
            "site_id": self.site.pk,
            "location_id": None,
            "tenant_id": None,
            "filename": "quick-bounds.xlsx",
        }
        if result is not None:
            session["import_result"] = result.to_session_dict()
        else:
            session.pop("import_result", None)
        session["import_preview_pending"] = True
        session.save()

    def _prepare_row_action(self, url_name, payload, source_id, case_name):
        """Create the real active-import state one deferred action requires."""
        from dcim.models import Device

        from netbox_data_import.engine import run_import

        payload = dict(payload)
        device_name = f"bounds-{case_name}"
        row = self._device_row(source_id, device_name)

        if url_name in {"save_resolution", "resolve_duplicate_name"}:
            self._store_active_import([row])
        elif url_name == "match_existing_device":
            device = Device.objects.create(
                name=device_name,
                site=self.site,
                device_type=self.device_type,
                role=self.role,
            )
            payload["netbox_device_id"] = device.pk
            self._store_active_import([row])
        elif url_name == "auto_match_devices":
            row["serial"] = f"SERIAL-{case_name}"
            Device.objects.create(
                name=device_name,
                serial=row["serial"],
                site=self.site,
                device_type=self.device_type,
                role=self.role,
            )
            self._store_active_import([row])
        elif url_name == "ignore_field_difference":
            row["serial"] = f"SOURCE-{case_name}"
            Device.objects.create(
                name=device_name,
                serial=f"NETBOX-{case_name}",
                site=self.site,
                device_type=self.device_type,
                role=self.role,
            )
            result = run_import([row], self.profile, {"site": self.site}, dry_run=True, user=self.user)
            preview_row = next(item for item in result.rows if item.object_type == "device")
            self.assertEqual(preview_row.action, "update", preview_row.to_dict())
            self.assertIn("serial", preview_row.extra_data.get("field_diff", {}))
            self._store_active_import([row], result)
        elif url_name == "sync_single_row":
            result = run_import([row], self.profile, {"site": self.site}, dry_run=True, user=self.user)
            preview_row = next(item for item in result.rows if item.object_type == "device")
            self.assertEqual(preview_row.action, "create", preview_row.to_dict())
            self._store_active_import([row], result)
        else:  # pragma: no cover - the coverage ratchet keeps this branch unreachable
            self.fail(f"No active-import fixture for {url_name}")
        return payload

    def test_every_quick_action_is_covered(self):
        """A new quick action has to arrive with its own boundary payload."""
        self.assertEqual(_quick_action_url_names() - set(OVERLENGTH_PAYLOADS), set())

    def test_the_deferred_writer_scanner_recognizes_a_delete_only_view(self):
        """A routed delete-only writer must enter the same coverage ratchet as a save writer."""
        source = """
class DeleteOnlyView:
    def post(self):
        delete_permission_scoped_objects(user, queryset)
"""

        self.assertEqual(_permission_scoped_writer_class_names(source), {"DeleteOnlyView"})

    def test_the_write_seam_scanner_checks_helpers_on_the_view_class(self):
        source = """
class DeleteThroughHelper(_PermissionScopedWriteMixin):
    def _delete(self):
        Widget.objects.create()

    def post(self):
        delete_permission_scoped_objects(user, queryset)
"""

        errors = _quick_action_write_seam_errors(source, {"delete_widget": "DeleteThroughHelper"})

        self.assertTrue(
            any("DeleteThroughHelper calls direct .create()" in error for error in errors),
            errors,
        )

    def test_every_quick_action_uses_only_the_permission_scoped_write_seam(self):
        """A routed preview writer cannot bypass object constraints with a direct ORM write."""
        from netbox_data_import import urls

        source = pathlib.Path(urls.__file__).with_name("views.py").read_text()
        self.assertEqual(_quick_action_write_seam_errors(source, _quick_action_routes()), [])

    def test_an_overlength_quick_action_leaves_the_database_unchanged(self):
        """Reject each create or update without truncating or changing an existing row."""
        for url_name, payloads in OVERLENGTH_PAYLOADS.items():
            for index, payload in enumerate(payloads):
                with self.subTest(url_name=url_name, payload=index):
                    before = _writer_database_state()
                    response = self.client.post(
                        reverse(f"plugins:netbox_data_import:{url_name}"),
                        {"profile_id": self.profile.pk, **payload},
                    )

                    self.assertLess(
                        response.status_code,
                        500,
                        f"{url_name} payload {index} returned {response.status_code}",
                    )
                    self.assertEqual(
                        _writer_database_state(),
                        before,
                        f"{url_name} payload {index} changed a database row",
                    )

    def test_every_permission_scoped_writer_is_covered(self):
        """A new view that writes through the shared saver has to arrive with its own payload."""
        self.assertEqual(_permission_scoped_writer_url_names() - set(ROW_ACTION_PAYLOADS), set())
        self.assertEqual(set(ROW_ACTION_CONTROL_PAYLOADS), set(ROW_ACTION_PAYLOADS))

    def test_in_bounds_row_action_reaches_each_writer(self):
        """Prove that each deferred-action fixture reaches its database write."""
        for url_name, payload in ROW_ACTION_CONTROL_PAYLOADS.items():
            with self.subTest(url_name=url_name):
                source_id = payload.get("source_id", f"CONTROL-{url_name}")
                payload = self._prepare_row_action(
                    url_name,
                    payload,
                    source_id,
                    f"control-{url_name}",
                )
                before = _writer_database_state()
                response = self.client.post(
                    reverse(f"plugins:netbox_data_import:{url_name}"),
                    {"profile_id": self.profile.pk, **payload},
                )

                self.assertLess(response.status_code, 500)
                self.assertNotEqual(
                    _writer_database_state(),
                    before,
                    f"{url_name} did not reach a database write",
                )

    def test_an_overlength_row_action_leaves_the_database_unchanged(self):
        """Reject each deferred write without creating or changing an affected row."""
        for url_name, payloads in ROW_ACTION_PAYLOADS.items():
            for index, payload in enumerate(payloads):
                with self.subTest(url_name=url_name, payload=index):
                    source_id = payload.get("source_id", LONG)
                    payload = self._prepare_row_action(
                        url_name,
                        payload,
                        source_id,
                        f"overlength-{url_name}-{index}",
                    )
                    before = _writer_database_state()
                    response = self.client.post(
                        reverse(f"plugins:netbox_data_import:{url_name}"),
                        {"profile_id": self.profile.pk, **payload},
                    )

                    self.assertLess(
                        response.status_code,
                        500,
                        f"{url_name} payload {index} returned {response.status_code}",
                    )
                    self.assertEqual(
                        _writer_database_state(),
                        before,
                        f"{url_name} payload {index} changed a database row",
                    )
