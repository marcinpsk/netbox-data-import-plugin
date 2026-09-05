# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>

from django.db import migrations


def remove_device_type_creation_config(apps, schema_editor):
    """Remove the retired Device Type creation setting from every Import Profile."""
    ImportProfile = apps.get_model("netbox_data_import", "ImportProfile")

    for profile in ImportProfile.objects.all().iterator():
        if "create_missing_device_types" not in profile.adapter_config:
            continue
        profile.adapter_config = {
            key: value for key, value in profile.adapter_config.items() if key != "create_missing_device_types"
        }
        profile.save(update_fields=["adapter_config"])


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_data_import", "0029_alter_cableimportsource_from_text_and_more"),
    ]

    operations = [
        # Reversing restores nothing: the old field was optional, so its absence read as False.
        migrations.RunPython(remove_device_type_creation_config, migrations.RunPython.noop),
    ]
