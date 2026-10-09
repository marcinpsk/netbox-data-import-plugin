/* SPDX-License-Identifier: Apache-2.0 */
/* SPDX-FileCopyrightText: 2026 Marcin Zieba <marcinpsk@gmail.com> */

function refreshCard(card) {
  // ruleid: nbdi-no-netbox-bundled-global
  window.htmx.ajax('GET', card.dataset.proposalRead, {source: card, target: card, swap: 'outerHTML'});
  // ruleid: nbdi-no-netbox-bundled-global
  htmx.process(card);
  // ok: nbdi-no-netbox-bundled-global
  card.dispatchEvent(new Event('ndi:read'));
}

function enhance(select) {
  // ruleid: nbdi-no-netbox-bundled-global
  if (window.TomSelect) return;
  // ruleid: nbdi-no-netbox-bundled-global
  new TomSelect(select, {});
  // ok: nbdi-no-netbox-bundled-global
  select.tomselect.addOption({value: 'a', text: 'A'});
}

function openModal(target) {
  // ok: nbdi-no-netbox-bundled-global
  window.Modal.getOrCreateInstance(target).show();
}

document.addEventListener('htmx:afterSwap', function (event) {
  // ok: nbdi-no-netbox-bundled-global
  return event.detail.xhr.status;
});
