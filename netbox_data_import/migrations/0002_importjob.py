# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_data_import", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="ImportJob",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("input_filename", models.CharField(blank=True, max_length=255)),
                ("dry_run", models.BooleanField(default=False)),
                ("site_name", models.CharField(blank=True, max_length=100)),
                ("result_counts", models.JSONField(default=dict)),
                ("result_rows", models.JSONField(default=list)),
                (
                    "profile",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="import_jobs",
                        to="netbox_data_import.importprofile",
                    ),
                ),
            ],
            options={
                "verbose_name": "Import Job",
                "verbose_name_plural": "Import Jobs",
                "ordering": ["-created"],
            },
        ),
    ]
