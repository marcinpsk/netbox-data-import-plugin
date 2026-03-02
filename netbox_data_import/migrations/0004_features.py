# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_data_import", "0003_remove_classrolemapping_unique_class_role_mapping_per_profile_and_more"),
    ]

    operations = [
        # Add ignore field to ClassRoleMapping
        migrations.AddField(
            model_name="classrolemapping",
            name="ignore",
            field=models.BooleanField(
                default=False,
                help_text="If checked, rows with this class are silently skipped (not shown as errors)",
            ),
        ),
        # Add preview_view_mode field to ImportProfile
        migrations.AddField(
            model_name="importprofile",
            name="preview_view_mode",
            field=models.CharField(
                choices=[("rows", "Row view"), ("racks", "Rack view")],
                default="rows",
                help_text="How to display the import preview (row table or rack diagrams)",
                max_length=10,
            ),
        ),
        # Create IgnoredDevice model
        migrations.CreateModel(
            name="IgnoredDevice",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                (
                    "profile",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="ignored_devices",
                        to="netbox_data_import.importprofile",
                    ),
                ),
                (
                    "source_id",
                    models.CharField(help_text="Source ID value that identifies this device", max_length=200),
                ),
                (
                    "device_name",
                    models.CharField(blank=True, help_text="Original device name (for display only)", max_length=200),
                ),
            ],
            options={
                "verbose_name": "Ignored Device",
                "verbose_name_plural": "Ignored Devices",
                "ordering": ["profile", "source_id"],
            },
        ),
        migrations.AlterUniqueTogether(
            name="ignoreddevice",
            unique_together={("profile", "source_id")},
        ),
        # Create ColumnTransformRule model
        migrations.CreateModel(
            name="ColumnTransformRule",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                (
                    "profile",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="column_transform_rules",
                        to="netbox_data_import.importprofile",
                    ),
                ),
                (
                    "source_column",
                    models.CharField(help_text="Source Excel column to transform (exact header name)", max_length=200),
                ),
                (
                    "pattern",
                    models.CharField(
                        help_text=r"Python regex with capture groups (re.fullmatch). E.g. ^(\w+) - (.+)$",
                        max_length=500,
                    ),
                ),
                (
                    "group_1_target",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("rack_name", "Rack name"),
                            ("device_name", "Device name"),
                            ("device_class", "Device class (maps to role/rack)"),
                            ("face", "Face (Front/Back)"),
                            ("airflow", "Airflow"),
                            ("u_position", "U position"),
                            ("status", "Status"),
                            ("make", "Make (manufacturer)"),
                            ("model", "Model (device type)"),
                            ("u_height", "U height"),
                            ("serial", "Serial number"),
                            ("asset_tag", "Asset tag"),
                            ("source_id", "Source ID (stored in custom field)"),
                        ],
                        help_text="Target field for capture group 1 (leave blank to ignore)",
                        max_length=50,
                    ),
                ),
                (
                    "group_2_target",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("rack_name", "Rack name"),
                            ("device_name", "Device name"),
                            ("device_class", "Device class (maps to role/rack)"),
                            ("face", "Face (Front/Back)"),
                            ("airflow", "Airflow"),
                            ("u_position", "U position"),
                            ("status", "Status"),
                            ("make", "Make (manufacturer)"),
                            ("model", "Model (device type)"),
                            ("u_height", "U height"),
                            ("serial", "Serial number"),
                            ("asset_tag", "Asset tag"),
                            ("source_id", "Source ID (stored in custom field)"),
                        ],
                        help_text="Target field for capture group 2 (leave blank to ignore)",
                        max_length=50,
                    ),
                ),
            ],
            options={
                "verbose_name": "Column Transform Rule",
                "verbose_name_plural": "Column Transform Rules",
                "ordering": ["profile", "source_column"],
            },
        ),
        migrations.AlterUniqueTogether(
            name="columntransformrule",
            unique_together={("profile", "source_column")},
        ),
        # Create SourceResolution model
        migrations.CreateModel(
            name="SourceResolution",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                (
                    "profile",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="source_resolutions",
                        to="netbox_data_import.importprofile",
                    ),
                ),
                (
                    "source_id",
                    models.CharField(help_text="Source ID of the row this resolution applies to", max_length=200),
                ),
                ("source_column", models.CharField(help_text="Column name this resolution applies to", max_length=200)),
                ("original_value", models.TextField(help_text="Original cell value before resolution")),
                (
                    "resolved_fields",
                    models.JSONField(
                        default=dict,
                        help_text="Dict of target_field -> resolved_value (e.g. {'device_name': 'SW1', 'asset_tag': '59AH76'})",
                    ),
                ),
            ],
            options={
                "verbose_name": "Source Resolution",
                "verbose_name_plural": "Source Resolutions",
                "ordering": ["profile", "source_id"],
            },
        ),
        migrations.AlterUniqueTogether(
            name="sourceresolution",
            unique_together={("profile", "source_id", "source_column")},
        ),
    ]
