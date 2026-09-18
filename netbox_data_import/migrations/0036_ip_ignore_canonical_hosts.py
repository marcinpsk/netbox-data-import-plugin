# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>

import ipaddress

from django.db import migrations

#: The Device fields the preview compares as addresses. Frozen here: a migration is a point in time.
IP_TARGET_FIELDS = ("primary_ip4", "primary_ip6", "oob_ip")


def _host(value):
    """Return the host part of a stored canonical, or the value when it is not an address."""
    try:
        return str(ipaddress.ip_interface(str(value)).ip)
    except ValueError:
        return value


def _rewrite(apps, convert):
    """Apply *convert* to the canonical of both snapshots on every ignored IP difference."""
    IgnoredFieldDifference = apps.get_model("netbox_data_import", "IgnoredFieldDifference")
    updated = []
    for record in IgnoredFieldDifference.objects.filter(target_field__in=IP_TARGET_FIELDS):
        for snapshot in (record.file_snapshot, record.netbox_snapshot):
            if isinstance(snapshot, dict) and snapshot.get("canonical"):
                snapshot["canonical"] = convert(snapshot["canonical"], snapshot.get("display", ""))
        updated.append(record)
    if updated:
        IgnoredFieldDifference.objects.bulk_update(updated, ["file_snapshot", "netbox_snapshot"], batch_size=500)


def to_host(apps, schema_editor):
    """The preview now compares an address on its host, the way the writer matches one."""
    _rewrite(apps, lambda canonical, display: _host(canonical))


def to_interface(apps, schema_editor):
    """Restore the host/prefix spelling from the display value the row kept alongside it."""

    def convert(canonical, display):
        try:
            return str(ipaddress.ip_interface(str(display or canonical)))
        except ValueError:
            return canonical

    _rewrite(apps, convert)


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_data_import", "0035_retire_superseded_proposals"),
    ]

    operations = [
        migrations.RunPython(to_host, to_interface),
    ]
