/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

import {readFileSync} from 'node:fs';
import {afterEach, beforeEach, expect, it, vi} from 'vitest';
import {CLAIM, completed, fixture, payload} from './trace_proposal_fixture.js';

const source = readFileSync('netbox_data_import/static/netbox_data_import/js/trace_proposals.js', 'utf8');
const claimSource = readFileSync('netbox_data_import/static/netbox_data_import/js/preview_claim.js', 'utf8');
const node = name => document.querySelector('[data-proposal-' + name + ']');
const button = key => document.querySelector('[data-proposal-action="' + key + '"]');
const response = body => ({ok: true, json: async () => body});
const refused = (status, error) => ({ok: false, status, json: async () => ({ok: false, error})});
const cardOf = key => document.querySelector('[data-proposal-field="' + key + '"]');
const inCard = (key, selector) => cardOf(key).querySelector(selector);
const reads = () => fetch.mock.calls.filter(([, options = {}]) => !options.method);
let reload;

function mount(initial = payload(), others = {}) {
  document.body.innerHTML = fixture(initial, others);
  window.eval(claimSource);
  window.eval(source);
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal('fetch', vi.fn(async () => response(completed())));
  reload = vi.fn();
  vi.stubGlobal('location', {reload});
});
afterEach(async () => {
  document.body.replaceChildren();
  await vi.advanceTimersByTimeAsync(0);
  vi.clearAllTimers();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

it('polls at three seconds and stops at a terminal response', async () => {
  mount();
  expect(node('progress').hidden).toBe(false);
  await vi.advanceTimersByTimeAsync(2999);
  expect(fetch).toHaveBeenCalledTimes(0);
  await vi.advanceTimersByTimeAsync(1);
  expect(fetch).toHaveBeenCalledTimes(1);
  const asked = new URL(fetch.mock.calls[0][0]);
  expect([...asked.searchParams]).toEqual([['field_key', 'field'], ...CLAIM]);
  expect(node('badge').textContent).toBe('Proposal - not applied');
  expect(node('progress').hidden).toBe(true);
  await vi.advanceTimersByTimeAsync(12000);
  expect(fetch).toHaveBeenCalledTimes(1);
});

it('stops polling when a card leaves the DOM', async () => {
  fetch.mockImplementation(async () => response(payload()));
  mount();
  await vi.advanceTimersByTimeAsync(3000);
  expect(fetch).toHaveBeenCalledTimes(1);
  document.body.replaceChildren();
  await vi.advanceTimersByTimeAsync(0);
  expect(vi.getTimerCount()).toBe(0);
  await vi.advanceTimersByTimeAsync(12000);
  expect(fetch).toHaveBeenCalledTimes(1);
  expect(vi.getTimerCount()).toBe(0);
});

it('aborts a detached read and ignores its late answer', async () => {
  let release;
  fetch.mockImplementation(() => new Promise(done => { release = done; }));
  mount();
  await vi.advanceTimersByTimeAsync(3000);
  const signal = fetch.mock.calls[0][1].signal;
  mount(completed({candidate: 'replacement (Rear port)'}));
  release(response(completed({candidate: 'obsolete'})));
  await vi.advanceTimersByTimeAsync(0);
  expect(signal.aborted).toBe(true);
  expect(node('candidate').textContent).toBe('replacement (Rear port)');
});

it('renders candidate details as text and recent history attempts', () => {
  const initial = completed({candidate: '<img src=x onerror=alert(1)> (Front port)'});
  initial.history_display.push({id: 6, created: 'earlier', status: 'Failed', outcome: 'No outcome', failure: 'Timeout'});
  initial.history_has_more = true;
  mount(initial);
  expect(node('display').hidden).toBe(false);
  expect(node('candidate').textContent).toBe(initial.presentation.candidate);
  expect(node('candidate').children.length).toBe(0);
  expect(node('explanation').textContent).toBe('The labels name the same port.');
  expect([...node('history').children].map(row => row.textContent)).toEqual([
    '#7 · 2026-09-11T08:00:00+00:00 · Queued · No outcome',
    '#6 · earlier · Failed · No outcome · Timeout',
  ]);
  expect(node('history-disclosure').hidden).toBe(false);
  expect(node('history-link').hidden).toBe(false);
  expect(node('history-link').href).toContain('resolution-proposal-history');
});

it('keeps stale and no-match accept buttons visible and disabled with their reason', () => {
  for (const reason of ['The eligible candidates changed.', 'The backend found no match.']) {
    const initial = completed({badge: 'Proposal - stale, not applied'});
    initial.presentation.actions[2].reason = reason;
    mount(initial);
    expect(button('accept').disabled).toBe(true);
    expect(button('accept').hidden).toBe(false);
    expect(document.querySelector('[data-proposal-reason="accept"]').textContent).toBe(reason);
    expect(document.querySelector('[data-proposal-reason="accept"]').hidden).toBe(false);
  }
});

it('renders a failed attempt and offers Ask AI again', () => {
  const initial = completed({badge: 'Failed', field_state: 'failed', failure: 'Backend refusal', failure_code: 'backend_refusal'});
  initial.presentation.actions[0].label = 'Ask AI again';
  mount(initial);
  expect(node('failure').textContent).toBe('Backend refusal (backend_refusal)');
  expect(node('state').textContent).toBe('failed');
  expect([button('request').textContent, button('request').disabled]).toEqual(['Ask AI again', false]);
});

it('sends cancel with the field, attempt, claim and CSRF token once after repeated script evaluation', async () => {
  mount();
  window.eval(source);
  button('cancel').click();
  button('cancel').click();
  await vi.advanceTimersByTimeAsync(0);
  expect(fetch).toHaveBeenCalledTimes(1);
  await vi.advanceTimersByTimeAsync(12000);
  expect(fetch).toHaveBeenCalledTimes(1);
  const [url, options] = fetch.mock.calls[0];
  expect([url, options.method, options.credentials, options.headers.Accept]).toEqual([
    '/cancel/', 'POST', 'same-origin', 'application/json',
  ]);
  expect([...options.body]).toEqual([
    ['csrfmiddlewaretoken', 'fixture-token'], ...CLAIM, ['field_key', 'field'], ['proposal_id', '7'],
  ]);
  expect(reload).toHaveBeenCalledOnce();
});

it('stops every card polling before a successful action reloads the workspace', async () => {
  const fieldOf = url => new URL(url).searchParams.get('field_key');
  let otherAbortedAtReload;
  reload.mockImplementation(() => {
    otherAbortedAtReload = reads().find(([url]) => fieldOf(url) === 'other')[1].signal.aborted;
  });
  // `other` has a read in flight when the action lands, and `third` has its next read scheduled.
  fetch.mockImplementation(async (url, options = {}) => {
    if (options.method === 'POST') return response({ok: true, proposal_id: 7, status: 'cancelled', decision: ''});
    return fieldOf(url) === 'other' ? new Promise(() => {}) : response(payload());
  });
  mount(completed(), {other: payload(), third: payload()});
  await vi.advanceTimersByTimeAsync(3000);
  expect(reads().map(([url]) => fieldOf(url)).sort()).toEqual(['other', 'third']);
  inCard('field', '[data-proposal-action="reject"]').click();
  await vi.advanceTimersByTimeAsync(0);
  expect(reload).toHaveBeenCalledOnce();
  expect(otherAbortedAtReload).toBe(true);
  await vi.advanceTimersByTimeAsync(12000);
  expect(reads()).toHaveLength(2);
});

it('keeps the action busy while it posts and reloads without a post-action read', async () => {
  let finishAction;
  fetch.mockImplementation(() => new Promise(done => { finishAction = done; }));
  mount(completed());
  button('accept').click();
  await vi.advanceTimersByTimeAsync(0);
  expect(button('accept').disabled).toBe(true);
  button('accept').click();
  await vi.advanceTimersByTimeAsync(0);
  expect(fetch).toHaveBeenCalledTimes(1);

  finishAction(response({ok: true, proposal_id: 7, status: 'completed', decision: 'accepted'}));
  await vi.advanceTimersByTimeAsync(0);
  expect(fetch).toHaveBeenCalledTimes(1);
  expect(reload).toHaveBeenCalledOnce();
  expect(button('accept').disabled).toBe(true);
});

it('ignores a poll answer that arrives after the reload started', async () => {
  let finishPoll;
  fetch.mockImplementation(async (_url, options = {}) => options.method === 'POST'
    ? response({ok: true, proposal_id: 7, status: 'cancelled', decision: ''})
    : new Promise(done => { finishPoll = done; }));
  mount(completed(), {other: payload()});
  await vi.advanceTimersByTimeAsync(3000);
  expect(reads()).toHaveLength(1);
  const pollSignal = reads()[0][1].signal;
  inCard('field', '[data-proposal-action="reject"]').click();
  await vi.advanceTimersByTimeAsync(0);
  expect(reload).toHaveBeenCalledOnce();
  expect(pollSignal.aborted).toBe(true);

  // The stub ignores the abort, so only the reload latch keeps the late answer off the card.
  finishPoll(response(completed({candidate: 'late (Rear port)'})));
  await vi.advanceTimersByTimeAsync(3000);
  expect(inCard('other', '[data-proposal-candidate]').textContent).toBe('');
  expect(inCard('other', '[data-proposal-error]').textContent).toBe('');
  expect(reads()).toHaveLength(1);
});

it('lets one reload win when actions on two cards both succeed', async () => {
  const finish = {};
  fetch.mockImplementation(async (url, options = {}) => options.method === 'POST'
    ? new Promise(done => { finish[options.body.get('field_key')] = done; })
    : response(completed()));
  mount(completed(), {other: completed()});
  inCard('field', '[data-proposal-action="accept"]').click();
  inCard('other', '[data-proposal-action="reject"]').click();
  await vi.advanceTimersByTimeAsync(0);
  expect(Object.keys(finish).sort()).toEqual(['field', 'other']);

  finish.field(response({ok: true, proposal_id: 7, status: 'completed', decision: 'accepted'}));
  await vi.advanceTimersByTimeAsync(0);
  expect(reload).toHaveBeenCalledOnce();

  finish.other(response({ok: false, error: 'The preview is stale.'}));
  await vi.advanceTimersByTimeAsync(0);
  expect(reload).toHaveBeenCalledOnce();
  expect(inCard('other', '[data-proposal-error]').hidden).toBe(true);
  expect(inCard('other', '[data-proposal-action="reject"]').disabled).toBe(true);
  expect(reads()).toHaveLength(0);
});

it('refreshes an action refusal and shows the server reason', async () => {
  fetch.mockImplementation(async (_url, options) => options.method === 'POST'
    ? refused(409, 'The proposal is stale.')
    : response(completed()));
  mount(completed());
  button('accept').click();
  await vi.advanceTimersByTimeAsync(0);
  expect([node('error').hidden, node('error').textContent]).toEqual([false, 'The proposal is stale.']);
  expect(button('accept').disabled).toBe(false);
  expect(reload).not.toHaveBeenCalled();
});

it('stops polling a card whose claim the server refuses and shows why', async () => {
  fetch.mockImplementation(async () => refused(409, 'A newer preview replaced this one.'));
  mount(payload(), {other: payload()});
  await vi.advanceTimersByTimeAsync(3000);
  expect(reads()).toHaveLength(2);
  expect(inCard('field', '[data-proposal-error]').textContent).toBe('A newer preview replaced this one.');
  expect(vi.getTimerCount()).toBe(0);
  await vi.advanceTimersByTimeAsync(12000);
  expect(reads()).toHaveLength(2);
  expect(reload).not.toHaveBeenCalled();
});

it('retries a failed pending read at the next interval', async () => {
  fetch.mockRejectedValueOnce(new Error('The network is unavailable.'));
  mount();
  await vi.advanceTimersByTimeAsync(3000);
  expect(node('error').textContent).toBe('The network is unavailable.');
  await vi.advanceTimersByTimeAsync(3000);
  expect(node('badge').textContent).toBe('Proposal - not applied');
});

it('recovers controls when both an action and its refresh fail', async () => {
  fetch.mockRejectedValue(new Error('Offline'));
  mount(completed());
  button('accept').click();
  await vi.advanceTimersByTimeAsync(0);
  expect([button('accept').disabled, node('error').textContent]).toEqual([false, 'Offline']);
});

it('reports a non-JSON response instead of treating a login page as success', async () => {
  fetch.mockResolvedValue({ok: true, json: async () => { throw new SyntaxError('HTML'); }});
  mount();
  await vi.advanceTimersByTimeAsync(3000);
  expect(node('error').textContent).toBe('The proposal response could not be read. Reload the workspace.');
});

it('moves the field badge onto the class of the state the read reports', async () => {
  fetch.mockResolvedValue(response(completed({field_state: 'accepted', state_style: 'accepted'})));
  mount();
  expect(node('state').className).toBe('badge ndi-trace-state-proposed');
  await vi.advanceTimersByTimeAsync(3000);
  expect([node('state').className, node('state').textContent])
    .toEqual(['badge ndi-trace-state-accepted', 'accepted']);
});
