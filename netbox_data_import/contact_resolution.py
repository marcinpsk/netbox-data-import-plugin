# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Marcin Zieba <marcinpsk@gmail.com>
"""Resolve, review, and apply one primary Contact decision."""

from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import connection, transaction
from django.db.models import Q

from .models import stored_import_source, validate_contact_candidate_resolution
from .object_permissions import ObjectPermissionDenied, enforce_saved_object_permission


def _text(value) -> str:
    """Return a stripped string for one optional source value."""
    if value is None:
        return ""
    return str(value).strip()


# `name` is proposed from its header, because any text is a valid name. The `email` and `phone`
# entries only keep a column that names a different field out of the name proposal.
_ROLE_HEADER_HINTS = {
    "email": ("email", "mail"),
    "phone": ("phone", "number", "tel", "mobile", "cell"),
    "name": ("name", "contact", "person"),
}
_PHONE_PUNCTUATION = re.compile(r"[\s()\-./]")
_PHONE_SHAPE = re.compile(r"\+?\d{7,}")


def _looks_like_email(value: str) -> bool:
    """Determine whether a value has a valid email address format.
    
    Parameters:
        value (str): The value to validate.
    
    Returns:
        bool: `true` if the value is a valid email address, `false` otherwise.
    """
    try:
        validate_email(value)
    except ValidationError:
        return False
    return True


def _looks_like_phone(value: str) -> bool:
    """Determine whether a value has the expected shape of a phone number.
    
    Parameters:
    	value (str): The value to evaluate.
    
    Returns:
    	bool: `true` if the value matches the expected phone-number shape, `false` otherwise.
    """
    return bool(_PHONE_SHAPE.fullmatch(_PHONE_PUNCTUATION.sub("", value)))


def _header_hints_at(source_column: str, role: str) -> bool:
    """Determine whether a source column name contains a header hint for the specified contact role.
    
    Parameters:
    	source_column (str): Source column name to inspect.
    	role (str): Contact role whose header hints should be checked.
    
    Returns:
    	bool: `True` if the column name contains a matching hint, `False` otherwise.
    """
    lowered = source_column.lower()
    return any(hint in lowered for hint in _ROLE_HEADER_HINTS[role])


def suggest_contact_roles(candidate_values: dict[str, str]) -> dict[str, str]:
    """Map each Contact field to the candidate column that most likely supplies it.

    Returns ``{role: source_column}`` for the roles it can recognize, and leaves out the rest
    so the operator decides. The value shape settles ``email`` and ``phone``. Only a header
    keyword settles ``name``, because any text is a valid name and an organization looks the
    same as a person.
    """
    by_shape = {
        "email": [column for column, value in candidate_values.items() if _looks_like_email(value)],
        "phone": [column for column, value in candidate_values.items() if _looks_like_phone(value)],
    }
    suggestions = {}
    for role, columns in by_shape.items():
        # A header says which field a value can feed, never which of several same-shaped values
        # is the right one: `Backup Email` carries the keyword that `Primary Contact` does not.
        # So a second value of the same shape means the operator decides, because the collapsed
        # modal turns a proposal into a one-click save.
        if len(columns) == 1:
            suggestions[role] = columns[0]

    recognized = set(by_shape["email"]) | set(by_shape["phone"])
    claimed = set(suggestions.values())
    # A header that says "email" or "phone" holds a malformed value of that type, never a name.
    named = [
        column
        for column in candidate_values
        if column not in claimed
        and column not in recognized
        and _header_hints_at(column, "name")
        and not _header_hints_at(column, "email")
        and not _header_hints_at(column, "phone")
    ]
    if len(named) == 1:
        suggestions["name"] = named[0]
    return suggestions


@dataclass(frozen=True)
class ContactSelection:
    """The resolved Contact values and optional selected NetBox identity."""

    values: dict[str, str]
    contact_id: int | None = None


@dataclass(frozen=True)
class ContactReview:
    """A read-only Contact decision shared by preview and write paths."""

    selection: ContactSelection | None
    extra_columns: dict
    plan: dict | None
    candidate_values: dict[str, str]
    suggestion: dict | None


class ContactResolutionRequired(ValidationError):
    """Require an operator decision for ambiguous or invalid Contact values."""

    candidate_target = "contact"

    def __init__(self, candidate_values: dict[str, str], message: str | None = None, suggestion=None):
        self.candidate_values = candidate_values
        self.suggestion = suggestion
        super().__init__(
            {
                "contact": message
                or "Select which candidate values supply Contact fields, enter Contact details, or select no contact."
            }
        )


class PrimaryContactResolver:
    """Hide Contact resolution, lookup, assignment, and JSON migration behind one interface."""

    @staticmethod
    def _candidate_source_columns(profile) -> dict[str, frozenset[str]]:
        grouped = {}
        for mapping in profile.column_mappings.filter(target_field__startswith="candidate:"):
            target = mapping.target_field.removeprefix("candidate:")
            grouped.setdefault(target, set()).add(mapping.source_column)
        return {target: frozenset(columns) for target, columns in grouped.items()}

    @staticmethod
    def _candidate_values(row, candidate_source_columns, extra_columns) -> dict[str, str]:
        row_values = row.get("_candidate_values", {}).get("contact", {})
        values = (
            {str(source): _text(value) for source, value in row_values.items() if _text(value)}
            if isinstance(row_values, dict)
            else {}
        )
        for source_column in candidate_source_columns.get("contact", ()):
            value = _text(extra_columns.pop(source_column, ""))
            if value and source_column not in values:
                values[source_column] = value
        return values

    @classmethod
    def _selection(cls, row, profile, candidate_values, legacy_primary_contact) -> ContactSelection | None:
        """
        Resolve contact candidate values into a contact selection.
        
        Parameters:
            row: Import row containing a previously applied contact resolution.
            profile: Import profile providing contact lookup configuration.
            candidate_values: Candidate contact values available for resolution.
            legacy_primary_contact: Legacy primary contact value, if present.
        
        Returns:
            A resolved ContactSelection, or None when no contact data is available.
        
        Raises:
            ContactResolutionRequired: If candidate values exist without a resolved selection.
        """
        if row.get("contact_resolution_applied") is True:
            normalized = validate_contact_candidate_resolution(
                {
                    "contact_resolution_applied": True,
                    "contact_field_sources": row.get("contact_field_sources", {}),
                    "contact_field_values": row.get("contact_field_values", {}),
                    "contact_id": row.get("contact_id"),
                },
                profile.adapter_settings.primary_contact_lookup_field,
                candidate_values,
            )
            values = dict(normalized["field_values"])
            for field_name, source_column in normalized["field_sources"].items():
                values[field_name] = candidate_values[source_column]
            if not values and normalized["contact_id"] is None:
                return None
            return ContactSelection(values=values, contact_id=normalized["contact_id"])

        if legacy_primary_contact:
            candidate_values.setdefault("Legacy primary contact", legacy_primary_contact)
            return ContactSelection(
                values={
                    "name": legacy_primary_contact,
                    profile.adapter_settings.primary_contact_lookup_field: legacy_primary_contact,
                }
            )
        if not candidate_values:
            return None
        raise ContactResolutionRequired(candidate_values)

    @classmethod
    def review(
        cls,
        obj,
        row: dict,
        profile,
        user=None,
        *,
        candidate_source_columns: dict[str, frozenset[str]] | None = None,
    ) -> ContactReview:
        """Return the effective Contact plan without writing database state."""
        extra_columns = {}
        import_source = stored_import_source(obj)
        if import_source is not None and isinstance(import_source.extra_columns, dict):
            extra_columns.update(import_source.extra_columns)
        row_extra = row.get("_extra_columns")
        if isinstance(row_extra, dict):
            extra_columns.update(row_extra)

        legacy_primary_contact = _text(row.get("primary_contact")) or _text(extra_columns.get("primary_contact"))
        extra_columns.pop("primary_contact", None)
        source_columns = candidate_source_columns or cls._candidate_source_columns(profile)
        candidate_values = cls._candidate_values(row, source_columns, extra_columns)
        try:
            selection = cls._selection(row, profile, candidate_values, legacy_primary_contact)
            plan = cls._plan(obj, profile, selection, user)
        except ContactResolutionRequired as exc:
            exc.suggestion = cls.suggest(candidate_values, profile, user)
            raise
        except ValidationError as exc:
            if not candidate_values:
                raise
            suggestion = cls.suggest(candidate_values, profile, user)
            raise ContactResolutionRequired(candidate_values, "; ".join(exc.messages), suggestion) from exc

        suggestion = cls._suggestion_from_plan(plan)
        return ContactReview(selection, extra_columns, plan, candidate_values, suggestion)

    @staticmethod
    def _suggestion_from_plan(plan) -> dict | None:
        if not plan or plan["contact_action"] != "reuse":
            return None
        return {
            "id": plan["contact_id"],
            "name": plan["contact_name"],
            "email": plan["contact_email"],
            "phone": plan["contact_phone"],
        }

    @classmethod
    def suggest(cls, candidate_values: dict[str, str], profile, user=None) -> dict | None:
        """
        Find a visible Contact matching exactly one configured identity value from the row.
        
        Returns:
            dict | None: The Contact's ID, name, email, and phone when exactly one match
            is found; otherwise, `None`.
        """
        from tenancy.models import Contact

        lookup_field = profile.adapter_settings.primary_contact_lookup_field
        values = []
        for candidate in candidate_values.values():
            value = _text(candidate)
            if not value:
                continue
            if lookup_field == "email":
                try:
                    validate_email(value)
                except ValidationError:
                    continue
            values.append(value)
        if not values:
            return None

        query = Q()
        for value in values:
            query |= Q(**{f"{lookup_field}__iexact": value})
        contacts = Contact.objects.filter(query)
        if user is not None:
            contacts = contacts.restrict(user, "view")
        matches = list(contacts.order_by("pk")[:2])
        if len(matches) != 1:
            return None
        contact = matches[0]
        return {
            "id": contact.pk,
            "name": contact.name,
            "email": contact.email,
            "phone": contact.phone,
        }

    @classmethod
    def _plan_assignment(cls, obj, role, contact, user, lock):
        """
        Determine the assignment action needed to make a Contact primary for an object and role.
        
        Parameters:
            obj: Object whose Contact assignment is being planned.
            role: Contact role to evaluate.
            contact: Contact to assign or promote.
            user: User whose permissions are checked.
            lock: Whether assignment queries should lock matching rows.
        
        Returns:
            A tuple containing the existing primary assignment, the matching assignment for
            the selected Contact, and the action to apply.
        """
        from tenancy.models import ContactAssignment

        if obj is None:
            if user is not None and not user.has_perm("tenancy.add_contactassignment"):
                raise ObjectPermissionDenied("tenancy.add_contactassignment")
            return None, None, "create"

        scope = {
            "object_type": ContentType.objects.get_for_model(obj),
            "object_id": obj.pk,
            "role": role,
        }
        assignments = ContactAssignment.objects.select_for_update() if lock else ContactAssignment.objects
        primary_assignments = list(assignments.filter(**scope, priority="primary")[:2])
        if len(primary_assignments) > 1:
            raise ValidationError(
                {"primary_contact": "More than one primary assignment exists for the selected contact role."}
            )
        primary_assignment = primary_assignments[0] if primary_assignments else None
        assignment = assignments.filter(**scope, contact=contact).first() if contact.pk is not None else None

        if primary_assignment is not None and primary_assignment.contact_id != contact.pk:
            enforce_saved_object_permission(primary_assignment, user, "change")
            if assignment is None:
                return primary_assignment, None, "replace"
            enforce_saved_object_permission(assignment, user, "change")
            return primary_assignment, assignment, "demote_and_promote"
        if assignment is None:
            if user is not None and not user.has_perm("tenancy.add_contactassignment"):
                raise ObjectPermissionDenied("tenancy.add_contactassignment")
            return primary_assignment, None, "create"
        if assignment.priority != "primary":
            enforce_saved_object_permission(assignment, user, "change")
            return primary_assignment, assignment, "promote"
        return primary_assignment, assignment, "unchanged"

    @classmethod
    def _plan(cls, obj, profile, selection: ContactSelection | None, user=None, lock=False) -> dict | None:
        """
        Build a validated plan for creating or reusing a Contact and assigning it as the primary Contact.
        
        Parameters:
        	selection (ContactSelection | None): The selected or proposed Contact details.
        	user: The user whose permissions apply to Contact access and creation.
        	lock (bool): Whether to lock relevant records while building the plan.
        
        Returns:
        	dict | None: The Contact and assignment actions to apply, or `None` when no Contact is selected.
        
        Raises:
        	ValidationError: If the configured role, selected Contact, lookup value, or assignment data is invalid.
        	ObjectPermissionDenied: If the user lacks permission to view or create the required Contact.
        """
        if selection is None:
            return None
        role_name = profile.adapter_settings.primary_contact_role
        if not role_name:
            raise ValidationError({"primary_contact": "Select a primary contact role on the import profile."})
        # The profile memoizes the role, and one instance serves both review and apply. Re-read it
        # under the apply lock so a role deleted in between is refused here, not by the FK check.
        if lock:
            from tenancy.models import ContactRole

            role = ContactRole.objects.select_for_update().filter(name=role_name).first()
        else:
            role = profile.resolved_primary_contact_role
        if role is None:
            raise ValidationError(
                {
                    "primary_contact": f"The import profile references Contact Role '{role_name}', which no longer exists."
                }
            )

        from tenancy.models import Contact

        lookup_field = profile.adapter_settings.primary_contact_lookup_field
        contact_queryset = Contact.objects.select_for_update() if lock else Contact.objects
        if selection.contact_id is not None:
            contact = contact_queryset.filter(pk=selection.contact_id).first()
            if contact is None:
                raise ValidationError({"primary_contact": "The selected NetBox Contact no longer exists."})
            if user is not None and not Contact.objects.restrict(user, "view").filter(pk=contact.pk).exists():
                raise ObjectPermissionDenied("tenancy.view_contact")
            selected_lookup = _text(selection.values.get(lookup_field))
            current_lookup = _text(getattr(contact, lookup_field))
            if selected_lookup and selected_lookup.casefold() != current_lookup.casefold():
                raise ValidationError(
                    {"primary_contact": f"The selected Contact no longer has the chosen {lookup_field} value."}
                )
            contact_values = {
                "name": contact.name,
                "email": contact.email,
                "phone": contact.phone,
            }
        else:
            contact_values = selection.values
            value = _text(contact_values.get(lookup_field))
            if not value:
                raise ValidationError(
                    {"primary_contact": f"Select or enter a value for the Contact {lookup_field} lookup field."}
                )
            if lookup_field == "email":
                validate_email(value)
            proposed_contact = Contact(**contact_values)
            proposed_contact.full_clean()
            contacts = list(contact_queryset.filter(**{f"{lookup_field}__iexact": value})[:2])
            if len(contacts) > 1:
                raise ValidationError(
                    {"primary_contact": f"More than one contact has the {lookup_field} value '{value}'."}
                )
            if contacts:
                contact = contacts[0]
                if user is not None and not Contact.objects.restrict(user, "view").filter(pk=contact.pk).exists():
                    raise ObjectPermissionDenied("tenancy.view_contact")
            else:
                if user is not None and not user.has_perm("tenancy.add_contact"):
                    raise ObjectPermissionDenied("tenancy.add_contact")
                contact = proposed_contact

        primary_assignment, assignment, assignment_action = cls._plan_assignment(obj, role, contact, user, lock)
        return {
            "lookup_field": lookup_field,
            "value": _text(getattr(contact, lookup_field, "")) or _text(contact_values.get(lookup_field)),
            "contact_values": contact_values,
            "role_id": role.pk,
            "contact_id": contact.pk,
            "contact_name": contact.name,
            "contact_email": contact.email,
            "contact_phone": contact.phone,
            "contact_action": "reuse" if contact.pk is not None else "create",
            "primary_assignment_id": primary_assignment.pk if primary_assignment is not None else None,
            "assignment_id": assignment.pk if assignment is not None else None,
            "assignment_action": assignment_action,
        }

    @classmethod
    def apply(cls, obj, profile, review: ContactReview, user=None) -> dict | None:
        """Apply one reviewed Contact decision and remove its legacy JSON value."""
        if review.selection is None:
            cls._remove_legacy_json(obj)
            return None

        from tenancy.models import Contact, ContactAssignment

        with transaction.atomic():
            cls.lock_imports()
            plan = cls._plan(obj, profile, review.selection, user, lock=True)
            if plan["contact_id"] is None:
                contact = Contact(**plan["contact_values"])
                contact.full_clean()
                contact.save()
                enforce_saved_object_permission(contact, user, "add")
            else:
                contact = Contact.objects.get(pk=plan["contact_id"])

            scope = {
                "object_type": ContentType.objects.get_for_model(obj),
                "object_id": obj.pk,
                "role_id": plan["role_id"],
            }
            action = plan["assignment_action"]
            assignment = None
            if action == "replace":
                assignment = ContactAssignment.objects.get(pk=plan["primary_assignment_id"])
                assignment.contact = contact
                assignment.full_clean()
                assignment.save(update_fields=["contact"])
                enforce_saved_object_permission(assignment, user, "change")
            elif action == "demote_and_promote":
                previous = ContactAssignment.objects.get(pk=plan["primary_assignment_id"])
                previous.priority = "secondary"
                previous.full_clean()
                previous.save(update_fields=["priority"])
                enforce_saved_object_permission(previous, user, "change")
                assignment = ContactAssignment.objects.get(pk=plan["assignment_id"])
            elif action in ("promote", "unchanged"):
                assignment = ContactAssignment.objects.get(pk=plan["assignment_id"])

            if assignment is None:
                assignment = ContactAssignment(contact=contact, priority="primary", **scope)
                assignment.full_clean()
                assignment.save()
                enforce_saved_object_permission(assignment, user, "add")
            elif assignment.priority != "primary":
                assignment.priority = "primary"
                assignment.full_clean()
                assignment.save(update_fields=["priority"])
                enforce_saved_object_permission(assignment, user, "change")

            cls._remove_legacy_json(obj)
            # atomic-exit-safe: success-commit-intended
            return plan

    @staticmethod
    def _remove_legacy_json(obj) -> None:
        """Drop the legacy primary_contact value once a native Contact assignment holds it."""
        import_source = stored_import_source(obj)
        if import_source is None or "primary_contact" not in import_source.extra_columns:
            return
        extra_columns = deepcopy(import_source.extra_columns)
        extra_columns.pop("primary_contact")
        import_source.extra_columns = extra_columns
        import_source.save(update_fields=["extra_columns"])

    @staticmethod
    def lock_imports() -> None:
        """Serialize import jobs that can create shared Contact identities."""
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ["netbox_data_import.contact_sync"])
