from django.db import migrations

ADAPTER_KEY = "flat_workbook"
SETTING_KEY = "primary_contact_lookup_field"
FROZEN_DEFAULT = "email"


def repair_empty_primary_contact_lookup_field(apps, schema_editor):
    """Replace the two invalid stored lookup values with the default in effect at this release."""
    ImportProfile = apps.get_model("netbox_data_import", "ImportProfile")
    profiles = ImportProfile.objects.filter(source_adapter=ADAPTER_KEY).only("pk", "adapter_config")
    for profile in profiles.iterator():
        config = profile.adapter_config
        if not isinstance(config, dict) or SETTING_KEY not in config:
            continue
        if config[SETTING_KEY] not in ("", None):
            continue
        ImportProfile.objects.filter(pk=profile.pk).update(adapter_config={**config, SETTING_KEY: FROZEN_DEFAULT})


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_data_import", "0023_drop_moved_profile_columns"),
    ]

    operations = [
        migrations.RunPython(repair_empty_primary_contact_lookup_field),
    ]
