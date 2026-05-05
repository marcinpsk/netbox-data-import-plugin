# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Shared test helpers for netbox_data_import tests."""

import os

FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "sample_cans.xlsx")


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
    from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site

    from netbox_data_import.engine import parse_file, run_import
    from netbox_data_import.models import DeviceExistingMatch
    from netbox_data_import.views import _serialize_rows

    site = Site.objects.create(name="MatchSite", slug="match-site")
    role = DeviceRole.objects.create(name="TestRole", slug="test-role")
    manufacturer = Manufacturer.objects.create(name="TestMfg", slug="test-mfg")
    device_type = DeviceType.objects.create(manufacturer=manufacturer, model="TestModel", slug="test-model")

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
