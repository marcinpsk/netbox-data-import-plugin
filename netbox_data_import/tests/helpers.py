# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Shared test helpers for netbox_data_import tests."""

import os
from contextlib import contextmanager
from queue import Queue
from threading import Thread
from time import monotonic, sleep

from django.db import connections

FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "sample_cans.xlsx")


@contextmanager
def run_on_separate_connection(target):
    """Run *target* in a thread with a fresh database connection."""
    errors = Queue()

    def runner():
        connections["default"].close()
        try:
            target()
        except BaseException as exc:
            errors.put(exc)
        finally:
            connections["default"].close()

    thread = Thread(target=runner, daemon=True)
    thread.start()
    try:
        yield
    finally:
        thread.join(timeout=10)
        if thread.is_alive():
            raise AssertionError("The separate database connection did not finish.")
        if not errors.empty():
            raise errors.get()


def user_with_object_permission(username, grants):
    """Create a user holding one real ObjectPermission per model grant."""
    from django.contrib.auth import get_user_model
    from django.contrib.contenttypes.models import ContentType
    from users.models import ObjectPermission

    user = get_user_model().objects.create_user(username=username, password="testpass")
    for model, actions, constraints in grants:
        permission = ObjectPermission.objects.create(
            name=f"{username} {model.__name__} {'-'.join(actions)}",
            actions=list(actions),
            constraints=constraints,
        )
        permission.object_types.add(ContentType.objects.get_for_model(model))
        permission.users.add(user)
    return user


def make_dcim_objects(name_prefix=""):
    """Create and return (site, manufacturer, device_type, role) with the given prefix.

    Useful for test setUp methods that need basic DCIM infrastructure.
    All objects receive ``name_prefix`` prepended so tests that run in the same
    database transaction can use unique names.

    Example::

        site, mfg, dt, role = make_dcim_objects("Test")
    """
    from dcim.models import DeviceRole, DeviceType, Manufacturer, Site

    slug_prefix = name_prefix.lower()
    site = Site.objects.create(name=f"{name_prefix}Site", slug=f"{slug_prefix}site")
    manufacturer = Manufacturer.objects.create(name=f"{name_prefix}Mfg", slug=f"{slug_prefix}mfg")
    device_type = DeviceType.objects.create(
        manufacturer=manufacturer,
        model=f"{name_prefix}Model",
        slug=f"{slug_prefix}model",
        u_height=1,
    )
    role = DeviceRole.objects.create(name=f"{name_prefix}Role", slug=f"{slug_prefix}role")
    return site, manufacturer, device_type, role


def setup_preview_with_device_matches(client, profile):
    """Populate *client*'s session with import state and DeviceExistingMatch records.

    Runs a dry import against a freshly-created site, links the first two
    device result rows to two newly-created Device objects, and writes the
    resulting state into the test client's session.

    Returns ``(site, device1, device2, device_rows)`` so callers can make
    assertions against the created objects.
    """
    from dcim.models import Device

    from netbox_data_import.engine import parse_file, run_import
    from netbox_data_import.models import DeviceExistingMatch
    from netbox_data_import.views import _serialize_rows

    site, _manufacturer, device_type, role = make_dcim_objects("Match")

    device1 = Device.objects.create(name="device-a", site=site, device_type=device_type, role=role)
    device2 = Device.objects.create(name="device-b", site=site, device_type=device_type, role=role)

    with open(FIXTURE_PATH, "rb") as f:
        rows = parse_file(f, profile)
    result = run_import(rows, profile, {"site": site}, dry_run=True)

    device_rows = [r for r in result.rows if r.object_type == "device" and r.source_id]
    if len(device_rows) > 0:
        DeviceExistingMatch.objects.create(
            profile=profile,
            source_id=device_rows[0].source_id,
            source_asset_tag=device_rows[0].extra_data.get("asset_tag", "asset_a"),
            netbox_device_id=device1.id,
            device_name=device1.name,
        )
    if len(device_rows) > 1:
        DeviceExistingMatch.objects.create(
            profile=profile,
            source_id=device_rows[1].source_id,
            source_asset_tag=device_rows[1].extra_data.get("asset_tag", "asset_b"),
            netbox_device_id=device2.id,
            device_name=device2.name,
        )

    session = client.session
    session["import_result"] = result.to_session_dict()
    session["import_rows"] = _serialize_rows(rows)
    session["import_context"] = {
        "profile_id": profile.pk,
        "site_id": site.pk,
        "location_id": None,
        "tenant_id": None,
        "filename": "sample_cans.xlsx",
    }
    session.save()
    return site, device1, device2, device_rows


def set_import_source(device, profile, source_id="", extra_columns=None, unassigned_ips=None):
    """Store the import record the engine writes for one device."""
    from netbox_data_import.models import DeviceImportSource

    record, _ = DeviceImportSource.objects.update_or_create(
        device=device,
        defaults={
            "profile": profile,
            "source_id": source_id,
            "extra_columns": extra_columns or {},
            "unassigned_ips": unassigned_ips or {},
        },
    )
    return record


def user_with_object_permission(username, model, *, granted, password="testpass", actions=("view",)):
    """Create a user and, when *granted*, the ObjectPermission that opens *model*.

    NetBox runs only ObjectPermissionBackend, so a Django ``user_permissions`` row grants nothing.
    An ObjectPermission is how an operator actually issues the permission.
    """
    from core.models import ObjectType
    from django.contrib.auth import get_user_model
    from users.models import ObjectPermission

    user = get_user_model().objects.create_user(username=username, password=password)
    if granted:
        permission = ObjectPermission.objects.create(name=f"{username} {model.__name__}", actions=list(actions))
        permission.users.add(user)
        permission.object_types.add(ObjectType.objects.get_for_model(model))
    return user


def wait_until_a_lock_is_blocked(test, timeout=10):
    """Block until another backend is waiting for a lock this connection holds."""
    from django.db import connection

    # pg_stat_activity is cached for the whole transaction; pg_locks reads live lock-manager state.
    query = "SELECT count(*) FROM pg_locks WHERE NOT granted AND pg_backend_pid() = ANY(pg_blocking_pids(pid))"
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        with connection.cursor() as cursor:
            cursor.execute(query)
            if cursor.fetchone()[0]:
                return
        sleep(0.05)
    test.fail("No other backend started waiting for a lock this connection holds.")
