# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
import hashlib
from contextlib import contextmanager
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, models, transaction
from django.urls import reverse
from django.utils import timezone
from core.choices import JobStatusChoices
from core.models import Job
from netbox.models import NetBoxModel

from .adapters import (
    DEFAULT_ADAPTER_KEY,
    UnknownSourceAdapter,
    adapter_choices,
    get_adapter,
    output_kinds_for,
)
from . import plan
from .catalog import CATALOG, POLICY_SECTIONS, has_implemented_module, policy_section

CONTACT_RESOLUTION_FIELDS = frozenset({"name", "email", "phone"})
CONTACT_RESOLUTION_REQUIRED_KEYS = frozenset({"contact_resolution_applied", "contact_field_sources"})
CONTACT_RESOLUTION_KEYS = CONTACT_RESOLUTION_REQUIRED_KEYS | frozenset({"contact_field_values", "contact_id"})


def validate_adapter_target_module(adapter_key):
    """Reject a Source Adapter whose Target Module this release does not implement yet."""
    if not has_implemented_module(output_kinds_for(adapter_key)):
        raise ValidationError(
            {"source_adapter": f"This release cannot import from the '{adapter_key}' source adapter yet."}
        )


def validate_registered_adapter(profile):
    """Reject a profile whose stored Source Adapter this release does not register."""
    if profile is not None and profile.adapter is None:
        raise ValidationError(
            f"This profile uses the source adapter '{profile.source_adapter}', which this release does not register."
        )


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


@contextmanager
def locked_profile_policy(*profile_ids):
    """Hold the given profile rows for a policy write or for an import execution.

    Every SourceResolution write and the import worker take this same lock, so a decision cannot
    commit between the worker's check and its writes. Locking the resolution rows alone would leave
    an insert free to land in that window, because a row that does not exist yet cannot be locked.

    The rows lock in primary-key order, so two callers naming several profiles cannot deadlock by
    taking them in opposite orders.
    """
    wanted = sorted({profile_id for profile_id in profile_ids if profile_id is not None})
    # Django short-circuits `pk__in=[]`, so an empty set would yield without ever taking a lock.
    if not wanted:
        raise ImportProfile.DoesNotExist("A policy write must name at least one ImportProfile to lock.")
    with transaction.atomic():
        locked = ImportProfile.objects.select_for_update().filter(pk__in=wanted).order_by("pk")
        if len(locked) != len(wanted):
            raise ImportProfile.DoesNotExist(f"No ImportProfile matches every id in {wanted}.")
        yield


@contextmanager
def locked_resolution_policy(resolution_pk):
    """Hold the profile a saved resolution belongs to, read from the database rather than trusted.

    A caller reaches this holding an instance it fetched earlier, whose profile may be a stale copy.
    The row is read again under the lock, so the caller acts on a row that still exists and still
    belongs to the locked profile.
    """
    gone = SourceResolution.DoesNotExist(f"No SourceResolution matches id {resolution_pk}.")
    profile_id = SourceResolution.objects.filter(pk=resolution_pk).values_list("profile_id", flat=True).first()
    if profile_id is None:
        raise gone
    with locked_profile_policy(profile_id):
        # A delete can still commit in the gap above, and a write that saw the row would resurrect it.
        if not SourceResolution.objects.filter(pk=resolution_pk, profile_id=profile_id).exists():
            raise gone
        yield


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

    def _validate_source_adapter_immutability(self):
        """Return the persisted adapter and reject a different selected adapter."""
        # A set pk does not prove that the row exists. An unsaved instance can carry a pk.
        stored = (
            type(self).objects.filter(pk=self.pk).values_list("source_adapter", flat=True).first()
            if self.pk is not None
            else None
        )
        if stored is not None and stored != self.source_adapter:
            raise ValidationError({"source_adapter": "The source adapter cannot change after the profile is created."})
        return stored

    def save(self, *args, **kwargs):
        """Normalize adapter configuration on every supported write that stores it."""
        update_fields = kwargs.get("update_fields")
        updated = set(update_fields) if update_fields is not None else None
        if updated is None or updated & {"source_adapter", "adapter_config"}:
            self._validate_source_adapter_immutability()
        if updated is None or "adapter_config" in updated:
            adapter = self.adapter
            if adapter is None:
                raise ValidationError({"source_adapter": f"Unknown source adapter '{self.source_adapter}'."})
            self.adapter_config = adapter.config_form_class().validate_config(self.adapter_config)
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        """Take the policy lock before the cascade, which would otherwise take the child rows first.

        A policy write holds this row and then writes a child, so a cascade in the opposite order
        deadlocks against it. NetBox deletes each object through this method, in bulk as well.
        """
        with locked_profile_policy(self.pk):
            # atomic-exit-safe: locked-cascade-committed
            return super().delete(*args, **kwargs)

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
        cache_key = (self.source_adapter, id(self.adapter_config))
        cached = self.__dict__.get("_adapter_settings_cache")
        if cached is not None and cached[0] == cache_key:
            return cached[1]
        settings = AdapterSettings(self.adapter, self.adapter_config, self.source_adapter)
        self.__dict__["_adapter_settings_cache"] = (cache_key, settings)
        return settings

    @property
    def adapter_config_display(self):
        """Return (label, value) pairs for the adapter's declared settings, in declaration order."""
        from django import forms
        from django.forms.utils import pretty_name

        config = _require_adapter_config_mapping(self.adapter_config)
        adapter = self.adapter
        if adapter is None:
            return []
        rows = []
        for name, field in adapter.config_form_class().base_fields.items():
            value = config.get(name, field.initial)
            if isinstance(field, forms.ChoiceField) and not isinstance(field, forms.ModelChoiceField):
                value = dict(field.choices).get(value, value)
            rows.append((field.label or pretty_name(name), value))
        return rows

    def grouped_column_map(self) -> dict[str, list[str]]:
        """Return this profile's mapped source columns, keyed by Target Field."""
        grouped: dict[str, list[str]] = {}
        # Two columns can feed one Target Field, and which one wins must not be a query-order accident.
        for mapping in self.column_mappings.order_by("target_field", "pk"):
            grouped.setdefault(mapping.target_field, []).append(mapping.source_column)
        return grouped

    @property
    def planning_fingerprint(self) -> str:
        """Return the fingerprint of every profile value planning depends on."""
        related_sections = {
            getattr(relation.related_model, "POLICY_SECTION", ""): relation.get_accessor_name()
            for relation in self._meta.related_objects
        }
        sections = []
        for section in POLICY_SECTIONS:
            accessor = related_sections[section.key]
            serialized_rows = []
            for row in getattr(self, accessor).all():
                serialized_rows.append(
                    {
                        field.name: field.value_from_object(row)
                        for field in row._meta.concrete_fields
                        if field.name not in {"id", "profile"}
                    }
                )
            serialized_rows.sort(key=plan.canonical_json)
            sections.append({"key": section.key, "rows": serialized_rows})
        return plan.fingerprint_of(
            {
                "profile_id": self.pk,
                "source_adapter": self.source_adapter,
                "adapter_config": self.adapter_config,
                "policy_sections": sections,
            }
        )

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
        stored = self._validate_source_adapter_immutability()
        if stored is None:
            # A creation rule only: the adapter is immutable, so a stored profile keeps validating.
            validate_adapter_target_module(self.source_adapter)
        self.adapter_config = adapter.config_form_class().validate_config(self.adapter_config)


def _require_adapter_config_mapping(config):
    """Return a stored adapter configuration mapping or expose corrupt JSON state."""
    if not isinstance(config, dict):
        raise ValidationError(
            "The stored adapter configuration must be a mapping. Replace it with a valid JSON mapping."
        )
    return config


class AdapterSettings:
    """Read one adapter setting, falling back to the adapter form's declared default."""

    def __init__(self, adapter, config, adapter_key):
        self._adapter_key = adapter_key
        self._fields = adapter.config_form_class().base_fields if adapter is not None else None
        self._config = _require_adapter_config_mapping(config)

    def __getattr__(self, name):
        fields = object.__getattribute__(self, "_fields")
        if fields is None:
            key = object.__getattribute__(self, "_adapter_key")
            raise UnknownSourceAdapter(f"This release does not register the source adapter '{key}'.")
        if name not in fields:
            raise AttributeError(f"'{name}' is not a setting of this profile's source adapter")
        config = object.__getattribute__(self, "_config")
        field = fields[name]
        if name not in config:
            return field.initial
        value = config[name]
        if field.required and (value is None or value == ""):
            raise ValidationError(f"The required adapter setting '{name}' is empty. Edit and save this import profile.")
        return value


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


class SourceDocument(models.Model):
    """The stored uploaded workbook that a plan and its executions read.

    Preview, replanning, a background execution, and an audit read all resolve the same bytes, so the
    plan carries a reference instead of the content.
    """

    RETENTION = timedelta(days=30)

    # Audit input outlives its profile, so a delete orphans the row and retention reclaims it.
    profile = models.ForeignKey(
        ImportProfile, on_delete=models.SET_NULL, null=True, blank=True, related_name="source_documents"
    )
    content = models.BinaryField()
    content_fingerprint = models.CharField(max_length=64)
    filename = models.CharField(max_length=255, blank=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created"]
        indexes = [models.Index(fields=["profile", "content_fingerprint"])]
        verbose_name = "Source Document"
        verbose_name_plural = "Source Documents"

    def __str__(self):
        return f"{self.filename or 'upload'} ({self.content_fingerprint[:12]})"

    @staticmethod
    def fingerprint(content: bytes) -> str:
        """Return the content fingerprint, which is what a plan compares against."""
        return hashlib.sha256(bytes(content)).hexdigest()

    @classmethod
    def store(cls, *, profile, content, filename="", uploaded_by=None):
        """Store one upload. A newer upload never removes an older one."""
        return cls.objects.create(
            profile=profile,
            content=bytes(content),
            content_fingerprint=cls.fingerprint(content),
            filename=filename,
            uploaded_by=uploaded_by,
        )

    @classmethod
    def purge_unreferenced(cls, *, now=None) -> int:
        """Delete unreferenced uploads past the retention window and return the count.

        A document an Import Execution references is permanent audit input, so the queryset excludes
        it and the protecting foreign key backs that up.
        """
        cutoff = (now or timezone.now()) - cls.RETENTION
        # The protecting relation forces a row-by-row collect, so defer the bytes one purge would load.
        stale = cls.objects.filter(import_executions__isnull=True, created__lt=cutoff).defer("content")
        return stale.delete()[0]


class ExecutionOutcome:
    """The outcome vocabulary of an Import Execution (section 9.2)."""

    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"

    CHOICES = ((PENDING, "Pending"), (SUCCEEDED, "Succeeded"), (FAILED, "Failed"))


class FailureReason:
    """Typed failure reasons an Import Execution records."""

    ABANDONED = "abandoned"
    DATABASE = "database"
    PERMISSION = "permission"
    PRECONDITION = "precondition"
    PLANNING = "planning"
    SELECTION = "selection"
    STALE_PLAN = "stale_plan"
    VALIDATION = "validation"


class ImportExecution(models.Model):
    """The audit record of one selective or final execution.

    Rows created before the plan cutover keep their historical columns, have null new fields, and are
    display-only: they never satisfy an idempotency lookup and never take part in plan comparison.
    """

    #: A synchronous attempt cannot outlive the web request bound, so an older pending row is gone.
    SYNCHRONOUS_BOUND = timedelta(minutes=10)

    profile = models.ForeignKey(
        ImportProfile,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="import_executions",
    )
    created = models.DateTimeField(auto_now_add=True)
    input_filename = models.CharField(max_length=255, blank=True)
    site_name = models.CharField(max_length=100, blank=True)
    result_counts = models.JSONField(default=dict)

    source_document = models.ForeignKey(
        SourceDocument,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="import_executions",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    idempotency_key = models.CharField(max_length=64, null=True, blank=True)
    plan_schema_version = models.PositiveIntegerField(null=True, blank=True)
    accepted_plan_fingerprint = models.CharField(max_length=64, null=True, blank=True)
    selected_units = models.JSONField(null=True, blank=True)
    outcome = models.CharField(max_length=16, choices=ExecutionOutcome.CHOICES, null=True, blank=True)
    applied_changes = models.JSONField(null=True, blank=True)
    failure_detail = models.JSONField(null=True, blank=True)
    # Set by link_job: the reservation commits before the Job exists, and outlives a deleted Job.
    job_backed = models.BooleanField(default=False)
    job = models.OneToOneField(
        "core.Job", on_delete=models.SET_NULL, null=True, blank=True, related_name="import_execution"
    )

    class Meta:
        ordering = ["-created"]
        constraints = [
            models.UniqueConstraint(
                fields=["profile", "idempotency_key"],
                condition=models.Q(idempotency_key__isnull=False),
                name="ndi_execution_profile_idempotency_key",
            ),
        ]
        verbose_name = "Import Execution"
        verbose_name_plural = "Import Executions"

    def __str__(self):
        return f"Import {self.pk} — {self.created:%Y-%m-%d %H:%M} ({self.input_filename})"

    def get_absolute_url(self):
        """Return the associated profile's URL (no per-execution detail view exists)."""
        if not self.profile_id:
            return reverse("plugins:netbox_data_import:importprofile_list")
        return reverse("plugins:netbox_data_import:importprofile", args=[self.profile_id])

    @classmethod
    def reserve(cls, **fields):
        """Insert and commit the pending row, or return the row already holding this key.

        The insert reserves the unique (Import Profile, idempotency key), so a duplicate submission
        or job delivery loses the race and returns the existing row in any outcome.
        """
        if transaction.get_connection().in_atomic_block:
            raise RuntimeError("The Import Execution reservation must commit before the target transaction opens.")
        if not fields.get("idempotency_key"):
            raise ValueError("An Import Execution reservation requires an idempotency key.")
        # PostgreSQL treats two NULL profiles as distinct, so the partial unique index cannot hold.
        if not fields.get("profile"):
            raise ValueError("An Import Execution reservation requires an Import Profile.")
        required_audit_fields = {
            "source_document": "a Source Document",
            "actor": "an actor",
            "plan_schema_version": "a plan schema version",
            "accepted_plan_fingerprint": "an accepted plan fingerprint",
            "selected_units": "selected Synchronization Unit identities",
        }
        for field_name, label in required_audit_fields.items():
            if fields.get(field_name) is None:
                raise ValueError(f"An Import Execution reservation requires {label}.")
        existing = cls.for_idempotency(fields["profile"], fields["idempotency_key"])
        if existing is not None:
            return existing, False
        try:
            return cls.objects.create(outcome=ExecutionOutcome.PENDING, **fields), True
        except IntegrityError:
            # Only a lost race for this key is recoverable; any other constraint failure must surface.
            winner = cls.for_idempotency(fields["profile"], fields["idempotency_key"])
            if winner is None:
                raise
            return winner, False

    def link_job(self, job):
        """Record the native Job that runs this execution, after the reservation has committed."""
        self.job = job
        self.job_backed = True
        self.save(update_fields=["job", "job_backed"])
        return self

    @classmethod
    def for_idempotency(cls, profile, idempotency_key):
        """Return the reserved row for this key, reconciled, or None. A legacy row never matches."""
        if not idempotency_key:
            return None
        found = cls.objects.filter(profile=profile, idempotency_key=idempotency_key).first()
        return found.reconcile_pending() if found is not None else None

    def reconcile_pending(self, *, now=None):
        """Transition an abandoned pending row to failed, so no sweeper is needed."""
        if self.outcome != ExecutionOutcome.PENDING:
            return self
        if self.job_backed:
            # The Job decides once it exists; a deleted Job leaves no way to finish the attempt.
            job = Job.objects.filter(pk=self.job_id).first() if self.job_id else None
            live = job is not None and job.status in JobStatusChoices.ENQUEUED_STATE_CHOICES
        else:
            # Either a synchronous attempt or a background one still between reserving and enqueuing.
            live = self.created > (now or timezone.now()) - self.SYNCHRONOUS_BOUND
        if live:
            return self
        try:
            self.mark_failed(reason=FailureReason.ABANDONED)
        except ValueError:
            # A worker finished the row between this read and the transition; its outcome wins.
            pass
        return self

    def _finish(self, **values):
        """Transition this row out of pending exactly once, with the database as the arbiter.

        Two instances can both hold a pending copy, so an in-memory check would let the second
        write overwrite a committed outcome and destroy the audit evidence.
        """
        updated = type(self).objects.filter(pk=self.pk, outcome=ExecutionOutcome.PENDING).update(**values)
        if not updated:
            self.refresh_from_db()
            raise ValueError(f"Import Execution {self.pk} already finished as '{self.outcome}'.")
        for name, value in values.items():
            setattr(self, name, value)
        return self

    def mark_succeeded(self, *, applied_changes):
        """Record the applied identities and the deleted-object snapshot."""
        return self._finish(outcome=ExecutionOutcome.SUCCEEDED, applied_changes=applied_changes, failure_detail=None)

    def mark_failed(self, *, reason, failed_change=None, rolled_back=(), not_attempted=()):
        """Record what failed, what rolled back, and what was never attempted."""
        return self._finish(
            outcome=ExecutionOutcome.FAILED,
            applied_changes=None,
            failure_detail={
                "failed_change": failed_change,
                "rolled_back": list(rolled_back),
                "not_attempted": list(not_attempted),
                "reason": reason,
            },
        )


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
