# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
from django.db import models
from django.urls import reverse
from netbox.models import NetBoxModel

PREVIEW_VIEW_CHOICES = [
    ("rows", "Row view"),
    ("racks", "Rack view"),
]


TARGET_FIELD_CHOICES = [
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
    preview_view_mode = models.CharField(
        max_length=10,
        choices=PREVIEW_VIEW_CHOICES,
        default="rows",
        help_text="How to display the import preview (row table or rack diagrams)",
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
        """Return the detail URL for this import profile."""
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
        constraints = [
            models.UniqueConstraint(fields=["profile", "target_field"], name="ndi_columnmapping_profile_target"),
        ]
        verbose_name = "Column Mapping"
        verbose_name_plural = "Column Mappings"

    def __str__(self):
        return f"{self.source_column} → {self.get_target_field_display()}"

    def get_absolute_url(self):
        """Return the edit URL for this column mapping."""
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
    ignore = models.BooleanField(
        default=False,
        help_text="If checked, rows with this class are silently skipped (not shown as errors)",
    )

    class Meta:
        ordering = ["profile", "source_class"]
        constraints = [
            models.UniqueConstraint(fields=["profile", "source_class"], name="ndi_classrolemapping_profile_class"),
        ]
        verbose_name = "Class → Role Mapping"
        verbose_name_plural = "Class → Role Mappings"

    def __str__(self):
        if self.creates_rack:
            return f"{self.source_class} → Rack"
        return f"{self.source_class} → {self.role_slug}"

    def get_absolute_url(self):
        """Return the edit URL for this class→role mapping."""
        return reverse("plugins:netbox_data_import:classrolemapping_edit", args=[self.pk])


class ImportJob(models.Model):
    """Records a completed import run with its results."""

    profile = models.ForeignKey(
        ImportProfile,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="import_jobs",
    )
    created = models.DateTimeField(auto_now_add=True)
    input_filename = models.CharField(max_length=255, blank=True)
    dry_run = models.BooleanField(default=False)
    site_name = models.CharField(max_length=100, blank=True)
    result_counts = models.JSONField(default=dict)
    result_rows = models.JSONField(default=list)

    class Meta:
        ordering = ["-created"]
        verbose_name = "Import Job"
        verbose_name_plural = "Import Jobs"

    def __str__(self):
        return f"Import {self.pk} — {self.created:%Y-%m-%d %H:%M} ({self.input_filename})"

    def get_absolute_url(self):
        """Return the associated profile's URL (no per-job detail view exists)."""
        if not self.profile_id:
            return reverse("plugins:netbox_data_import:importprofile_list")
        return reverse("plugins:netbox_data_import:importprofile", args=[self.profile_id])


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
        constraints = [
            models.UniqueConstraint(
                fields=["profile", "source_make", "source_model"], name="ndi_dtm_profile_make_model"
            ),
        ]
        verbose_name = "Device Type Mapping"
        verbose_name_plural = "Device Type Mappings"

    def __str__(self):
        return (
            f"{self.source_make} / {self.source_model} → {self.netbox_manufacturer_slug}/{self.netbox_device_type_slug}"
        )

    def get_absolute_url(self):
        """Return the edit URL for this device type mapping."""
        return reverse("plugins:netbox_data_import:devicetypemapping_edit", args=[self.pk])


class ManufacturerMapping(models.Model):
    """Maps a source 'make' value to an existing NetBox manufacturer slug."""

    profile = models.ForeignKey(
        ImportProfile,
        on_delete=models.CASCADE,
        related_name="manufacturer_mappings",
    )
    source_make = models.CharField(
        max_length=200,
        help_text="Exact source make value (e.g. 'Dell EMC')",
    )
    netbox_manufacturer_slug = models.CharField(
        max_length=100,
        help_text="NetBox manufacturer slug to map this make to (e.g. 'dell')",
    )

    class Meta:
        ordering = ["profile", "source_make"]
        constraints = [
            models.UniqueConstraint(fields=["profile", "source_make"], name="ndi_mfgmapping_profile_make"),
        ]
        verbose_name = "Manufacturer Mapping"
        verbose_name_plural = "Manufacturer Mappings"

    def __str__(self):
        return f"{self.source_make} → {self.netbox_manufacturer_slug}"


class IgnoredDevice(models.Model):
    """Per-device ignore record — prevents a specific source device from being imported."""

    profile = models.ForeignKey(
        ImportProfile,
        on_delete=models.CASCADE,
        related_name="ignored_devices",
    )
    source_id = models.CharField(
        max_length=200,
        help_text="Source ID value that identifies this device",
    )
    device_name = models.CharField(
        max_length=200,
        blank=True,
        help_text="Original device name (for display only)",
    )

    class Meta:
        ordering = ["profile", "source_id"]
        constraints = [
            models.UniqueConstraint(fields=["profile", "source_id"], name="ndi_ignoreddevice_profile_srcid"),
        ]
        verbose_name = "Ignored Device"
        verbose_name_plural = "Ignored Devices"

    def __str__(self):
        return f"{self.device_name or self.source_id} (ignored)"


class ColumnTransformRule(models.Model):
    r"""Regex-based transform applied to a source column during parse.

    Example: source_column='Name', pattern='^(\w{4,8}) - (.+)$',
    group_1_target='asset_tag', group_2_target='device_name'
    transforms "59AH76 - PROD-LAB03-SW1" into asset_tag="59AH76", device_name="PROD-LAB03-SW1".
    """

    profile = models.ForeignKey(
        ImportProfile,
        on_delete=models.CASCADE,
        related_name="column_transform_rules",
    )
    source_column = models.CharField(
        max_length=200,
        help_text="Source Excel column to transform (exact header name)",
    )
    pattern = models.CharField(
        max_length=500,
        help_text=r"Python regex with capture groups (re.fullmatch). E.g. ^(\w+) - (.+)$",
    )
    group_1_target = models.CharField(
        max_length=50,
        blank=True,
        choices=TARGET_FIELD_CHOICES,
        help_text="Target field for capture group 1 (leave blank to ignore)",
    )
    group_2_target = models.CharField(
        max_length=50,
        blank=True,
        choices=TARGET_FIELD_CHOICES,
        help_text="Target field for capture group 2 (leave blank to ignore)",
    )

    class Meta:
        ordering = ["profile", "source_column"]
        constraints = [
            models.UniqueConstraint(fields=["profile", "source_column"], name="ndi_ctr_profile_column"),
        ]
        verbose_name = "Column Transform Rule"
        verbose_name_plural = "Column Transform Rules"

    def clean(self):
        """Validate the regex pattern and that it has enough capture groups."""
        import re

        from django.core.exceptions import ValidationError

        try:
            compiled = re.compile(self.pattern)
        except re.error as exc:
            raise ValidationError({"pattern": f"Invalid regex pattern: {exc}"})

        required_groups = 0
        if self.group_1_target:
            required_groups = 1
        if self.group_2_target:
            required_groups = 2
        if compiled.groups < required_groups:
            raise ValidationError(
                {
                    "pattern": (
                        f"Regex must contain at least {required_groups} capture group(s) "
                        f"for the configured group target(s), but found {compiled.groups}."
                    )
                }
            )

    def __str__(self):
        return f"{self.source_column}: {self.pattern}"

    def get_absolute_url(self):
        """Return the edit URL for this column transform rule."""
        return reverse("plugins:netbox_data_import:columntransformrule_edit", args=[self.pk])


class SourceResolution(models.Model):
    """Saved manual resolution for a specific source cell value.

    When a user manually splits "59AH76 - PROD-LAB03-SW1" into asset_tag=59AH76
    and device_name=PROD-LAB03-SW1, that resolution is saved here. On re-import,
    parse_file applies it automatically (like git rerere).
    """

    profile = models.ForeignKey(
        ImportProfile,
        on_delete=models.CASCADE,
        related_name="source_resolutions",
    )
    source_id = models.CharField(
        max_length=200,
        help_text="Source ID of the row this resolution applies to",
    )
    source_column = models.CharField(
        max_length=200,
        help_text="Column name this resolution applies to",
    )
    original_value = models.TextField(
        help_text="Original cell value before resolution",
    )
    resolved_fields = models.JSONField(
        default=dict,
        help_text="Dict of target_field -> resolved_value (e.g. {'device_name': 'SW1', 'asset_tag': '59AH76'})",
    )

    class Meta:
        ordering = ["profile", "source_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["profile", "source_id", "source_column"], name="ndi_srcresolution_profile_id_col"
            ),
        ]
        verbose_name = "Source Resolution"
        verbose_name_plural = "Source Resolutions"

    def __str__(self):
        return f"{self.source_id}/{self.source_column}: {self.original_value!r}"


class DeviceExistingMatch(models.Model):
    """Explicit match between a source row and an existing NetBox device.

    When a user clicks "Link existing" on a device preview row, this record is saved.
    On re-import, the engine uses this to emit action='update' against the matched device
    instead of action='create', even if the device has no source-ID custom field yet.
    """

    profile = models.ForeignKey(
        ImportProfile,
        on_delete=models.CASCADE,
        related_name="device_matches",
    )
    source_id = models.CharField(
        max_length=200,
        help_text="Source ID value that identifies this row",
    )
    source_asset_tag = models.CharField(
        max_length=100,
        blank=True,
        default="",
        help_text="Asset tag from source row (for display / lookup; may become stale)",
    )
    netbox_device_id = models.PositiveIntegerField(
        help_text="Primary key of the matched NetBox Device",
    )
    device_name = models.CharField(
        max_length=200,
        blank=True,
        help_text="NetBox device name (for display only; may become stale)",
    )

    class Meta:
        ordering = ["profile", "source_id"]
        constraints = [
            models.UniqueConstraint(fields=["profile", "source_id"], name="ndi_devicematch_profile_srcid"),
        ]
        verbose_name = "Device Existing Match"
        verbose_name_plural = "Device Existing Matches"

    def __str__(self):
        tag = f" / {self.source_asset_tag}" if self.source_asset_tag else ""
        return f"{self.source_id}{tag} → Device #{self.netbox_device_id} ({self.device_name})"
