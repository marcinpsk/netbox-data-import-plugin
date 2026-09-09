# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>

import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_data_import", "0031_inferencebackend"),
    ]

    operations = [
        migrations.AlterField(
            model_name="inferencebackend",
            name="connect_timeout",
            field=models.PositiveIntegerField(default=5, validators=[django.core.validators.MinValueValidator(1)]),
        ),
        migrations.AlterField(
            model_name="inferencebackend",
            name="read_timeout",
            field=models.PositiveIntegerField(default=60, validators=[django.core.validators.MinValueValidator(1)]),
        ),
    ]
