# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Every form the private-coordinator rules must catch, and the forms they must not."""

from netbox_data_import.import_engine import ImportEngine
from netbox_data_import import import_engine as ie


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
    # ruleid: nbdi-tests-use-public-coordinator-direct
    return getattr(ImportEngine, "_private_helper")


def public_getattr_is_fine():
    # ok: nbdi-tests-use-public-coordinator-direct
    return getattr(ImportEngine, "plan")
