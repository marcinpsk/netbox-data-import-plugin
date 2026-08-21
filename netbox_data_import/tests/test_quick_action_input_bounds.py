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

from netbox_data_import.models import ImportProfile

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
        {"source_id": LONG, "row_number": "1", "device_id": "1"},
    ],
    "sync_single_row": [
        {"row_number": "1", "source_id": LONG},
    ],
    # This one reads its source IDs from the stored preview rows, not from the request.
    "auto_match_devices": [
        {},
    ],
}


def _permission_scoped_writer_url_names():
    """Return the URL name of every view that writes through the permission-scoped saver."""
    import ast

    from netbox_data_import import urls

    source = pathlib.Path(urls.__file__).with_name("views.py").read_text()
    writers = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.ClassDef):
            continue
        for sub in ast.walk(node):
            if (
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Name)
                and sub.func.id == "save_permission_scoped_object"
            ):
                writers.add(node.name)
                break
    names = set()
    for pattern in urls.urlpatterns:
        view_class = getattr(pattern.callback, "view_class", None)
        if view_class is not None and view_class.__name__ in writers:
            names.add(pattern.name)
    return names


def _quick_action_url_names():
    """Return the URL name of every quick action the preview page can post."""
    from netbox_data_import import urls

    names = set()
    for pattern in urls.urlpatterns:
        view_class = getattr(pattern.callback, "view_class", None)
        if view_class is None:
            continue
        if view_class.__name__.startswith("Quick") or view_class.__name__ == "IgnoreDeviceView":
            names.add(pattern.name)
    return names


class QuickActionInputBoundsTest(TestCase):
    """Each quick action writes request values straight to a column with a fixed width."""

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_superuser(
            username="quick-bounds-user", email="bounds@example.invalid", password="testpass"
        )
        cls.profile = ImportProfile.objects.create(
            name="Quick Bounds Profile", adapter_config={"sheet_name": "Data", "source_id_column": "Id"}
        )

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)

    def test_every_quick_action_is_covered(self):
        """A new quick action has to arrive with its own boundary payload."""
        self.assertEqual(_quick_action_url_names() - set(OVERLENGTH_PAYLOADS), set())

    def test_no_quick_action_writes_a_value_its_column_cannot_hold(self):
        """The database raises DataError and returns HTTP 500 when the view does not check first."""
        for url_name, payloads in OVERLENGTH_PAYLOADS.items():
            for index, payload in enumerate(payloads):
                with self.subTest(url_name=url_name, payload=index):
                    response = self.client.post(
                        reverse(f"plugins:netbox_data_import:{url_name}"),
                        {"profile_id": self.profile.pk, **payload},
                    )

                    self.assertLess(
                        response.status_code,
                        500,
                        f"{url_name} payload {index} returned {response.status_code}",
                    )

    def test_every_permission_scoped_writer_is_covered(self):
        """A new view that writes through the shared saver has to arrive with its own payload."""
        self.assertEqual(_permission_scoped_writer_url_names() - set(ROW_ACTION_PAYLOADS), set())

    def test_no_row_action_writes_a_value_its_column_cannot_hold(self):
        """These views share one saver, so an unbounded value reaches the database through it."""
        for url_name, payloads in ROW_ACTION_PAYLOADS.items():
            for index, payload in enumerate(payloads):
                with self.subTest(url_name=url_name, payload=index):
                    response = self.client.post(
                        reverse(f"plugins:netbox_data_import:{url_name}"),
                        {"profile_id": self.profile.pk, **payload},
                    )

                    self.assertLess(
                        response.status_code,
                        500,
                        f"{url_name} payload {index} returned {response.status_code}",
                    )
