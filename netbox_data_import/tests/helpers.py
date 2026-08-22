# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Shared test helpers for netbox_data_import tests."""

import os
from contextlib import contextmanager
from queue import Queue
from threading import Thread

from django.db import connections

FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "sample_cans.xlsx")


@contextmanager
def run_on_separate_connection(target):
    """Run a callback in a daemon thread using a separate database connection.
    
    Parameters:
        target (callable): Callback to execute in the separate thread.
    
    Raises:
        AssertionError: If the callback does not finish within 10 seconds.
        BaseException: Any exception raised by the callback.
    """
    errors = Queue()

    def runner():
        """
        Run the target callback using a freshly opened default database connection.
        
        Exceptions raised by the callback are captured for handling by the caller.
        """
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
    """
    Create a user with object permissions for the specified model grants.
    
    Parameters:
        username (str): Username for the created user.
        grants (iterable): Permission grants as `(model, actions, constraints)` tuples.
    
    Returns:
        User: The created user.
    """
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
