# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
from django.core.exceptions import ValidationError
from django.db import models
from django.urls import reverse
from netbox.models import NetBoxModel

from .adapters import (
    DEFAULT_ADAPTER_KEY,
    UnknownSourceAdapter,
    adapter_choices,
    get_adapter,
    output_kinds_for,
)
from .catalog import CATALOG, policy_section

CONTACT_RESOLUTION_FIELDS = frozenset({"name", "email", "phone"})
CONTACT_RESOLUTION_REQUIRED_KEYS = frozenset({"contact_resolution_applied", "contact_field_sources"})
CONTACT_RESOLUTION_KEYS = CONTACT_RESOLUTION_REQUIRED_KEYS | frozenset({"contact_field_values", "contact_id"})


def validate_section_applicability(profile, section_key):
    """Reject a policy row whose section does not apply to its profile's Source Adapter."""
    section = policy_section(section_key)
    if section is None or profile is None:
        return
    if not section.applies_to(profile.output_kinds):
        raise ValidationError(
            f"{section.label} do not apply to a profile using the '{profile.source_adapter}' source adapter."
        )


def _validated_contact_id(contact_id):
    """Return a saved Contact ID as a positive int, rejecting anything int() would reshape."""
    if contact_id in ("", None):
        return None
    # int() truncates, so a JSON float would silently select a different Contact.
    if isinstance(contact_id, bool) or (isinstance(contact_id, float) and not contact_id.is_integer()):
        raise ValidationError("The selected Contact ID is invalid.")
    try:
        contact_id = int(contact_id)
    except (TypeError, ValueError) as exc:
        raise ValidationError("The selected Contact ID is invalid.") from exc
    if contact_id < 1:
        raise ValidationError("The selected Contact ID is invalid.")
    return contact_id


def validate_contact_candidate_resolution(
    resolved_fields,
    lookup_field: str,
    available_source_columns,
) -> dict:
    """Validate and normalize one saved Contact candidate resolution."""
    if (
        not isinstance(resolved_fields, dict)
        or not CONTACT_RESOLUTION_REQUIRED_KEYS <= set(resolved_fields)
        or set(resolved_fields) - CONTACT_RESOLUTION_KEYS
    ):
        raise ValidationError("The Contact candidate resolution has an invalid structure.")
    if resolved_fields.get("contact_resolution_applied") is not True:
        raise ValidationError("The Contact candidate resolution is not marked as applied.")

    field_sources = resolved_fields.get("contact_field_sources")
    if not isinstance(field_sources, dict) or set(field_sources) - CONTACT_RESOLUTION_FIELDS:
        raise ValidationError("The Contact candidate resolution contains an unknown field.")
    if any(not isinstance(source_column, str) or not source_column for source_column in field_sources.values()):
        raise ValidationError("Each resolved Contact field must select one source column.")
    missing_sources = set(field_sources.values()) - set(available_source_columns)
    if missing_sources:
        missing = sorted(missing_sources)[0]
        raise ValidationError(f"The source column '{missing}' has no candidate value in this row.")

    field_values = resolved_fields.get("contact_field_values", {})
    if not isinstance(field_values, dict) or set(field_values) - CONTACT_RESOLUTION_FIELDS:
        raise ValidationError("The Contact candidate resolution contains an unknown literal field.")
    if any(not isinstance(value, str) or not value.strip() for value in field_values.values()):
        raise ValidationError("Each literal Contact field must contain text.")
    overlap = set(field_sources) & set(field_values)
    if overlap:
        raise ValidationError(f"Select a source column or enter a value for Contact {sorted(overlap)[0]}, not both.")

    contact_id = _validated_contact_id(resolved_fields.get("contact_id"))

    supplied_fields = set(field_sources) | set(field_values)
    if supplied_fields and "name" not in supplied_fields:
        raise ValidationError("Select a source column or enter a value for the Contact name.")
    if supplied_fields and lookup_field not in supplied_fields:
        raise ValidationError(f"Select a source column or enter a value for the Contact {lookup_field} lookup field.")
    return {
        "field_sources": field_sources,
        "field_values": {field: value.strip() for field, value in field_values.items()},
        "contact_id": contact_id,
    }


class ImportProfile(NetBoxModel):
    """Named configuration for one source file format."""

    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)
    source_adapter = models.CharField(
        max_length=50,
        choices=adapter_choices,
        default=DEFAULT_ADAPTER_KEY,
        help_text="Source format this profile reads. It cannot change after creation.",
    )
    adapter_config = models.JSONField(
        default=dict,
        blank=True,
        help_text="Scalar settings the selected Source Adapter declares.",
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

    @property
    def adapter(self):
        """Return the registered Source Adapter class for this profile."""
        return get_adapter(self.source_adapter)

    @property
    def output_kinds(self) -> frozenset[str]:
        """Return the adapter output kinds this profile can supply."""
        return output_kinds_for(self.source_adapter)

    @property
    def adapter_settings(self):
        """Return attribute access over ``adapter_config`` backed by the adapter's declared defaults."""
        return AdapterSettings(self.adapter, self.adapter_config, self.source_adapter)

    @property
    def adapter_config_display(self):
        """Return (label, value) pairs for the adapter's declared settings, in declaration order."""
        from django import forms
        from django.forms.utils import pretty_name

        adapter = self.adapter
        if adapter is None:
            return []
        config = self.adapter_config or {}
        rows = []
        for name, field in adapter.config_form_class().base_fields.items():
            value = config.get(name, field.initial)
            if isinstance(field, forms.ChoiceField) and not isinstance(field, forms.ModelChoiceField):
                value = dict(field.choices).get(value, value)
            rows.append((field.label or pretty_name(name), value))
        return rows

    @property
    def resolved_primary_contact_role(self):
        """Return the referenced Contact Role object, or None when unset or dangling.

        Planning reads this once per row, so the lookup is memoized against the configured name. A
        plain instance cache would keep returning the old role after ``adapter_config`` changes.
        """
        name = self.adapter_settings.primary_contact_role
        if not name:
            return None
        cached = self.__dict__.get("_primary_contact_role_cache")
        if cached is not None and cached[0] == name:
            return cached[1]
        from tenancy.models import ContactRole

        role = ContactRole.objects.filter(name=name).first()
        self.__dict__["_primary_contact_role_cache"] = (name, role)
        return role

    def clean(self):
        """Reject an adapter change after creation and validate the adapter configuration."""
        super().clean()
        adapter = self.adapter
        if adapter is None:
            raise ValidationError({"source_adapter": f"Unknown source adapter '{self.source_adapter}'."})
        if self.pk is not None:
            stored = type(self).objects.filter(pk=self.pk).values_list("source_adapter", flat=True).first()
            if stored is not None and stored != self.source_adapter:
                raise ValidationError(
                    {"source_adapter": "The source adapter cannot change after the profile is created."}
                )
        self.adapter_config = adapter.config_form_class().validate_config(self.adapter_config)


class AdapterSettings:
    """Read one adapter setting, falling back to the adapter form's declared default."""

    def __init__(self, adapter, config, adapter_key):
        self._adapter_key = adapter_key
        self._fields = adapter.config_form_class().base_fields if adapter is not None else None
        self._config = config if isinstance(config, dict) else {}

    def __getattr__(self, name):
        fields = object.__getattribute__(self, "_fields")
        if fields is None:
            key = object.__getattribute__(self, "_adapter_key")
            raise UnknownSourceAdapter(f"This release does not register the source adapter '{key}'.")
        if name not in fields:
            raise AttributeError(f"'{name}' is not a setting of this profile's source adapter")
        config = object.__getattribute__(self, "_config")
        return config.get(name, fields[name].initial)


class PolicySectionModel(models.Model):
    """A profile policy table scoped to the adapter output kinds its catalog section declares."""

    POLICY_SECTION = ""

    class Meta:
        abstract = True

    def clean(self):
        """Reject a row whose section does not apply to the profile's Source Adapter."""
        super().clean()
        validate_section_applicability(self.profile if self.profile_id else None, self.POLICY_SECTION)


class ColumnMapping(PolicySectionModel):
    """Maps one source column header to one semantic NetBox field."""

    POLICY_SECTION = "column_mappings"

    profile = models.ForeignKey(
        ImportProfile,
        on_delete=models.CASCADE,
        related_name="column_mappings",
    )
    source_column = models.CharField(
        max_length=200,
        help_text="Exact column header in the source file (case-sensitive)",
    )
    target_field = models.CharField(max_length=100)

    class Meta:
        ordering = ["profile", "target_field"]
        verbose_name = "Column Mapping"
        verbose_name_plural = "Column Mappings"

    def clean(self):
        """Resolve the target field through the catalog and reject an inapplicable row."""
        super().clean()
        value = self.target_field or ""
        if not CATALOG.is_valid(value, output_kinds=self.profile.output_kinds if self.profile_id else None):
            raise ValidationError({"target_field": CATALOG.invalid_key_message(value)})

    def get_target_field_display(self):
        """Return the human-readable name for the target_field value."""
        return CATALOG.display(self.target_field)

    def __str__(self):
        return f"{self.source_column} → {self.get_target_field_display()}"

    def get_absolute_url(self):
        """Return the edit URL for this column mapping."""
        return reverse("plugins:netbox_data_import:columnmapping_edit", args=[self.pk])


class ClassRoleMapping(PolicySectionModel):
    """Maps a source 'class' value to a NetBox outcome (rack or device role)."""

    POLICY_SECTION = "class_role_mappings"

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
    rack_type = models.ForeignKey(
        to="dcim.RackType",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text="Optional rack type assigned when creating racks",
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
            suffix = f" ({self.rack_type})" if self.rack_type_id else ""
            return f"{self.source_class} → Rack{suffix}"
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


class DeviceTypeMapping(PolicySectionModel):
    """Explicit (make, model) override when source naming doesn't slugify cleanly."""

    POLICY_SECTION = "device_type_mappings"

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


class ManufacturerMapping(PolicySectionModel):
    """Maps a source 'make' value to an existing NetBox manufacturer slug."""

    POLICY_SECTION = "manufacturer_mappings"

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


class IgnoredDevice(PolicySectionModel):
    """Per-device ignore record — prevents a specific source device from being imported."""

    POLICY_SECTION = "ignored_devices"

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


class ColumnTransformRule(PolicySectionModel):
    r"""Regex-based transform applied to a source column during parse.

    Example: source_column='Name', pattern='^(\w{4,8}) - (.+)$',
    group_1_target='asset_tag', group_2_target='device_name'
    transforms "TEST0001 - EXAMPLE-SWITCH-01" into asset_tag="TEST0001", device_name="EXAMPLE-SWITCH-01".
    """

    POLICY_SECTION = "column_transform_rules"

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
        max_length=100,
        blank=True,
        help_text="Target field for capture group 1 (leave blank to ignore)",
    )
    group_2_target = models.CharField(
        max_length=100,
        blank=True,
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
        """Validate the regex pattern, group counts, and group target field names."""
        import re

        from django.core.exceptions import ValidationError

        super().clean()

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

        output_kinds = self.profile.output_kinds if self.profile_id else None
        for attr in ("group_1_target", "group_2_target"):
            value = getattr(self, attr) or ""
            if not value:
                continue
            # A capture group yields text, so a candidate target is not a valid group target.
            if not CATALOG.is_valid(value, output_kinds=output_kinds, allow_candidates=False):
                raise ValidationError({attr: CATALOG.invalid_key_message(value)})

    def __str__(self):
        return f"{self.source_column}: {self.pattern}"

    def get_absolute_url(self):
        """Return the edit URL for this column transform rule."""
        return reverse("plugins:netbox_data_import:columntransformrule_edit", args=[self.pk])


class SourceResolution(PolicySectionModel):
    """Saved target-field decision for one source row.

    A resolution can split one source value or select candidate source columns
    for structured target fields. The import reapplies it when the same source
    row appears in a later file.
    """

    POLICY_SECTION = "source_resolutions"

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
        help_text="Dict of target_field -> resolved_value (e.g. {'device_name': 'SW1', 'asset_tag': 'TEST0001'})",
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


class DeviceExistingMatch(PolicySectionModel):
    """Explicit match between a source row and an existing NetBox device.

    When a user clicks "Link existing" on a device preview row, this record is saved.
    On re-import, the engine uses this to emit action='update' against the matched device
    instead of action='create', even if the device has no source-ID custom field yet.
    """

    POLICY_SECTION = "device_existing_matches"

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
            models.UniqueConstraint(
                fields=["profile", "netbox_device_id"],
                name="ndi_devicematch_profile_device",
            ),
        ]
        verbose_name = "Device Existing Match"
        verbose_name_plural = "Device Existing Matches"

    def __str__(self):
        tag = f" / {self.source_asset_tag}" if self.source_asset_tag else ""
        return f"{self.source_id}{tag} → Device #{self.netbox_device_id} ({self.device_name})"


class IgnoredFieldDifference(PolicySectionModel):
    """Preserve one exact file/NetBox value pair for a device field difference."""

    POLICY_SECTION = "ignored_field_differences"

    profile = models.ForeignKey(
        ImportProfile,
        on_delete=models.CASCADE,
        related_name="ignored_field_differences",
    )
    source_id = models.CharField(
        max_length=200,
        help_text="Source ID of the row this review applies to",
    )
    netbox_device_id = models.PositiveIntegerField(
        help_text="Primary key of the matched NetBox Device",
    )
    target_field = models.CharField(
        max_length=100,
        help_text="Target field whose current difference is ignored",
    )
    file_snapshot = models.JSONField(
        default=dict,
        help_text="Normalized and display values from the source row",
    )
    netbox_snapshot = models.JSONField(
        default=dict,
        help_text="Normalized and display values from the matched NetBox device",
    )

    class Meta:
        ordering = ["profile", "source_id", "target_field"]
        constraints = [
            models.UniqueConstraint(
                fields=["profile", "source_id", "netbox_device_id", "target_field"],
                name="ndi_ignored_diff_profile_source_device_field",
            ),
        ]
        verbose_name = "Ignored Field Difference"
        verbose_name_plural = "Ignored Field Differences"

    def __str__(self):
        return f"{self.source_id}/{self.target_field} on device #{self.netbox_device_id} (ignored)"


class DeviceImportSource(models.Model):
    """Import provenance the plugin keeps for one Device.

    Replaces the plugin-managed ``data_import_source`` custom field. The per-profile custom
    field an operator configures (``ImportProfile.custom_field_name``) is separate and stays.
    """

    device = models.OneToOneField(
        to="dcim.Device",
        on_delete=models.CASCADE,
        related_name="data_import_source",
    )
    profile = models.ForeignKey(
        ImportProfile,
        on_delete=models.CASCADE,
        related_name="device_sources",
    )
    source_id = models.CharField(
        max_length=200,
        blank=True,
        default="",
        help_text="Source ID of the row that wrote this device",
    )
    extra_columns = models.JSONField(
        default=dict,
        blank=True,
        help_text="Source column values that no mapping consumes",
    )
    unassigned_ips = models.JSONField(
        default=dict,
        blank=True,
        help_text="IP values the import could not assign to a NetBox IP field",
    )

    class Meta:
        ordering = ["device"]
        indexes = [models.Index(fields=["profile", "source_id"])]
        verbose_name = "Device Import Source"
        verbose_name_plural = "Device Import Sources"

    def __str__(self):
        return f"{self.source_id or '(no source ID)'} → Device #{self.device_id}"


def stored_import_source(obj):
    """Return the plugin's import record for one object, or None when it holds none."""
    from dcim.models import Device

    if not isinstance(obj, Device) or obj.pk is None:
        return None
    return DeviceImportSource.objects.filter(device_id=obj.pk).first()
