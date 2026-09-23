# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Every call shape the rules must catch, and the shapes they must not."""

import os
import signal
import sys
import unittest.mock as mock

from signal import SIGALRM, SIG_DFL, SIG_IGN

from unittest.mock import patch

from netbox_data_import.import_engine import ImportEngine
from netbox_data_import.inference_transport import request_to_resolved_address
from netbox_data_import import import_engine as ie
from netbox_data_import.import_engine import ImportEngine as Coordinator


def job_data_exposes_accepted_plan(job, plan):
    # ruleid: nbdi-job-data-excludes-accepted-plan
    job.data = {"phase": "queued", "accepted_plan": plan}


def job_data_key_exposes_accepted_plan(job, plan):
    # ruleid: nbdi-job-data-excludes-accepted-plan
    job.data["accepted_plan"] = plan


def queued_worker_receives_accepted_plan(runner, plan):
    # ok: nbdi-job-data-excludes-accepted-plan
    return runner.enqueue(accepted_plan=plan)


def job_data_keeps_progress_only(job):
    # ok: nbdi-job-data-excludes-accepted-plan
    job.data = {"phase": "queued", "processed": 0}


def caught_today_class_attr():
    # ruleid: nbdi-tests-use-public-coordinator
    return ImportEngine._private_helper


def caught_today_alias():
    Engine = ImportEngine
    # ruleid: nbdi-tests-use-public-coordinator
    return Engine._private_helper


def missed_constructor_assign():
    engine = ImportEngine()
    # ruleid: nbdi-tests-use-public-coordinator
    return engine._private_helper


def missed_inline_construct():
    # ruleid: nbdi-tests-use-public-coordinator-direct
    return ImportEngine()._private_helper()


def missed_attribute_target(self):
    self.engine = ImportEngine()
    # ruleid: nbdi-tests-use-public-coordinator
    return self.engine._private_helper


def missed_tuple_target():
    engine, other = ImportEngine(), None
    # ruleid: nbdi-tests-use-public-coordinator
    return engine._private_helper


def missed_walrus():
    # ruleid: nbdi-tests-use-public-coordinator
    return (engine := ImportEngine())._private_helper


def missed_dunder_bypass():
    # ruleid: nbdi-tests-use-public-coordinator
    return ImportEngine.__dict__["_failure_reason"]


def public_use_is_fine():
    # ok: nbdi-tests-use-public-coordinator
    return ImportEngine.plan(None, None, None, None)


def unrelated_private_is_fine():
    other = object()
    # ok: nbdi-tests-use-public-coordinator
    return other._something


def same_name_other_scope_is_fine():
    engine = object()
    # ok: nbdi-tests-use-public-coordinator
    return engine._private_helper


def missed_getattr():
    # ruleid: nbdi-tests-use-public-coordinator
    return getattr(ImportEngine, "_private_helper")


def public_getattr_is_fine():
    # ok: nbdi-tests-use-public-coordinator
    return getattr(ImportEngine, "plan")


def missed_module_alias_private():
    # ruleid: nbdi-tests-use-public-coordinator-direct
    return ie._private_helper


def missed_module_alias_class_private():
    # ruleid: nbdi-tests-use-public-coordinator
    return ie.ImportEngine._private_helper


def missed_module_alias_getattr():
    # ruleid: nbdi-tests-use-public-coordinator
    return getattr(ie.ImportEngine, "_private_helper")


def missed_class_as_import():
    # ruleid: nbdi-tests-use-public-coordinator
    return Coordinator._private_helper


def module_alias_public_is_fine():
    # ok: nbdi-tests-use-public-coordinator-direct
    return ie.ImportEngine.plan(None, None, None, None)


def class_as_import_public_is_fine():
    # ok: nbdi-tests-use-public-coordinator
    return Coordinator.plan(None, None, None, None)


def missed_bound_instance_getattr():
    engine = ImportEngine()
    # ruleid: nbdi-tests-use-public-coordinator
    return getattr(engine, "_private_helper")


def missed_bound_class_getattr():
    Engine = ImportEngine
    # ruleid: nbdi-tests-use-public-coordinator
    return getattr(Engine, "_private_helper")


def bound_public_getattr_is_fine():
    engine = ImportEngine()
    # ok: nbdi-tests-use-public-coordinator
    return getattr(engine, "plan")


def missed_module_getattr():
    # ruleid: nbdi-tests-use-public-coordinator-direct
    return getattr(ie, "_private_helper")


def module_public_getattr_is_fine():
    # ok: nbdi-tests-use-public-coordinator-direct
    return getattr(ie, "plan")


def missed_unbounded_read(session, url, address):
    # ruleid: nbdi-bounded-response-body
    return request_to_resolved_address(session, "GET", url, address)


def missed_unbounded_read_with_other_keywords(session, url, address, deadline):
    # ruleid: nbdi-bounded-response-body
    return request_to_resolved_address(session, "GET", url, address, deadline=deadline, allow_redirects=False)


def bounded_read_is_fine(session, url, address):
    # ok: nbdi-bounded-response-body
    return request_to_resolved_address(session, "GET", url, address, response_body_limit=1024)


def bounded_read_by_position_is_fine(session, url, address):
    # ok: nbdi-bounded-response-body
    return request_to_resolved_address(session, "GET", url, address, 1024)


def missed_explicit_none_keyword(session, url, address):
    # ruleid: nbdi-bounded-response-body
    return request_to_resolved_address(session, "GET", url, address, response_body_limit=None)


def missed_explicit_none_by_position(session, url, address):
    # ruleid: nbdi-bounded-response-body
    return request_to_resolved_address(session, "GET", url, address, None)


def missed_patched_stdin():
    # ruleid: nbdi-no-interpreter-stream-patch
    return patch.object(sys, "stdin", None)


def missed_patched_stdout():
    # ruleid: nbdi-no-interpreter-stream-patch
    return patch.object(sys, "stdout", None)


def missed_patched_stdout_by_name():
    # ruleid: nbdi-no-interpreter-stream-patch
    return patch("sys.stdout", None)


def missed_patched_stderr_through_the_module():
    # ruleid: nbdi-no-interpreter-stream-patch
    return mock.patch.object(sys, "stderr", None)


def patching_the_environment_is_fine():
    # ok: nbdi-no-interpreter-stream-patch
    return patch.dict(os.environ, {"VAULT_TOKEN": "secret"})


def missed_alarm_handler():
    # ruleid: nbdi-deadline-alarm-keeps-its-default-action
    signal.signal(signal.SIGALRM, lambda number, frame: None)


def arming_the_alarm_is_fine():
    # ok: nbdi-deadline-alarm-keeps-its-default-action
    signal.setitimer(signal.ITIMER_REAL, 1.0)


def handling_another_signal_is_fine():
    # ok: nbdi-deadline-alarm-keeps-its-default-action
    signal.signal(signal.SIGTERM, lambda number, frame: None)


def missed_alarm_handler_from_the_imported_constant():
    # ruleid: nbdi-deadline-alarm-keeps-its-default-action
    signal.signal(SIGALRM, lambda number, frame: None)


def ignoring_the_alarm_is_still_refused():
    # ruleid: nbdi-deadline-alarm-keeps-its-default-action
    signal.signal(signal.SIGALRM, signal.SIG_IGN)


def ignoring_the_alarm_by_imported_constant_is_still_refused():
    # ruleid: nbdi-deadline-alarm-keeps-its-default-action
    signal.signal(SIGALRM, SIG_IGN)


def restoring_the_default_action_is_fine():
    # ok: nbdi-deadline-alarm-keeps-its-default-action
    signal.signal(signal.SIGALRM, signal.SIG_DFL)


def restoring_the_default_action_by_imported_constant_is_fine():
    # ok: nbdi-deadline-alarm-keeps-its-default-action
    signal.signal(SIGALRM, SIG_DFL)
