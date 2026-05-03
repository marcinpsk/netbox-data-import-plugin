# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("dcim", "0188_racktype"),
        ("netbox_data_import", "0011_importprofile_capture_extra_data"),
    ]

    operations = [
        migrations.AlterField(
            model_name="classrolemapping",
            name="rack_type",
            field=models.ForeignKey(
                blank=True,
                help_text="Optional rack type assigned when creating racks",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to="dcim.racktype",
            ),
        ),
    ]
