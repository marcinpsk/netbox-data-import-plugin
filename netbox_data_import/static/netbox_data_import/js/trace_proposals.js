/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

/* htmx swaps each termination card in place. A refused proposal command or read answers with the
 * workspace JSON envelope and not with a card, so this script shows that refusal in its card. It also
 * keeps a page that is not a card or a workspace, such as the login page, out of every workspace swap. */
(function () {
  if (window.ndiTraceProposals) return;
  window.ndiTraceProposals = true;

  // A read refused with one of these is refused again on the next interval, so the card stops polling.
  var HALTING = [401, 403, 404, 409];

  function closestTo(event, selector) {
    // htmx dispatches a swap event on its target, so the element that sent the request is in requestConfig.
    var source = event.detail.requestConfig ? event.detail.requestConfig.elt : event.detail.elt;
    return source && source.closest ? source.closest(selector) : null;
  }

  function cardOf(event) {
    return closestTo(event, '[data-proposal-field]');
  }

  function show(card, message) {
    var slot = card.querySelector('[data-proposal-error]');
    slot.textContent = message;
    slot.hidden = !message;
  }

  function halt(card, message) {
    card.setAttribute('data-proposal-halted', '');
    show(card, message);
  }

  function refusal(xhr) {
    try {
      var payload = JSON.parse(xhr.responseText);
      if (payload && typeof payload.error === 'string') return payload;
    } catch (_) {
      // Not the JSON envelope, for example a NetBox error page; the generic sentence follows.
    }
    return {error: 'The proposal request was refused.'};
  }

  document.addEventListener('htmx:beforeRequest', function (event) {
    var card = cardOf(event);
    if (!card) return;
    // htmx starts a poll with no event, and the read after a refusal with the ndi:read event.
    var asked = event.detail.requestConfig && event.detail.requestConfig.triggeringEvent;
    if (event.detail.elt !== card) {
      show(card, '');
    } else if (card.hasAttribute('data-proposal-halted') || (document.hidden && !asked)) {
      // A halted card stays quiet, and a hidden tab polls again on the first interval after it shows.
      event.preventDefault();
    }
  });

  document.addEventListener('htmx:beforeSwap', function (event) {
    var target = event.detail.target;
    if (!event.detail.shouldSwap || !closestTo(event, '.ndi-trace-workspace')) return;
    var card = target.matches('[data-proposal-field]') ? target : null;
    if (!card && target.id !== 'page-content') return;
    if (event.detail.xhr.responseText.indexOf(card ? 'data-proposal-field=' : 'id="page-content"') !== -1) return;
    // Another page came back, for example the login page after the session ended, so the browser opens it.
    event.detail.shouldSwap = false;
    if (card) halt(card, 'NetBox answered with another page. Opening it.');
    window.location.assign(event.detail.xhr.responseURL || window.location.href);
  });

  document.addEventListener('htmx:responseError', function (event) {
    var card = cardOf(event);
    if (!card && event.detail.xhr.status === 401 && closestTo(event, '.ndi-trace-workspace')) {
      // A workspace form has no card to show it in, and a page load leads to the login page.
      window.location.assign(window.location.href);
      return;
    }
    if (!card) return;
    var status = event.detail.xhr.status;
    var answer = refusal(event.detail.xhr);
    if (event.detail.elt === card) {
      if (HALTING.indexOf(status) === -1) show(card, answer.error);
      else halt(card, answer.error);
      return;
    }
    if (answer.code === 'preview_stale' || status === 401) {
      halt(card, answer.error);
      return;
    }
    // The action can lose a race with another operator, so the card first shows what the field holds now.
    // The refused request still holds its card until this event returns, so the read starts one task later.
    // NetBox does not set window.htmx, so the card's own hx-get answers the event that its hx-trigger names.
    show(card, answer.error);
    setTimeout(function () {
      card.dispatchEvent(new CustomEvent('ndi:read', {detail: {refusal: answer.error}}));
    }, 0);
  });

  // Only the swap that answers this read carries its refusal, so an aborted or replaced read drops it.
  // The same answer also swaps the active proposal count out of band, which has no refusal slot.
  document.addEventListener('htmx:afterSwap', function (event) {
    var read = event.detail.requestConfig ? event.detail.requestConfig.triggeringEvent : null;
    if (!read || read.type !== 'ndi:read' || !event.target.matches('[data-proposal-field]')) return;
    show(event.target, read.detail.refusal);
  });

  // Card answers can arrive out of order, so a count the database made earlier never replaces a later one.
  document.addEventListener('htmx:oobBeforeSwap', function (event) {
    if (event.detail.target.id !== 'ndiActiveProposals') return;
    var copy = event.detail.fragment.querySelector ? event.detail.fragment.querySelector('#ndiActiveProposals') : null;
    if (!copy) return;
    if (Number(copy.dataset.countedAt) < Number(event.detail.target.dataset.countedAt)) event.detail.shouldSwap = false;
  });

  document.addEventListener('htmx:sendError', function (event) {
    var card = cardOf(event);
    if (card) show(card, 'The proposal request did not reach NetBox. Try again.');
  });
}());
