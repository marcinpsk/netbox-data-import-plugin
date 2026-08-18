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


def move_columns_into_adapter_config(apps, schema_editor):
    """Stamp the flat_workbook adapter key and copy the device-format columns into adapter_config."""
    ImportProfile = apps.get_model("netbox_data_import", "ImportProfile")
    ContactRole = apps.get_model("tenancy", "ContactRole")
    role_names = dict(ContactRole.objects.values_list("pk", "name"))

    for profile in ImportProfile.objects.all().iterator():
        config = {column: getattr(profile, column) for column in MOVED_COLUMNS}
        # The role reference becomes a natural key so an exported profile stays portable.
        config["primary_contact_role"] = role_names.get(profile.primary_contact_role_id)
        profile.source_adapter = "flat_workbook"
        profile.adapter_config = config
        profile.save(update_fields=["source_adapter", "adapter_config"])


def restore_columns_from_adapter_config(apps, schema_editor):
    """Copy the adapter configuration back into the columns 0023 restores on a rollback."""
    ImportProfile = apps.get_model("netbox_data_import", "ImportProfile")
    ContactRole = apps.get_model("tenancy", "ContactRole")
    role_ids = dict(ContactRole.objects.values_list("name", "pk"))

    for profile in ImportProfile.objects.all().iterator():
        config = profile.adapter_config or {}
        for column in MOVED_COLUMNS:
            if column in config:
                setattr(profile, column, config[column])
        profile.primary_contact_role_id = role_ids.get(config.get("primary_contact_role"))
        profile.save(update_fields=[*MOVED_COLUMNS, "primary_contact_role"])


class Migration(migrations.Migration):
    dependencies = [
        ("tenancy", "0001_initial"),
        ("netbox_data_import", "0021_importprofile_adapter_config_and_more"),
    ]

    operations = [
        migrations.RunPython(move_columns_into_adapter_config, restore_columns_from_adapter_config),
    ]
