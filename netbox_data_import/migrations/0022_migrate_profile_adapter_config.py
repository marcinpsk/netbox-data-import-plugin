from django.db import migrations

# The columns the flat-workbook adapter takes ownership of. Listing them here keeps the migration
# reproducible after the model drops them in 0023.
MOVED_COLUMNS = (
    "sheet_name",
    "source_id_column",
    "custom_field_name",
    "update_existing",
    "create_missing_device_types",
    "capture_extra_data",
    "primary_contact_lookup_field",
    "preview_view_mode",
)
FROZEN_REQUIRED_DEFAULTS = {
    "sheet_name": "Data",
    "primary_contact_lookup_field": "email",
    "preview_view_mode": "rows",
}


def move_columns_into_adapter_config(apps, schema_editor):
    """Stamp the flat_workbook adapter key and copy the device-format columns into adapter_config."""
    ImportProfile = apps.get_model("netbox_data_import", "ImportProfile")
    ContactRole = apps.get_model("tenancy", "ContactRole")
    role_names = dict(ContactRole.objects.values_list("pk", "name"))

    for profile in ImportProfile.objects.all().iterator():
        config = {column: getattr(profile, column) for column in MOVED_COLUMNS}
        config.update(
            {
                key: default if config[key] in ("", None) else config[key]
                for key, default in FROZEN_REQUIRED_DEFAULTS.items()
            }
        )
        # The role reference becomes a natural key so an exported profile stays portable.
        config["primary_contact_role"] = role_names.get(profile.primary_contact_role_id)
        profile.source_adapter = "flat_workbook"
        profile.adapter_config = config
        profile.save(update_fields=["source_adapter", "adapter_config"])


class Migration(migrations.Migration):
    dependencies = [
        ("tenancy", "0001_initial"),
        ("netbox_data_import", "0021_importprofile_adapter_config"),
    ]

    operations = [
        # No reverse callable: a renamed or deleted ContactRole cannot be resolved back to its id.
        migrations.RunPython(move_columns_into_adapter_config),
    ]
