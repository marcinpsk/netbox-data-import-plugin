# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""The upgrade removes accepted plans stored in native Job records."""

import uuid

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


APP = "netbox_data_import"
BEFORE = (APP, "0038_cable_tag_integrity")
AFTER = (APP, "0039_remove_job_plan_copies")


class JobPlanCleanupMigrationTest(TransactionTestCase):
    """Remove exposed plans from import Jobs without changing other Job data."""

    def test_upgrade_removes_only_import_job_plan_copies(self):
        executor = MigrationExecutor(connection)
        self.addCleanup(lambda: MigrationExecutor(connection).migrate([AFTER], fake=True))
        executor.migrate([BEFORE], fake=True)
        Job = executor.loader.project_state([BEFORE]).apps.get_model("core", "Job")

        exposed = Job.objects.create(
            name="Data Import",
            job_id=uuid.uuid4(),
            data={
                "job_type": "netbox_data_import.import",
                "phase": "failed",
                "accepted_plan": {"policy": {"cable_type": "mmf-om4"}},
                "source_document_id": 17,
            },
        )
        unrelated = Job.objects.create(
            name="Other Job",
            job_id=uuid.uuid4(),
            data={"job_type": "other.job", "accepted_plan": {"keep": True}},
        )
        clean = Job.objects.create(
            name="Clean Import",
            job_id=uuid.uuid4(),
            data={"job_type": "netbox_data_import.import", "phase": "completed"},
        )

        MigrationExecutor(connection).migrate([AFTER])
        exposed.refresh_from_db()
        unrelated.refresh_from_db()
        clean.refresh_from_db()

        self.assertEqual(
            exposed.data,
            {"job_type": "netbox_data_import.import", "phase": "failed", "source_document_id": 17},
        )
        self.assertEqual(unrelated.data, {"job_type": "other.job", "accepted_plan": {"keep": True}})
        self.assertEqual(clean.data, {"job_type": "netbox_data_import.import", "phase": "completed"})
