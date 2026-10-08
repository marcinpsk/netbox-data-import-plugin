/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

/* htmx swaps each termination card in place. A refused proposal command or read answers with the
 * workspace JSON envelope and not with a card, so this script shows that refusal in its card. */
(function () {
  if (window.ndiTraceProposals) return;
  window.ndiTraceProposals = true;

  function cardOf(event) {
    var source = event.detail.elt;
    return source && source.closest ? source.closest('[data-proposal-field]') : null;
  }

  function show(card, message) {
    var slot = card.querySelector('[data-proposal-error]');
    slot.textContent = message;
    slot.hidden = !message;
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
    if (event.detail.elt !== card) {
      show(card, '');
    } else if (card.hasAttribute('data-proposal-halted')) {
      // A refused claim stays refused, so asking again every interval would only repeat the refusal.
      event.preventDefault();
    }
  });

  document.addEventListener('htmx:responseError', function (event) {
    var card = cardOf(event);
    if (!card) return;
    var answer = refusal(event.detail.xhr);
    if (event.detail.elt === card || answer.code === 'preview_stale') {
      if (event.detail.xhr.status === 409) card.setAttribute('data-proposal-halted', '');
      show(card, answer.error);
      return;
    }
    // The action can lose a race with another operator, so the card first shows what the field holds now.
    // The refused request still holds its card until this event returns, and htmx would queue the read.
    setTimeout(function () {
      window.htmx.ajax('GET', card.dataset.proposalRead, {source: card, target: card, swap: 'outerHTML'})
        .then(function () { show(document.getElementById(card.id) || card, answer.error); });
    }, 0);
  });

  document.addEventListener('htmx:sendError', function (event) {
    var card = cardOf(event);
    if (card) show(card, 'The proposal request did not reach NetBox. Try again.');
  });
}());
