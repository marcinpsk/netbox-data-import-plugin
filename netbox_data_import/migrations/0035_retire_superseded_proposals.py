# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>

from django.db import migrations

#: The contract versions this release sends. A stored request below either one cannot be answered.
PROMPT_VERSION = 2
RESPONSE_SCHEMA_VERSION = 2


def retire_superseded_proposals(apps, schema_editor):
    """Fail every queued or running proposal whose stored request predates this release."""
    ResolutionProposal = apps.get_model("netbox_data_import", "ResolutionProposal")

    ResolutionProposal.objects.filter(status__in=("queued", "running")).exclude(
        prompt_version=PROMPT_VERSION, response_schema_version=RESPONSE_SCHEMA_VERSION
    ).update(status="failed", failure_reason="superseded_request")


class Migration(migrations.Migration):
    dependencies = [
        ("netbox_data_import", "0034_tracedeviceresolution"),
    ]

    operations = [
        # No reverse callable: the request the row carried cannot be sent under either contract.
        migrations.RunPython(retire_superseded_proposals),
    ]
