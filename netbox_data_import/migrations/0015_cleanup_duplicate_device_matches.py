# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
import logging

from django.db import migrations
from django.db.models import Count

logger = logging.getLogger(__name__)


def remove_ambiguous_device_matches(apps, schema_editor):
    """Remove every binding when multiple sources claim one NetBox device."""
    device_match = apps.get_model("netbox_data_import", "DeviceExistingMatch")
    database = schema_editor.connection.alias
    duplicate_groups = list(
        device_match.objects.using(database)
        .values("profile_id", "netbox_device_id")
        .annotate(match_count=Count("id"))
        .filter(match_count__gt=1)
    )
    for group in duplicate_groups:
        matches = device_match.objects.using(database).filter(
            profile_id=group["profile_id"],
            netbox_device_id=group["netbox_device_id"],
        )
        source_ids = list(matches.order_by("source_id").values_list("source_id", flat=True))
        logger.warning(
            "Removing ambiguous device bindings for profile %s and NetBox device %s: source IDs %s",
            group["profile_id"],
            group["netbox_device_id"],
            ", ".join(source_ids),
        )
        matches.delete()


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_data_import", "0014_alter_columnmapping_target_field_and_more"),
    ]

    operations = [
        migrations.RunPython(remove_ambiguous_device_matches, migrations.RunPython.noop),
    ]
