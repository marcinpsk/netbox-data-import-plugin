# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Enforce the AGENTS.md model-to-base rule."""

import inspect

from django import forms
from django.db import models
from django.test import SimpleTestCase
from netbox.api.serializers import NetBoxModelSerializer
from netbox.forms import NetBoxModelForm, NetBoxModelImportForm
from netbox.models import NetBoxModel
from rest_framework.serializers import ModelSerializer

from netbox_data_import import forms as plugin_forms
from netbox_data_import.api import serializers as plugin_serializers


def _classes_with_models(module, base):
    """Yield locally declared subclasses that specify a model."""
    for _name, candidate in inspect.getmembers(module, inspect.isclass):
        if candidate.__module__ != module.__name__ or not issubclass(candidate, base):
            continue
        model = getattr(getattr(candidate, "Meta", None), "model", None)
        if model is not None:
            yield candidate, model


class ModelBaseConventionTest(SimpleTestCase):
    """Match each form and serializer base to its model family."""

    def test_model_forms_and_serializers_match_their_model_family(self):
        self.assertFalse(issubclass(plugin_serializers.PolicySectionSerializer, NetBoxModelSerializer))
        configurations = (
            (plugin_forms, forms.ModelForm, (NetBoxModelForm, NetBoxModelImportForm)),
            (plugin_serializers, ModelSerializer, (NetBoxModelSerializer,)),
        )

        for module, framework_base, netbox_bases in configurations:
            for candidate, model in _classes_with_models(module, framework_base):
                with self.subTest(class_name=candidate.__name__, model=model._meta.label):
                    self.assertTrue(issubclass(model, models.Model))
                    self.assertEqual(
                        issubclass(candidate, netbox_bases),
                        issubclass(model, NetBoxModel),
                    )
