# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_data_import", "0006_deviceexistingmatch_asset_tag"),
    ]

    operations = [
        migrations.CreateModel(
            name="ManufacturerMapping",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "source_make",
                    models.CharField(help_text="Exact source make value (e.g. 'Dell EMC')", max_length=200),
                ),
                (
                    "netbox_manufacturer_slug",
                    models.CharField(
                        help_text="NetBox manufacturer slug to map this make to (e.g. 'dell')", max_length=100
                    ),
                ),
                (
                    "profile",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="manufacturer_mappings",
                        to="netbox_data_import.importprofile",
                    ),
                ),
            ],
            options={
                "verbose_name": "Manufacturer Mapping",
                "verbose_name_plural": "Manufacturer Mappings",
                "ordering": ["profile", "source_make"],
                "unique_together": {("profile", "source_make")},
            },
        ),
    ]
