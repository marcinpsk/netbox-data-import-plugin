# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
import django_tables2 as tables
from netbox.tables import NetBoxTable, columns
from .models import ImportProfile, ColumnMapping, ClassRoleMapping, DeviceTypeMapping, ColumnTransformRule


class ImportProfileTable(NetBoxTable):
    """Table for listing ImportProfile objects."""

    name = tables.Column(linkify=True)
    sheet_name = tables.Column()
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
            "sheet_name",
            "update_existing",
            "create_missing_device_types",
            "column_mappings",
            "class_role_mappings",
            "device_type_mappings",
            "actions",
        )
        default_columns = (
            "name",
            "sheet_name",
            "column_mappings",
            "class_role_mappings",
            "device_type_mappings",
            "actions",
        )


class ColumnMappingTable(tables.Table):
    """Table for displaying ColumnMapping objects inline on the profile detail page."""

    source_column = tables.Column()
    target_field = tables.Column()
    actions = tables.TemplateColumn(
        template_code="""
        <a href="{% url 'plugins:netbox_data_import:columnmapping_edit' record.pk %}" class="btn btn-sm btn-warning">
            <i class="mdi mdi-pencil"></i>
        </a>
        <a href="{% url 'plugins:netbox_data_import:columnmapping_delete' record.pk %}" class="btn btn-sm btn-danger">
            <i class="mdi mdi-trash-can-outline"></i>
        </a>
        """,
        verbose_name="",
        orderable=False,
    )

    class Meta:
        model = ColumnMapping
        fields = ("source_column", "target_field", "actions")


class ClassRoleMappingTable(tables.Table):
    """Table for displaying ClassRoleMapping objects inline on the profile detail page."""

    source_class = tables.Column()
    creates_rack = tables.BooleanColumn()
    role_slug = tables.Column()
    ignore = tables.BooleanColumn()
    actions = tables.TemplateColumn(
        template_code="""
        <a href="{% url 'plugins:netbox_data_import:classrolemapping_edit' record.pk %}" class="btn btn-sm btn-warning">
            <i class="mdi mdi-pencil"></i>
        </a>
        <a href="{% url 'plugins:netbox_data_import:classrolemapping_delete' record.pk %}" class="btn btn-sm btn-danger">
            <i class="mdi mdi-trash-can-outline"></i>
        </a>
        """,
        verbose_name="",
        orderable=False,
    )

    class Meta:
        model = ClassRoleMapping
        fields = ("source_class", "creates_rack", "role_slug", "ignore", "actions")


class DeviceTypeMappingTable(tables.Table):
    """Table for displaying DeviceTypeMapping objects inline on the profile detail page."""

    source_make = tables.Column()
    source_model = tables.Column()
    netbox_manufacturer_slug = tables.Column()
    netbox_device_type_slug = tables.Column()
    actions = tables.TemplateColumn(
        template_code="""
        <a href="{% url 'plugins:netbox_data_import:devicetypemapping_edit' record.pk %}" class="btn btn-sm btn-warning">
            <i class="mdi mdi-pencil"></i>
        </a>
        <a href="{% url 'plugins:netbox_data_import:devicetypemapping_delete' record.pk %}" class="btn btn-sm btn-danger">
            <i class="mdi mdi-trash-can-outline"></i>
        </a>
        """,
        verbose_name="",
        orderable=False,
    )

    class Meta:
        model = DeviceTypeMapping
        fields = (
            "source_make",
            "source_model",
            "netbox_manufacturer_slug",
            "netbox_device_type_slug",
            "actions",
        )


class ColumnTransformRuleTable(tables.Table):
    """Table for displaying ColumnTransformRule objects inline on the profile detail page."""

    source_column = tables.Column()
    pattern = tables.Column()
    group_1_target = tables.Column()
    group_2_target = tables.Column()
    actions = tables.TemplateColumn(
        template_code="""
        <a href="{% url 'plugins:netbox_data_import:columntransformrule_edit' record.pk %}" class="btn btn-sm btn-warning">
            <i class="mdi mdi-pencil"></i>
        </a>
        <a href="{% url 'plugins:netbox_data_import:columntransformrule_delete' record.pk %}" class="btn btn-sm btn-danger">
            <i class="mdi mdi-trash-can-outline"></i>
        </a>
        """,
        verbose_name="",
        orderable=False,
    )

    class Meta:
        model = ColumnTransformRule
        fields = ("source_column", "pattern", "group_1_target", "group_2_target", "actions")
