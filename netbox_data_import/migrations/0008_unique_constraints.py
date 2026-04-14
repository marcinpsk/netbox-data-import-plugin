# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2025 Marcin Zieba <marcinpsk@gmail.com>
"""Replace deprecated unique_together with UniqueConstraint on all models."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_data_import", "0007_manufacturermapping"),
    ]

    operations = [
        # ColumnMapping
        migrations.AlterUniqueTogether(
            name="columnmapping",
            unique_together=set(),
        ),
        migrations.AddConstraint(
            model_name="columnmapping",
            constraint=models.UniqueConstraint(
                fields=["profile", "target_field"], name="ndi_columnmapping_profile_target"
            ),
        ),
        # ClassRoleMapping
        migrations.AlterUniqueTogether(
            name="classrolemapping",
            unique_together=set(),
        ),
        migrations.AddConstraint(
            model_name="classrolemapping",
            constraint=models.UniqueConstraint(
                fields=["profile", "source_class"], name="ndi_classrolemapping_profile_class"
            ),
        ),
        # DeviceTypeMapping
        migrations.AlterUniqueTogether(
            name="devicetypemapping",
            unique_together=set(),
        ),
        migrations.AddConstraint(
            model_name="devicetypemapping",
            constraint=models.UniqueConstraint(
                fields=["profile", "source_make", "source_model"], name="ndi_dtm_profile_make_model"
            ),
        ),
        # ManufacturerMapping
        migrations.AlterUniqueTogether(
            name="manufacturermapping",
            unique_together=set(),
        ),
        migrations.AddConstraint(
            model_name="manufacturermapping",
            constraint=models.UniqueConstraint(fields=["profile", "source_make"], name="ndi_mfgmapping_profile_make"),
        ),
        # IgnoredDevice
        migrations.AlterUniqueTogether(
            name="ignoreddevice",
            unique_together=set(),
        ),
        migrations.AddConstraint(
            model_name="ignoreddevice",
            constraint=models.UniqueConstraint(fields=["profile", "source_id"], name="ndi_ignoreddevice_profile_srcid"),
        ),
        # ColumnTransformRule
        migrations.AlterUniqueTogether(
            name="columntransformrule",
            unique_together=set(),
        ),
        migrations.AddConstraint(
            model_name="columntransformrule",
            constraint=models.UniqueConstraint(fields=["profile", "source_column"], name="ndi_ctr_profile_column"),
        ),
        # SourceResolution
        migrations.AlterUniqueTogether(
            name="sourceresolution",
            unique_together=set(),
        ),
        migrations.AddConstraint(
            model_name="sourceresolution",
            constraint=models.UniqueConstraint(
                fields=["profile", "source_id", "source_column"], name="ndi_srcresolution_profile_id_col"
            ),
        ),
        # DeviceExistingMatch
        migrations.AlterUniqueTogether(
            name="deviceexistingmatch",
            unique_together=set(),
        ),
        migrations.AddConstraint(
            model_name="deviceexistingmatch",
            constraint=models.UniqueConstraint(fields=["profile", "source_id"], name="ndi_devicematch_profile_srcid"),
        ),
    ]
