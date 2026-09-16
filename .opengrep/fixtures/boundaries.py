# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Every form the private-coordinator rules must catch, and the forms they must not."""

from netbox_data_import.import_engine import ImportEngine
from netbox_data_import import import_engine as ie
from netbox_data_import.import_engine import ImportEngine as Coordinator


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
