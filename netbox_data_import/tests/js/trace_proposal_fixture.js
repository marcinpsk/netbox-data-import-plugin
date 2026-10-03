/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

export function payload(overrides = {}) {
  return {
    ok: true,
    proposal: {id: 7},
    presentation: {
      pending: true, field_state: 'proposed', state_style: 'proposed', badge: 'Queued', candidate: '', explanation: '',
      failure: '', failure_code: '',
      job_status: 'Background job: Pending, requested 0 minutes ago', job_note: '', page_status: '',
      actions: [
        {key: 'request', label: 'Ask AI', reason: 'An active proposal exists.', url: '/request/'},
        {key: 'cancel', label: 'Cancel', reason: '', url: '/cancel/'},
        {key: 'accept', label: 'Accept', reason: 'Wait for a completed proposal.', url: '/accept/'},
        {key: 'reject', label: 'Reject', reason: 'Wait for a completed proposal.', url: '/reject/'},
      ],
      ...overrides,
    },
    history_display: [{id: 7, created: '2026-09-11T08:00:00+00:00', status: 'Queued', outcome: 'No outcome'}],
    history_has_more: false,
    history_url: '/api/plugins/netbox-data-import/resolution-proposal-history/?profile_id=1&field_key=field',
  };
}

export function completed(overrides = {}) {
  return payload({
    pending: false, field_state: 'proposed', state_style: 'proposed', badge: 'Proposal - not applied',
    candidate: 'eth0 (Interface)', explanation: 'The labels name the same port.',
    job_status: '', job_note: '', page_status: '',
    actions: [
      {key: 'request', label: 'Ask AI', reason: '', url: '/request/'},
      {key: 'cancel', label: 'Cancel', reason: 'There is no active proposal.', url: '/cancel/'},
      {key: 'accept', label: 'Accept', reason: '', url: '/accept/'},
      {key: 'reject', label: 'Reject', reason: '', url: '/reject/'},
    ],
    ...overrides,
  });
}

export function claimFields(revision = 4) {
  return [
    ['preview_token', 'token-1'], ['preview_revision', String(revision)],
    ['preview_document', '11'], ['preview_profile', '3'],
  ];
}
export const CLAIM = claimFields();

function card(key, initial) {
  const actions = items => items.map(action => `
    <button type="button" data-proposal-action="${action.key}">${action.label}</button>
    <div data-proposal-reason="${action.key}" hidden></div>`).join('');
  const proposal = `
    <div data-proposal-display>
      <span data-proposal-badge></span><span data-proposal-progress hidden>Waiting for the backend...</span>
      <div data-proposal-job hidden></div><div data-proposal-job-note hidden></div>
      <div data-proposal-page hidden></div>
      <div data-proposal-candidate></div><div data-proposal-explanation></div><div data-proposal-failure></div>
      ${actions(initial.presentation.actions.slice(2))}
    </div>
    <details open data-proposal-history-disclosure><summary>Proposal history</summary>
      <ul data-proposal-history></ul><a data-proposal-history-link hidden>View all attempts</a></details>`;
  return `
    <div data-proposal-field="${key}" data-proposal-url="/proposal/">
      <span class="badge ndi-trace-state-unknown" data-proposal-state data-proposal-state-prefix="ndi-trace-state-"></span>
      <div data-proposal-content>${initial.proposal ? proposal : ''}</div>
      <template data-proposal-template>${proposal}</template>
      <div data-proposal-field-actions>${actions(initial.presentation.actions.slice(0, 2))}</div>
      <div data-proposal-error hidden></div>
    </div>`;
}

/* One card for `field`, plus one card for each further field key in `others`. */
export function fixture(initial = payload(), others = {}, revision = 4) {
  const fields = {field: initial, ...others};
  const claim = claimFields(revision).map(([name, value]) => `<input type="hidden" name="${name}" value="${value}">`).join('');
  return `
    <base href="http://preview.test/">
    <style>[hidden] { display: none !important; }</style>
    <form id="ndi-preview-claim" hidden>${claim}</form>
    <output id="ndi-revision">${revision}</output>
    <form id="traceTerminationForm"><input name="csrfmiddlewaretoken" value="fixture-token"></form>
    <script type="application/json" id="traceProposalFields">${JSON.stringify(fields).replaceAll('<', '\\u003c')}</script>
    ${Object.entries(fields).map(([key, field]) => card(key, field)).join('')}`;
}
