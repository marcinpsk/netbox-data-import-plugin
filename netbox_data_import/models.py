# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
from django.db import models
from django.urls import reverse
from netbox.models import NetBoxModel


TARGET_FIELD_CHOICES = [
    ("rack_name",    "Rack name"),
    ("device_name",  "Device name"),
    ("device_class", "Device class (maps to role/rack)"),
    ("face",         "Face (Front/Back)"),
    ("airflow",      "Airflow"),
    ("u_position",   "U position"),
    ("status",       "Status"),
    ("make",         "Make (manufacturer)"),
    ("model",        "Model (device type)"),
    ("u_height",     "U height"),
    ("serial",       "Serial number"),
    ("asset_tag",    "Asset tag"),
    ("source_id",    "Source ID (stored in custom field)"),
]


class ImportProfile(NetBoxModel):
    """Named configuration for one source file format."""

    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)
    sheet_name = models.CharField(
        max_length=100,
        default="Data",
        help_text="Name of the Excel worksheet to read",
    )
    source_id_column = models.CharField(
        max_length=100,
        blank=True,
        help_text="Column whose value is stored in a NetBox custom field (e.g. 'Id')",
    )
    custom_field_name = models.CharField(
        max_length=100,
        blank=True,
        help_text="NetBox custom field name to store the source ID in (e.g. 'cans_id')",
    )
    update_existing = models.BooleanField(
        default=True,
        help_text="Update existing NetBox objects when a match is found",
    )
    create_missing_device_types = models.BooleanField(
        default=True,
        help_text="Auto-create manufacturers and device types that don't exist in NetBox",
    )

    # Override tags reverse accessor to avoid clashes with other plugins
    tags = models.ManyToManyField(
        to="extras.Tag",
        related_name="+",
        blank=True,
    )

    class Meta:
        ordering = ["name"]
        verbose_name = "Import Profile"
        verbose_name_plural = "Import Profiles"

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("plugins:netbox_data_import:importprofile", args=[self.pk])


class ColumnMapping(models.Model):
    """Maps one source column header to one semantic NetBox field."""

    profile = models.ForeignKey(
        ImportProfile,
        on_delete=models.CASCADE,
        related_name="column_mappings",
    )
    source_column = models.CharField(
        max_length=200,
        help_text="Exact column header in the source file (case-sensitive)",
    )
    target_field = models.CharField(max_length=50, choices=TARGET_FIELD_CHOICES)

    class Meta:
        ordering = ["profile", "target_field"]
        unique_together = [("profile", "target_field")]
        verbose_name = "Column Mapping"
        verbose_name_plural = "Column Mappings"

    def __str__(self):
        return f"{self.source_column} → {self.get_target_field_display()}"

    def get_absolute_url(self):
        return reverse("plugins:netbox_data_import:columnmapping_edit", args=[self.pk])


class ClassRoleMapping(models.Model):
    """Maps a source 'class' value to a NetBox outcome (rack or device role)."""

    profile = models.ForeignKey(
        ImportProfile,
        on_delete=models.CASCADE,
        related_name="class_role_mappings",
    )
    source_class = models.CharField(
        max_length=200,
        help_text="Value from the class column (e.g. 'Server', 'Cabinet')",
    )
    creates_rack = models.BooleanField(
        default=False,
        help_text="If checked, rows with this class create a Rack instead of a Device",
    )
    role_slug = models.CharField(
        max_length=100,
        blank=True,
        help_text="NetBox device role slug (ignored when 'creates rack' is checked)",
    )

    class Meta:
        ordering = ["profile", "source_class"]
        unique_together = [("profile", "source_class")]
        verbose_name = "Class → Role Mapping"
        verbose_name_plural = "Class → Role Mappings"

    def __str__(self):
        if self.creates_rack:
            return f"{self.source_class} → Rack"
        return f"{self.source_class} → {self.role_slug}"

    def get_absolute_url(self):
        return reverse("plugins:netbox_data_import:classrolemapping_edit", args=[self.pk])


class DeviceTypeMapping(models.Model):
    """Explicit (make, model) override when source naming doesn't slugify cleanly."""

    profile = models.ForeignKey(
        ImportProfile,
        on_delete=models.CASCADE,
        related_name="device_type_mappings",
    )
    source_make = models.CharField(max_length=200)
    source_model = models.CharField(max_length=200)
    netbox_manufacturer_slug = models.CharField(max_length=100)
    netbox_device_type_slug = models.CharField(max_length=100)

    class Meta:
        ordering = ["profile", "source_make", "source_model"]
        unique_together = [("profile", "source_make", "source_model")]
        verbose_name = "Device Type Mapping"
        verbose_name_plural = "Device Type Mappings"

    def __str__(self):
        return f"{self.source_make} / {self.source_model} → {self.netbox_manufacturer_slug}/{self.netbox_device_type_slug}"

    def get_absolute_url(self):
        return reverse("plugins:netbox_data_import:devicetypemapping_edit", args=[self.pk])
