from django.db import migrations

ADAPTER_KEY = "flat_workbook"
FROZEN_REQUIRED_DEFAULTS = {
    "sheet_name": "Data",
    "primary_contact_lookup_field": "email",
    "preview_view_mode": "rows",
}


def repair_empty_required_settings(apps, schema_editor):
    """Replace empty required settings with the defaults in effect at this release."""
    ImportProfile = apps.get_model("netbox_data_import", "ImportProfile")
    profiles = ImportProfile.objects.filter(source_adapter=ADAPTER_KEY).only("pk", "adapter_config")
    for profile in profiles.iterator():
        config = profile.adapter_config
        if not isinstance(config, dict):
            continue
        repaired = {
            key: default if config.get(key) in ("", None) else config[key]
            for key, default in FROZEN_REQUIRED_DEFAULTS.items()
        }
        if all(config.get(key) == value for key, value in repaired.items()):
            continue
        ImportProfile.objects.filter(pk=profile.pk).update(adapter_config={**config, **repaired})


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_data_import", "0023_drop_moved_profile_columns"),
    ]

    operations = [
        migrations.RunPython(repair_empty_required_settings),
    ]
