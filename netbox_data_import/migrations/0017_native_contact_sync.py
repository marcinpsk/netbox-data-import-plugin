import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_data_import", "0016_deviceexistingmatch_ndi_devicematch_profile_device"),
        ("tenancy", "0020_remove_contactgroupmembership"),
    ]

    operations = [
        migrations.AddField(
            model_name="importprofile",
            name="primary_contact_lookup_field",
            field=models.CharField(default="email", max_length=10),
        ),
        migrations.AddField(
            model_name="importprofile",
            name="primary_contact_role",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="tenancy.contactrole",
            ),
        ),
    ]
