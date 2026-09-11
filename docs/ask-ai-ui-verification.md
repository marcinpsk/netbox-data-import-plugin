<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> -->

# Ask AI UI integration: implementation and verification

The existing `trace_proposal` GET now returns `history` beside the latest `proposal` and its
freshness data. It also returns presentation data for the card and history list. This keeps the
field binding and profile access check in one read endpoint. Every history query includes the
profile, task type, and field key. The workspace checks profile view scope separately from preview
access. A user who can preview one profile but view only another receives no proposal history or
active count for the first profile.

`proposal_presentation.py` supplies card labels, field badges, metadata, and action reasons to
`views.py`. It derives the proposal vocabulary from the model choices. It resolves the active
backend once per response. The view decorates copies of the displayed termination dictionaries.
`cable_target.py`, the Import Plan, the models, and the proposal command endpoints are unchanged.

`trace_proposals.js` owns the inline cards. The manual picker remains a separate controller.
The card controller has delegated actions and one initialization guard across htmx navigation.
Each pending card reads its state after 3 seconds. Terminal responses stop its timer. Removing a
card clears its timer and aborts its read. An old response cannot update a replacement card.
Accept uses the existing decision endpoint, refreshes the accepted field, then submits the existing
Re-read form to replan the operator's preview.

## Specification interpretation and existing constraints

Section 10.2 says both "no accept action" for no match and "Every action is always visible."
The UI keeps Accept visible but disabled, with the no-match reason underneath. No usable accept
action exists. The specification is unchanged.

A retained synchronization can prevent an immediate replan. `TraceWorkspaceRereadView.post` refuses
while `_retained_sync_block_reason` is set. The existing acceptance endpoint permits the decision
and marks the preview dirty during that interval. The UI preserves this guard and states that the
resolution was saved and the workspace must be re-read when synchronization finishes. A browser
test proves that it does not submit the disabled replan action.

NetBox evaluates arbitrary object constraints against saved rows. The UI checks the general
resolution permission. The existing acceptance transaction remains responsible for the final
object constraint check. Its real endpoint tests remain in the full suite.

## Validation

The first commit is `15d5b86` (`feat: expose proposal history and workspace action states`).
Its full Python matrix passed 2,275 tests on NetBox 4.7 and 2,265 on NetBox 4.6.10.
The unchanged frontend baseline passed 114 Vitest tests and 76 Playwright tests.

| Final suite | Result |
| --- | --- |
| NetBox 4.7 | 2,276 passed, 524 subtests passed, zero skipped |
| NetBox 4.6.10 | 2,266 passed, 524 subtests passed, zero skipped |
| Vitest | 125 passed |
| Playwright | 85 passed |

Ruff, `ruff format --check`, mypy, and the pre-commit checks passed.
The browser tests use real browser DOM and fetch with controlled HTTP responses. The Django tests
use the real views, database, serializers, permissions, and queue. The theme inspection used HTML
rendered by Django and the actual NetBox stylesheet in Chromium. Both light and dark themes were
inspected. Disabled proposal buttons use theme text and border colors to remain readable.

## Mutation method

Each mutation asserts that its source text exists, replaces one occurrence, and asserts that the
source changed. Each test runs against the mutation. The source is restored in a `finally` block.
All 126 mutations were caught. The tables name the applied mutations and the tests that caught them. The failure output was read
to check the reason. The detached-card mutations change multiple guards because each guard can
independently stop the same invalid request.

The older-field and workspace view-scope regressions also failed before their fixes. Test counts
come from pytest's result summary, not the wrapper exit status. All Python runs use the configured
eight-worker pool and the dedicated database and Redis services for each NetBox version.

## Server presentation and history

| Behavior | Applied mutation | Test that caught it |
| --- | --- | --- |
| history-all | `ResolutionProposalSerializer(history, many=True)` → `ResolutionProposalSerializer(history[:1], many=True)` | `ProposalWorkspaceTest.test_history_returns_every_attempt_newest_first_with_status_and_outcome` |
| history-latest | `"proposal": records[0] if records else None` → `"proposal": records[-1] if records else None` | `ProposalWorkspaceTest.test_history_returns_every_attempt_newest_first_with_status_and_outcome` |
| history-profile | `profile=self.profile, task_type=SELECT_TERMINATION_TASK, field_key__in=histories` → `task_type=SELECT_TERMINATION_TASK, field_key__in=histories` | `ProposalWorkspaceTest.test_history_excludes_other_profiles_and_fields` |
| history-access | `ImportProfile.objects.restrict(request.user, profile_action).filter(pk=context.get("profile_id"))` → `ImportProfile.objects.filter(pk=context.get("profile_id"))` | `ProposalWorkspaceTest.test_history_excludes_other_profiles_and_fields` |
| history-view-only | `"history": records` → `"history": records if self.preview_allowed else []` | `ProposalWorkspaceTest.test_profile_view_only_can_read_history_without_target_access` |
| resolved-reason | `request_reason = "This termination is already resolved."` → `request_reason = ""` | `ProposalWorkspaceTest.test_workspace_supplies_affordances_without_editing_the_plan` |
| no-backend-reason | `self.backend_reason = "No Inference Backend is enabled or configured as a fallback."` → `self.backend_reason = ""` | `ProposalWorkspaceTest.test_workspace_supplies_affordances_without_editing_the_plan` |
| fallback-enabled | `resolve_active_backend()` → `resolve_active_backend() ;             self.backend_reason = "Unavailable"` | `ProposalWorkspaceTest.test_workspace_supplies_affordances_without_editing_the_plan` |
| active-request-reason | `request_reason = "An active proposal already exists for this field."` → `request_reason = ""` | `ProposalWorkspaceTest.test_active_proposal_disables_request_and_allows_another_operator_to_cancel` |
| cancel-other-operator | `_action("cancel", "Cancel", permission_reason or ("" if pending else "There is no active proposal."))` → `_action("cancel", "Cancel", "Requester only")` | `ProposalWorkspaceTest.test_active_proposal_disables_request_and_allows_another_operator_to_cancel` |
| device-permission | `return "The resolved Device is unavailable or outside your view permission."` → `return ""` | `ProposalWorkspaceTest.test_active_proposal_disables_request_and_allows_another_operator_to_cancel` |
| pending | `"pending": pending` → `"pending": False` | `ProposalWorkspaceTest.test_active_proposal_disables_request_and_allows_another_operator_to_cancel` |
| proposed-field | `state = "proposed"` → `state = UNRESOLVED` | `ProposalWorkspaceTest.test_active_proposal_disables_request_and_allows_another_operator_to_cancel` |
| active-summary | `summary["active_proposals"] = ResolutionProposal.objects.filter(` → `summary["active_proposals"] = 0 * ResolutionProposal.objects.filter(` | `ProposalWorkspaceTest.test_active_proposal_disables_request_and_allows_another_operator_to_cancel` |
| candidate-badge | `else "Proposal - not applied"` → `else "Completed"` | `ProposalWorkspaceTest.test_candidate_actions_follow_decision_permission_and_staleness` |
| candidate-kind | `candidate = f"{entry['display_name']} ({str(proposal.selected_object_type.name).capitalize()})"` → `candidate = entry["display_name"]` | `ProposalWorkspaceTest.test_candidate_actions_follow_decision_permission_and_staleness` |
| accept-permission | `if not self.preview_allowed or not self.actor.has_perm("netbox_data_import.add_terminationresolution"):` → `if False:` | `ProposalWorkspaceTest.test_candidate_actions_follow_decision_permission_and_staleness` |
| reject-allowed | `_action("reject", "Reject", reject_reason)` → `_action("reject", "Reject", "Not allowed")` | `ProposalWorkspaceTest.test_candidate_actions_follow_decision_permission_and_staleness` |
| accept-allowed | `_action("accept", "Accept", accept_reason)` → `_action("accept", "Accept", accept_reason or "Not allowed")` | `ProposalWorkspaceTest.test_candidate_actions_follow_decision_permission_and_staleness` |
| stale-badge | `"Proposal - stale, not applied" if stale_reason` → `"Completed" if stale_reason` | `ProposalWorkspaceTest.test_candidate_actions_follow_decision_permission_and_staleness` |
| stale-field | `state = "stale"` → `state = UNRESOLVED` | `ProposalWorkspaceTest.test_candidate_actions_follow_decision_permission_and_staleness` |
| stale-accept | `accept_reason = accept_reason or stale_reason` → `accept_reason = accept_reason` | `ProposalWorkspaceTest.test_candidate_actions_follow_decision_permission_and_staleness` |
| no-match-accept | `accept_reason = "The backend found no match. There is no candidate to accept."` → `accept_reason = ""` | `ProposalWorkspaceTest.test_no_match_has_disabled_accept_and_explanation` |
| explanation | `"explanation": proposal.explanation if proposal is not None else ""` → `"explanation": ""` | `ProposalWorkspaceTest.test_no_match_has_disabled_accept_and_explanation` |
| terminal | `"pending": pending` → `"pending": True` | `ProposalWorkspaceTest.test_no_match_has_disabled_accept_and_explanation` |
| accepted-field | `state = ProposalDecision.ACCEPTED` → `state = field["state"]` | `ProposalWorkspaceTest.test_accept_then_reread_presents_accepted_termination` |
| accepted-badge | `badge = proposal.get_decision_display()` → `badge = "Completed"` | `ProposalWorkspaceTest.test_accept_then_reread_presents_accepted_termination` |
| failure-code | `"failure_code": proposal.failure_reason if proposal is not None else ""` → `"failure_code": ""` | `ProposalWorkspaceTest.test_failed_card_retains_typed_reason_backend_and_attempt_count` |
| failure-label | `"failure": proposal.get_failure_reason_display() if proposal is not None else ""` → `"failure": ""` | `ProposalWorkspaceTest.test_failed_card_retains_typed_reason_backend_and_attempt_count` |
| failed-field | `state = ProposalStatus.FAILED` → `state = UNRESOLVED` | `ProposalWorkspaceTest.test_failed_card_retains_typed_reason_backend_and_attempt_count` |
| backend-attempt-count | `"attempt_count": len(metadata.get("attempts", []))` → `"attempt_count": 0` | `ProposalWorkspaceTest.test_failed_card_retains_typed_reason_backend_and_attempt_count` |
| backend-metadata | `for key, value in metadata.items()` → `for key, value in {}.items()` | `ProposalWorkspaceTest.test_failed_card_retains_typed_reason_backend_and_attempt_count` |
| retry-label | `"Ask AI again" if proposal is not None and proposal.status == ProposalStatus.FAILED else "Ask AI"` → `"Ask AI"` | `ProposalWorkspaceTest.test_failed_card_retains_typed_reason_backend_and_attempt_count` |
| backend-once | `resolve_active_backend()` → `resolve_active_backend() ;             resolve_active_backend()` | `ProposalWorkspaceTest.test_backend_resolution_runs_once_for_all_displayed_fields` |
| request-profile-permission | `return "You do not have permission to request proposals for this Import Profile."` → `return ""` | `ProposalWorkspaceTest.test_profile_view_permission_disables_request_and_reject` |
| reject-profile-permission | `reject_reason = decision_reason if self.preview_allowed else "You do not have permission to reject proposals."` → `reject_reason = decision_reason` | `ProposalWorkspaceTest.test_profile_view_permission_disables_request_and_reject` |
| mapped-peer | `return "Ask AI supports termination fields only. Choose the mapped peer manually."` → `return "Unavailable"` | `ProposalWorkspaceTest.test_mapped_peer_has_a_manual_reason` |

## Additional server assertions

| Behavior | Applied mutation | Test that caught it |
| --- | --- | --- |
| history-denied-envelope | `JsonResponse({"ok": False, "error": str(exc)}, status=409)` → `JsonResponse({"ok": False, "error": str(exc), "history": []}, status=409)` | `ProposalWorkspaceTest.test_history_excludes_other_profiles_and_fields` |
| history-lost-staleness | `payload["staleness_error"] = "The saved import target is gone or outside your view scope."` → `payload["staleness"] = {} ;             payload["staleness_error"] = "The saved import target is gone or outside your view scope."` | `ProposalWorkspaceTest.test_profile_view_only_can_read_history_without_target_access` |
| history-view-actions | `_action("reject", "Reject", reject_reason)` → `_action("reject", "Reject", "")` | `ProposalWorkspaceTest.test_profile_view_only_can_read_history_without_target_access` |
| plan-unchanged | `summary["preview_state"] = self._preview_state(request, drift)` → `summary["preview_state"] = self._preview_state(request, drift) ;         request.session[PREVIEW_PLAN_SESSION_KEY] = {}` | `ProposalWorkspaceTest.test_workspace_supplies_affordances_without_editing_the_plan` |
| selected-presentation | `{**field, "proposal": proposal_fields[field["field_key"]]["presentation"]}` → `{**field, "proposal": {**proposal_fields[field["field_key"]]["presentation"], "pending": False}}` | `ProposalWorkspaceTest.test_active_proposal_disables_request_and_allows_another_operator_to_cancel` |
| accept-replan-revision | `record_recalculated_preview(request.session, live, user=request.user)` → `pass` | `ProposalWorkspaceTest.test_accept_then_reread_presents_accepted_termination` |
| accepted-request-reason | `if not request_reason and state != UNRESOLVED:` → `if not request_reason and state not in (UNRESOLVED, ProposalDecision.ACCEPTED):` | `ProposalWorkspaceTest.test_accept_then_reread_presents_accepted_termination` |
| backend-enabled-row | `resolve_active_backend()` → `raise NoActiveInferenceBackend()` | `ProposalWorkspaceTest.test_backend_resolution_runs_once_for_all_displayed_fields` |
| backend-many-fields | `summary = dict(workspace.trace_summary)` → `selected = replace(selected, terminations=selected.terminations[:1]) ;         summary = dict(workspace.trace_summary)` | `ProposalWorkspaceTest.test_backend_resolution_runs_once_for_all_displayed_fields` |
| empty-history | `"history": records` → `"history": [None]` | `ProposalWorkspaceTest.test_read_without_an_attempt_returns_null` |
| history-display | `"outcome": row.get_outcome_display() or "No outcome"` → `"outcome": "No outcome"` | `ProposalWorkspaceTest.test_history_returns_every_attempt_newest_first_with_status_and_outcome` |
| unoffered-decisions | `decision_reason = "This preview asked no question about that termination."` → `decision_reason = ""` | `ProposalWorkspaceTest.test_history_for_a_field_outside_the_preview_disables_every_command` |
| unoffered-cancel | `return "This preview asked no question about that termination."` → `return ""` | `ProposalWorkspaceTest.test_active_history_outside_the_preview_disables_cancel` |

## Workspace view scope

| Behavior | Applied mutation | Test that caught it |
| --- | --- | --- |
| workspace-history-view-scope | `if self.view_reason: ;             return {field["field_key"]: self.field(field, []) for field in fields}` → `if False: ;             return {field["field_key"]: self.field(field, []) for field in fields}` | `ProposalWorkspaceTest.test_workspace_history_requires_profile_view_scope_even_with_preview_access` |
| workspace-summary-view-scope | `if proposal_display.view_reason:` → `if False:` | `ProposalWorkspaceTest.test_workspace_history_requires_profile_view_scope_even_with_preview_access` |
| workspace-action-view-scope | `"reason": self.view_reason or action["reason"]` → `"reason": action["reason"]` | `ProposalWorkspaceTest.test_workspace_history_requires_profile_view_scope_even_with_preview_access` |

## Browser controller

| Behavior | Applied mutation | Test that caught it |
| --- | --- | --- |
| poll-early | `}, 3000);` → `}, 2000);` | Playwright: pending progress polls every three seconds and stops on completion |
| poll-late | `}, 3000);` → `}, 4000);` | Playwright: pending progress polls every three seconds and stops on completion |
| poll-terminal | `if (!card.isConnected \|\| !state.payload.presentation.pending) return;` → `if (!card.isConnected) return;` | Playwright: pending progress polls every three seconds and stops on completion |
| pending-progress | `node(card, 'progress').hidden = !display.pending;` → `node(card, 'progress').hidden = true;` | Vitest: polls at three seconds and stops at a terminal response |
| terminal-progress | `node(card, 'progress').hidden = !display.pending;` → `node(card, 'progress').hidden = false;` | Vitest: polls at three seconds and stops at a terminal response |
| abort-detached | `if (state.controller) state.controller.abort();` → `if (state.controller) void state.controller;` | Vitest: aborts a detached read and ignores its late answer |
| remove-timer | `clearTimeout(state.timer);` → `void state.timer;` | Vitest: stops polling when a card leaves the DOM |
| read-field | `url.searchParams.set('field_key', card.dataset.proposalField);` → `url.searchParams.set('field_key', 'wrong');` | Vitest: polls at three seconds and stops at a terminal response |
| read-revision | `url.searchParams.set('preview_revision', card.dataset.previewRevision);` → `url.searchParams.set('preview_revision', 'wrong');` | Vitest: polls at three seconds and stops at a terminal response |
| candidate-card | `node(card, 'display').hidden = !payload.proposal;` → `node(card, 'display').hidden = true;` | Vitest: renders candidate details as text and every history attempt |
| candidate-labels | `node(card, name).textContent = display[name];` → `node(card, name).textContent = '';` | Vitest: polls at three seconds and stops at a terminal response |
| text-safety | `node(card, name).textContent = display[name];` → `node(card, name).innerHTML = display[name];` | Vitest: renders candidate details as text and every history attempt |
| candidate-child | `node(card, name).textContent = display[name];` → `node(card, name).textContent = display[name]; if (name === 'candidate') node(card, name).appendChild(document.createElement('img'));` | Vitest: renders candidate details as text and every history attempt |
| explanation | `node(card, name).textContent = display[name];` → `node(card, name).textContent = name === 'explanation' ? '' : display[name];` | Vitest: renders candidate details as text and every history attempt |
| backend-attempts | `'Backend attempts: ' + display.attempt_count` → `'Backend attempts: 0'` | Vitest: renders candidate details as text and every history attempt |
| backend-metadata | `item.label + ': ' + item.value` → `item.label` | Vitest: renders candidate details as text and every history attempt |
| history-all | `payload.history_display.forEach(function (attempt)` → `payload.history_display.slice(0, 1).forEach(function (attempt)` | Playwright: a completed card shows the candidate, kind, explanation, backend, and full history |
| history-empty-label | `node(card, 'empty-history').hidden = payload.history_display.length > 0;` → `node(card, 'empty-history').hidden = false;` | Vitest: renders candidate details as text and every history attempt |
| disabled-actions | `button.disabled = state.busy \|\| Boolean(action.reason);` → `button.disabled = false;` | Playwright: stale and no-match cards keep Accept disabled with the reason underneath |
| visible-actions | `button.disabled = state.busy \|\| Boolean(action.reason);` → `button.disabled = state.busy \|\| Boolean(action.reason); button.hidden = button.disabled;` | Playwright: stale and no-match cards keep Accept disabled with the reason underneath |
| disabled-reason | `reason.textContent = action.reason;` → `reason.textContent = '';` | Playwright: stale and no-match cards keep Accept disabled with the reason underneath |
| visible-reason | `reason.hidden = !action.reason;` → `reason.hidden = true;` | Vitest: keeps stale and no-match accept buttons visible and disabled with their reason |
| failure-reason | `display.failure + ' (' + display.failure_code + ')'` → `display.failure` | Vitest: renders a failed attempt and offers Ask AI again |
| field-state | `node(card, 'state').textContent = display.field_state;` → `node(card, 'state').textContent = '';` | Playwright: acceptance refreshes the accepted field and submits the existing replan form |
| retry-label | `button.textContent = action.label;` → `button.textContent = 'Action';` | Vitest: renders a failed attempt and offers Ask AI again |
| boost-guard | `window.ndiTraceProposals.init(); ;     return;` → `window.ndiTraceProposals.init();` | Playwright: a boost removes the old poll and binds the replacement card only once |
| action-method | `method: 'POST', body: data` → `method: 'GET', body: data` | Vitest: sends cancel with the field, attempt, revision and CSRF token once after repeated script evaluation |
| action-credentials | `credentials: 'same-origin', signal: state.controller.signal, ;       }).then(readResponse); ;       if (!card.isConnected) return;` → `credentials: 'omit', signal: state.controller.signal, ;       }).then(readResponse); ;       if (!card.isConnected) return;` | Vitest: sends cancel with the field, attempt, revision and CSRF token once after repeated script evaluation |
| action-field | `data.set('field_key', card.dataset.proposalField);` → `data.set('field_key', 'wrong');` | Vitest: sends cancel with the field, attempt, revision and CSRF token once after repeated script evaluation |
| action-id | `data.set('proposal_id', state.payload.proposal.id);` → `data.set('proposal_id', 'wrong');` | Playwright: acceptance refreshes the accepted field and submits the existing replan form |
| action-csrf | `data.set('csrfmiddlewaretoken', form.elements.namedItem('csrfmiddlewaretoken').value);` → `data.set('csrfmiddlewaretoken', 'wrong');` | Playwright: acceptance refreshes the accepted field and submits the existing replan form |
| action-revision | `data.set('preview_revision', card.dataset.previewRevision);` → `data.set('preview_revision', 'wrong');` | Playwright: acceptance refreshes the accepted field and submits the existing replan form |
| error-text | `target.textContent = message;` → `target.textContent = '';` | Playwright: HTTP action refusals are visible in the field |
| error-visible | `target.hidden = !message;` → `target.hidden = true;` | Playwright: HTTP action refusals are visible in the field |
| read-retry | `error(card, failure.message); ;       schedule(card);` → `error(card, failure.message);` | Vitest: retries a failed pending read at the next interval |
| error-controls | `state.busy = false; ;       render(card, state.payload); ;       await refresh(card); ;       if (card.isConnected) error(card, failure.message);` → `state.busy = false; ;       await refresh(card); ;       if (card.isConnected) error(card, failure.message);` | Vitest: recovers controls when both an action and its refresh fail |
| json-error | `throw new Error('The proposal response could not be read. Reload the workspace.');` → `throw new Error('Invalid response');` | Vitest: reports a non-JSON response instead of treating a login page as success |
| accept-replan | `reread.form.requestSubmit(reread);` → `void reread;` | Playwright: acceptance refreshes the accepted field and submits the existing replan form |

## Detached cards

| Behavior | Applied mutation | Test that caught it |
| --- | --- | --- |
| poll-detached | `if (!card.isConnected \|\| !state.payload.presentation.pending) return;` → `if (!state.payload.presentation.pending) return;`; `if (!card.isConnected) { stop(card); return; }` → `void card;`; `if (!state \|\| !card.isConnected) return;` → `if (!state) return;`; `if (!card.isConnected \|\| generation !== state.generation) return;` → `if (generation !== state.generation) return;`; `new MutationObserver(function () { ;     cards.forEach(function (_, card) { if (!card.isConnected) stop(card); });` → `new MutationObserver(function () { ;     void cards;` | Playwright: a boost removes the old poll and binds the replacement card only once |
| late-page-response | `return card.querySelector('[data-proposal-' + name + ']');` → `return document.querySelector('[data-proposal-' + name + ']');`; `cards.delete(card);` → `void card;`; `if (!card.isConnected \|\| generation !== state.generation) return;` → `if (generation !== state.generation) return;` | Vitest: aborts a detached read and ignores its late answer |

## Retained synchronization

| Behavior | Applied mutation | Test that caught it |
| --- | --- | --- |
| retained-replan-message | `The resolution was saved. Re-read the workspace when the active sync finishes.` → `Wait.` | Playwright: a saved acceptance explains why an active sync prevents the replan |
| retained-replan-refusal | `error(card, 'The resolution was saved. Re-read the workspace when the active sync finishes.'); ;       return;` → `error(card, 'The resolution was saved. Re-read the workspace when the active sync finishes.'); ;       reread.form.requestSubmit(reread); ;       return;` | Playwright: a saved acceptance explains why an active sync prevents the replan |

## Rendered template

| Behavior | Applied mutation | Test that caught it |
| --- | --- | --- |
| template-disabled-buttons | `{% if action.reason %}disabled{% endif %}` → (removed) | `ProposalWorkspaceTest.test_workspace_renders_actions_reasons_and_the_controller_contract` |
| template-visible-buttons | `data-proposal-action="{{ action.key }}"` → `data-proposal-action="{{ action.key }}" hidden` | `ProposalWorkspaceTest.test_workspace_renders_actions_reasons_and_the_controller_contract` |
| template-visible-reasons | `id="proposalReason{{ forloop.parentloop.counter }}{{ action.key }}"` → `id="proposalReason{{ forloop.parentloop.counter }}{{ action.key }}" hidden` | `ProposalWorkspaceTest.test_workspace_renders_actions_reasons_and_the_controller_contract` |
| template-reason-text | `{% if not action.reason %}hidden{% endif %}>{{ action.reason }}</div> ;                           </div>` → `{% if not action.reason %}hidden{% endif %}></div> ;                           </div>` | `ProposalWorkspaceTest.test_workspace_renders_actions_reasons_and_the_controller_contract` |
| template-controller | `netbox_data_import/js/trace_proposals.js` → `netbox_data_import/js/missing.js` | `ProposalWorkspaceTest.test_workspace_renders_actions_reasons_and_the_controller_contract` |
| template-record-binding | `proposal_fields = proposal_display.fields(selected.terminations if selected else [])` → `proposal_fields = proposal_display.fields(selected.terminations if selected else []) ;         for item in proposal_fields.values(): ;             if item["proposal"]: ;                 item["proposal"]["id"] = 0` | `ProposalWorkspaceTest.test_workspace_renders_actions_reasons_and_the_controller_contract` |

## Additional frontend assertions

| Behavior | Applied mutation | Test that caught it |
| --- | --- | --- |
| vitest-poll-early | `}, 3000);` → `}, 2000);` | Vitest: polls at three seconds and stops at a terminal response |
| vitest-poll-late | `}, 3000);` → `}, 4000);` | Vitest: polls at three seconds and stops at a terminal response |
| vitest-poll-terminal | `if (!card.isConnected \|\| !state.payload.presentation.pending) return;` → `if (!card.isConnected) return;` | Vitest: polls at three seconds and stops at a terminal response |
| vitest-history-all | `payload.history_display.forEach(function (attempt)` → `payload.history_display.slice(0, 1).forEach(function (attempt)` | Vitest: renders candidate details as text and every history attempt |
| vitest-disabled-actions | `button.disabled = state.busy \|\| Boolean(action.reason);` → `button.disabled = false;` | Vitest: keeps stale and no-match accept buttons visible and disabled with their reason |
| vitest-visible-actions | `button.disabled = state.busy \|\| Boolean(action.reason);` → `button.disabled = state.busy \|\| Boolean(action.reason); button.hidden = button.disabled;` | Vitest: keeps stale and no-match accept buttons visible and disabled with their reason |
| vitest-disabled-reason | `reason.textContent = action.reason;` → `reason.textContent = '';` | Vitest: keeps stale and no-match accept buttons visible and disabled with their reason |
| vitest-field-state | `node(card, 'state').textContent = display.field_state;` → `node(card, 'state').textContent = '';` | Vitest: renders a failed attempt and offers Ask AI again |
| vitest-boost-guard | `window.ndiTraceProposals.init(); ;     return;` → `window.ndiTraceProposals.init();` | Vitest: stops polling when a card leaves the DOM |
| vitest-error-text | `target.textContent = message;` → `target.textContent = '';` | Vitest: refreshes an action refusal and shows the server reason |
| browser-pending-progress | `node(card, 'progress').hidden = !display.pending;` → `node(card, 'progress').hidden = true;` | Playwright: pending progress polls every three seconds and stops on completion |
| browser-terminal-progress | `node(card, 'progress').hidden = !display.pending;` → `node(card, 'progress').hidden = false;` | Playwright: pending progress polls every three seconds and stops on completion |
| browser-explanation | `node(card, name).textContent = display[name];` → `node(card, name).textContent = name === 'explanation' ? '' : display[name];` | Playwright: a completed card shows the candidate, kind, explanation, backend, and full history |
| browser-backend-attempts | `'Backend attempts: ' + display.attempt_count` → `'Backend attempts: 0'` | Playwright: a completed card shows the candidate, kind, explanation, backend, and full history |
| browser-backend-metadata | `item.label + ': ' + item.value` → `item.label` | Playwright: a completed card shows the candidate, kind, explanation, backend, and full history |
| browser-failure-reason | `display.failure + ' (' + display.failure_code + ')'` → `display.failure` | Playwright: Ask AI again posts a new request after failure and shows pending progress |
| browser-action-field | `data.set('field_key', card.dataset.proposalField);` → `data.set('field_key', 'wrong');` | Playwright: Ask AI again posts a new request after failure and shows pending progress |
| browser-candidate | `node(card, name).textContent = display[name];` → `node(card, name).textContent = name === "candidate" ? "" : display[name];` | Playwright: a completed card shows the candidate, kind, explanation, backend, and full history |
| browser-accept-enabled | `button.disabled = state.busy \|\| Boolean(action.reason);` → `button.disabled = state.busy \|\| Boolean(action.reason) \|\| action.key === "accept";` | Playwright: a completed card shows the candidate, kind, explanation, backend, and full history |
| browser-reject-enabled | `button.disabled = state.busy \|\| Boolean(action.reason);` → `button.disabled = state.busy \|\| Boolean(action.reason) \|\| action.key === "reject";` | Playwright: a completed card shows the candidate, kind, explanation, backend, and full history |
| browser-action-refresh | `await refresh(card);` → `void card;` | Playwright: a boost removes the old poll and binds the replacement card only once |
| browser-single-post | `var payload = await fetch(action.url, {` → `await fetch(action.url, {method: "POST", body: data, headers: {Accept: "application/json"}}); ;       var payload = await fetch(action.url, {` | Playwright: a boost removes the old poll and binds the replacement card only once |
| vitest-request-enabled | `button.disabled = state.busy \|\| Boolean(action.reason);` → `button.disabled = state.busy \|\| Boolean(action.reason) \|\| action.key === "request";` | Vitest: renders a failed attempt and offers Ask AI again |
| vitest-release-actions | `state.busy = false; ;       render(card, state.payload); ;       await refresh(card); ;       if (card.isConnected) error(card, failure.message);` → `state.busy = true; ;       render(card, state.payload); ;       await refresh(card); ;       if (card.isConnected) error(card, failure.message);` | Vitest: refreshes an action refusal and shows the server reason |
| vitest-single-refresh | `await refresh(card);` → `await refresh(card); await refresh(card);` | Vitest: sends cancel with the field, attempt, revision and CSRF token once after repeated script evaluation |

## Checked and clean

- Profile, task, and field history scope, including preview access without profile view scope.
- Backend row and fallback availability, with one backend resolution per response.
- Request, cancel, accept, and reject reasons, including actions by another operator.
- Candidate, no-match, stale, failed, pending, and accepted card states.
- Three-second polling, terminal stopping, detached-card cleanup, and late-response isolation.
- htmx initialization, one action POST, CSRF, field binding, and preview revision binding.
- Acceptance writes the resolution through the existing endpoint and triggers the existing replan.
- Retained synchronization keeps its replan guard and shows the saved-decision explanation.
- Backend metadata, attempt count, every history entry, and text-only rendering of response content.
- Actual template controls and disabled reasons, plus light and dark theme inspection.
- No proposal imports or edits in the Cable Target Module. No Import Plan, model, or migration changes.
- Ruff, formatting, mypy, SPDX headers, mock discipline, and pre-commit checks.
- Two requested commits on the existing branch. No branch creation, rebase, push, or pull request.
