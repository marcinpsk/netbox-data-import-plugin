# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Import Profile policy is projected into plain Source Adapter configuration."""

from django.test import TestCase

from netbox_data_import.flat_workbook import FlatWorkbookConfig, TransformRule
from netbox_data_import.models import ColumnMapping, ColumnTransformRule, ImportProfile


class AdapterConfigProjectionTest(TestCase):
    """The projection seam owns every ORM read needed before source interpretation."""

    def test_flat_workbook_policy_becomes_plain_interpreter_configuration(self):
        """Mappings, transforms, and scalar settings cross the seam as detached values."""
        from netbox_data_import.adapter_config import interpreter_config_for

        profile = ImportProfile.objects.create(
            name="Adapter Config Projection",
            adapter_config={"sheet_name": "Inventory", "capture_extra_data": True},
        )
        ColumnMapping.objects.create(profile=profile, source_column="Primary ID", target_field="source_id")
        ColumnMapping.objects.create(profile=profile, source_column="Fallback ID", target_field="source_id")
        ColumnTransformRule.objects.create(
            profile=profile,
            source_column="Placement",
            pattern=r"(.+)/U(\d+)",
            group_1_target="rack_name",
            group_2_target="u_position",
        )

        config = interpreter_config_for(profile)

        self.assertEqual(
            config,
            FlatWorkbookConfig(
                sheet_name="Inventory",
                column_map={"source_id": ("Primary ID", "Fallback ID")},
                transform_rules=(
                    TransformRule(
                        source_column="Placement",
                        pattern=r"(.+)/U(\d+)",
                        group_1_target="rack_name",
                        group_2_target="u_position",
                    ),
                ),
                capture_extra_data=True,
            ),
        )
