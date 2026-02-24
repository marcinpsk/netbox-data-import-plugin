# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("extras", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="ImportProfile",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("created", models.DateTimeField(auto_now_add=True, null=True)),
                ("last_updated", models.DateTimeField(auto_now=True, null=True)),
                ("custom_field_data", models.JSONField(blank=True, default=dict, encoder=None)),
                ("name", models.CharField(max_length=100, unique=True)),
                ("description", models.TextField(blank=True)),
                ("sheet_name", models.CharField(default="Data", max_length=100)),
                ("source_id_column", models.CharField(blank=True, max_length=100)),
                ("custom_field_name", models.CharField(blank=True, max_length=100)),
                ("update_existing", models.BooleanField(default=True)),
                ("create_missing_device_types", models.BooleanField(default=True)),
                (
                    "tags",
                    models.ManyToManyField(
                        blank=True,
                        related_name="+",
                        to="extras.tag",
                    ),
                ),
            ],
            options={
                "verbose_name": "Import Profile",
                "verbose_name_plural": "Import Profiles",
                "ordering": ["name"],
            },
        ),
        migrations.CreateModel(
            name="ColumnMapping",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                (
                    "profile",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="column_mappings",
                        to="netbox_data_import.importprofile",
                    ),
                ),
                ("source_column", models.CharField(max_length=200)),
                (
                    "target_field",
                    models.CharField(
                        max_length=50,
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
                    ),
                ),
            ],
            options={
                "verbose_name": "Column Mapping",
                "verbose_name_plural": "Column Mappings",
                "ordering": ["profile", "target_field"],
            },
        ),
        migrations.AddConstraint(
            model_name="columnmapping",
            constraint=models.UniqueConstraint(
                fields=("profile", "target_field"),
                name="unique_column_mapping_per_profile",
            ),
        ),
        migrations.CreateModel(
            name="ClassRoleMapping",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                (
                    "profile",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="class_role_mappings",
                        to="netbox_data_import.importprofile",
                    ),
                ),
                ("source_class", models.CharField(max_length=200)),
                ("creates_rack", models.BooleanField(default=False)),
                ("role_slug", models.CharField(blank=True, max_length=100)),
            ],
            options={
                "verbose_name": "Class → Role Mapping",
                "verbose_name_plural": "Class → Role Mappings",
                "ordering": ["profile", "source_class"],
            },
        ),
        migrations.AddConstraint(
            model_name="classrolemapping",
            constraint=models.UniqueConstraint(
                fields=("profile", "source_class"),
                name="unique_class_role_mapping_per_profile",
            ),
        ),
        migrations.CreateModel(
            name="DeviceTypeMapping",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                (
                    "profile",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="device_type_mappings",
                        to="netbox_data_import.importprofile",
                    ),
                ),
                ("source_make", models.CharField(max_length=200)),
                ("source_model", models.CharField(max_length=200)),
                ("netbox_manufacturer_slug", models.CharField(max_length=100)),
                ("netbox_device_type_slug", models.CharField(max_length=100)),
            ],
            options={
                "verbose_name": "Device Type Mapping",
                "verbose_name_plural": "Device Type Mappings",
                "ordering": ["profile", "source_make", "source_model"],
            },
        ),
        migrations.AddConstraint(
            model_name="devicetypemapping",
            constraint=models.UniqueConstraint(
                fields=("profile", "source_make", "source_model"),
                name="unique_device_type_mapping_per_profile",
            ),
        ),
    ]
