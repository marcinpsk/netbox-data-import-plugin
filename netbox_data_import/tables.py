# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
import django_tables2 as tables
from netbox.tables import NetBoxTable, columns
from .models import (
    CableClassMapping,
    ImportProfile,
    ColumnMapping,
    ClassRoleMapping,
    DeviceTypeMapping,
    ColumnTransformRule,
    ImportExecution,
)


_MAPPING_ACTIONS_TEMPLATE = """
<a href="{% url edit_url record.pk %}" class="btn btn-sm btn-warning" aria-label="Edit {{ record }}">
    <i class="mdi mdi-pencil"></i>
</a>
<a href="{% url delete_url record.pk %}" class="btn btn-sm btn-danger" aria-label="Delete {{ record }}">
    <i class="mdi mdi-trash-can-outline"></i>
</a>
"""


def _mapping_actions_column(route_prefix: str) -> tables.TemplateColumn:
    """Return the edit and delete actions every inline mapping table shares."""
    return tables.TemplateColumn(
        template_code=_MAPPING_ACTIONS_TEMPLATE,
        extra_context={
            "edit_url": f"plugins:netbox_data_import:{route_prefix}_edit",
            "delete_url": f"plugins:netbox_data_import:{route_prefix}_delete",
        },
        verbose_name="",
        orderable=False,
    )


class ImportProfileTable(NetBoxTable):
    """Table for listing ImportProfile objects."""

    name = tables.Column(linkify=True)
    source_adapter = tables.Column()
    column_mappings = tables.Column(
        accessor="column_mappings.count",
        verbose_name="Columns",
        orderable=False,
    )
    class_role_mappings = tables.Column(
        accessor="class_role_mappings.count",
        verbose_name="Class Mappings",
        orderable=False,
    )
    device_type_mappings = tables.Column(
        accessor="device_type_mappings.count",
        verbose_name="DT Mappings",
        orderable=False,
    )
    actions = columns.ActionsColumn(actions=("edit", "delete"))

    class Meta(NetBoxTable.Meta):
        model = ImportProfile
        fields = (
            "pk",
            "name",
            "source_adapter",
            "column_mappings",
            "class_role_mappings",
            "device_type_mappings",
            "actions",
        )
        default_columns = (
            "name",
            "source_adapter",
            "column_mappings",
            "class_role_mappings",
            "device_type_mappings",
            "actions",
        )


class ColumnMappingTable(tables.Table):
    """Table for displaying ColumnMapping objects inline on the profile detail page."""

    source_column = tables.Column()
    target_field = tables.Column(accessor="get_target_field_display", order_by="target_field")
    actions = _mapping_actions_column("columnmapping")

    class Meta:
        model = ColumnMapping
        fields = ("source_column", "target_field", "actions")


class ClassRoleMappingTable(tables.Table):
    """Table for displaying ClassRoleMapping objects inline on the profile detail page."""

    source_class = tables.Column()
    creates_rack = tables.BooleanColumn()
    rack_type = tables.Column(verbose_name="Rack Type", accessor="rack_type", default="—")
    role_slug = tables.Column()
    ignore = tables.BooleanColumn()
    actions = _mapping_actions_column("classrolemapping")

    class Meta:
        model = ClassRoleMapping
        fields = ("source_class", "creates_rack", "rack_type", "role_slug", "ignore", "actions")


class CableClassMappingTable(tables.Table):
    """Display CableClass target decisions inline on the profile detail page."""

    cable_class = tables.Column(verbose_name="CableClass")
    cable_type = tables.Column(accessor="cable_type_display", order_by="cable_type", verbose_name="Cable Type")
    cable_profile = tables.Column(
        accessor="cable_profile_display", order_by="cable_profile", verbose_name="Cable Profile"
    )
    actions = _mapping_actions_column("cableclassmapping")

    class Meta:
        model = CableClassMapping
        fields = ("cable_class", "cable_type", "cable_profile", "actions")


class DeviceTypeMappingTable(tables.Table):
    """Table for displaying DeviceTypeMapping objects inline on the profile detail page."""

    source_make = tables.Column()
    source_model = tables.Column()
    netbox_manufacturer_slug = tables.Column()
    netbox_device_type_slug = tables.Column()
    actions = _mapping_actions_column("devicetypemapping")

    class Meta:
        model = DeviceTypeMapping
        fields = (
            "source_make",
            "source_model",
            "netbox_manufacturer_slug",
            "netbox_device_type_slug",
            "actions",
        )


class ImportExecutionTable(NetBoxTable):
    """Table for listing Import Execution records in the history view."""

    created = tables.DateTimeColumn(format="Y-m-d H:i")
    profile = tables.Column(linkify=lambda record: record.profile.get_absolute_url() if record.profile else None)
    input_filename = tables.Column(verbose_name="File")
    site_name = tables.Column(verbose_name="Site")
    racks_created = tables.Column(
        accessor="result_counts",
        verbose_name="Racks",
        orderable=False,
    )
    devices_created = tables.Column(
        accessor="result_counts",
        verbose_name="Devices",
        orderable=False,
    )
    errors = tables.TemplateColumn(
        template_code="""
        {% with c=record.result_counts %}
        {% if c.errors %}<span class="badge bg-danger">{{ c.errors }}</span>{% else %}—{% endif %}
        {% endwith %}
        """,
        verbose_name="Errors",
        orderable=False,
    )

    class Meta(NetBoxTable.Meta):
        model = ImportExecution
        fields = (
            "pk",
            "created",
            "profile",
            "input_filename",
            "site_name",
            "racks_created",
            "devices_created",
            "errors",
        )
        default_columns = (
            "created",
            "profile",
            "input_filename",
            "site_name",
            "racks_created",
            "devices_created",
            "errors",
        )

    def render_profile(self, record):
        """Return the profile name, or a placeholder for deleted profiles."""
        if record.profile:
            return record.profile.name
        return "(deleted)"

    def render_racks_created(self, value):
        """Extract racks_created count from the JSON result_counts dict."""
        value = value or {}
        if isinstance(value.get("created"), dict):
            return value["created"].get("rack", 0)
        return value.get("racks_created", 0)

    def render_devices_created(self, value):
        """Extract devices_created count from the JSON result_counts dict."""
        value = value or {}
        if isinstance(value.get("created"), dict):
            return value["created"].get("device", 0)
        return value.get("devices_created", 0)


class ColumnTransformRuleTable(tables.Table):
    """Table for displaying ColumnTransformRule objects inline on the profile detail page."""

    source_column = tables.Column()
    pattern = tables.Column()
    group_1_target = tables.Column()
    group_2_target = tables.Column()
    actions = _mapping_actions_column("columntransformrule")

    class Meta:
        model = ColumnTransformRule
        fields = ("source_column", "pattern", "group_1_target", "group_2_target", "actions")
