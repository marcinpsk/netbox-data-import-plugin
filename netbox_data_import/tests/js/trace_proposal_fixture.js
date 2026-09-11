/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

export function payload(overrides = {}) {
  return {
    ok: true,
    proposal: {id: 7},
    presentation: {
      pending: true, field_state: 'proposed', state_style: 'proposed', badge: 'Queued', candidate: '', explanation: '',
      failure: '', failure_code: '', attempt_count: 0, metadata: [],
      actions: [
        {key: 'request', label: 'Ask AI', reason: 'An active proposal exists.', url: '/request/'},
        {key: 'cancel', label: 'Cancel', reason: '', url: '/cancel/'},
        {key: 'accept', label: 'Accept', reason: 'Wait for a completed proposal.', url: '/accept/'},
        {key: 'reject', label: 'Reject', reason: 'Wait for a completed proposal.', url: '/reject/'},
      ],
      ...overrides,
    },
    history_display: [{id: 7, created: '2026-09-11T08:00:00+00:00', status: 'Queued', outcome: 'No outcome'}],
  };
}

export function completed(overrides = {}) {
  return payload({
    pending: false, field_state: 'proposed', state_style: 'proposed', badge: 'Proposal - not applied',
    candidate: 'eth0 (Interface)', explanation: 'The labels name the same port.',
    attempt_count: 2, metadata: [{label: 'backend model', value: 'fixture-model'}],
    actions: [
      {key: 'request', label: 'Ask AI', reason: '', url: '/request/'},
      {key: 'cancel', label: 'Cancel', reason: 'There is no active proposal.', url: '/cancel/'},
      {key: 'accept', label: 'Accept', reason: '', url: '/accept/'},
      {key: 'reject', label: 'Reject', reason: '', url: '/reject/'},
    ],
    ...overrides,
  });
}

export function fixture(initial = payload()) {
  const actions = items => items.map(action => `
    <button type="button" data-proposal-action="${action.key}">${action.label}</button>
    <div data-proposal-reason="${action.key}" hidden></div>`).join('');
  const proposal = `
    <div data-proposal-display>
      <span data-proposal-badge></span><span data-proposal-progress hidden>Waiting for the backend...</span>
      <div data-proposal-candidate></div><div data-proposal-explanation></div><div data-proposal-failure></div>
      <div data-proposal-attempts></div><ul data-proposal-metadata></ul>
      ${actions(initial.presentation.actions.slice(2))}
    </div>
    <details open data-proposal-history-disclosure><summary>Proposal history</summary>
      <ul data-proposal-history></ul></details>`;
  return `
    <base href="http://preview.test/">
    <style>[hidden] { display: none !important; }</style>
    <form action="/reread/" method="post"><button id="traceWorkspaceReread">Re-read from NetBox</button></form>
    <form id="traceTerminationForm"><input name="csrfmiddlewaretoken" value="fixture-token"></form>
    <script type="application/json" id="traceProposalFields">${JSON.stringify({field: initial}).replaceAll('<', '\\u003c')}</script>
    <div data-proposal-field="field" data-proposal-url="/proposal/" data-preview-revision="revision-1">
      <span class="badge ndi-trace-state-unknown" data-proposal-state data-proposal-state-prefix="ndi-trace-state-"></span>
      <div data-proposal-content>${initial.proposal ? proposal : ''}</div>
      <template data-proposal-template>${proposal}</template>
      <div data-proposal-field-actions>${actions(initial.presentation.actions.slice(0, 2))}</div>
      <div data-proposal-error hidden></div>
    </div>`;
}
