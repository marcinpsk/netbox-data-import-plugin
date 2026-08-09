from django.db import migrations
from django.db.models import Count


def remove_ambiguous_device_matches(apps, schema_editor):
    """Remove every binding when multiple sources claim one NetBox device."""
    device_match = apps.get_model("netbox_data_import", "DeviceExistingMatch")
    database = schema_editor.connection.alias
    duplicate_groups = (
        device_match.objects.using(database)
        .values("profile_id", "netbox_device_id")
        .annotate(match_count=Count("id"))
        .filter(match_count__gt=1)
    )
    for group in duplicate_groups.iterator():
        (
            device_match.objects.using(database)
            .filter(
                profile_id=group["profile_id"],
                netbox_device_id=group["netbox_device_id"],
            )
            .delete()
        )


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_data_import", "0014_alter_columnmapping_target_field_and_more"),
    ]

    operations = [
        migrations.RunPython(remove_ambiguous_device_matches, migrations.RunPython.noop),
    ]
