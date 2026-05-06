/**
 * Citation panel — Phase 6.
 *
 * Drop into chat.html with:
 *
 *     <script src="citation_panel.js" defer></script>
 *
 * Then call ``CitationPanel.bind(rootElement, {bearerToken})`` once
 * after rendering each agent response. The module:
 *
 * 1. Walks the response DOM looking for citation chips
 *    (anchors with ``data-citation-id``).
 * 2. Wires a click handler that fetches the preview PNG via the
 *    sidecar's ``GET /agent-api/v1/citations/{id}/preview.png`` route
 *    using the supplied bearer token.
 * 3. Opens a side panel with the rendered image + the citation's
 *    source id + section path + a deep link when the citation is a
 *    guideline.
 *
 * Why a small standalone module rather than inlined JS:
 *
 * - The chat.html is large enough that adding 200 lines of citation
 *   logic to it would scatter the contract.
 * - This module is the only place that knows about the citation
 *   preview endpoint shape; a future endpoint change is one file.
 *
 * The module is pure browser JavaScript (ES2022) — no transpile, no
 * bundler. The chat.html is served as a static asset and the client
 * runs everything in the page.
 */

(function () {
  'use strict';

  function buildPanelElement() {
    const panel = document.createElement('aside');
    panel.id = 'citation-panel';
    panel.setAttribute('role', 'complementary');
    panel.style.position = 'fixed';
    panel.style.top = '0';
    panel.style.right = '0';
    panel.style.width = '420px';
    panel.style.height = '100%';
    panel.style.background = '#fff';
    panel.style.borderLeft = '1px solid #d6d8db';
    panel.style.boxShadow = '-2px 0 8px rgba(0,0,0,0.08)';
    panel.style.overflowY = 'auto';
    panel.style.padding = '16px';
    panel.style.zIndex = '9999';
    panel.style.display = 'none';
    panel.innerHTML = `
      <button id="citation-close" type="button"
              aria-label="Close citation panel"
              style="float:right;border:none;background:none;font-size:20px;cursor:pointer;">&times;</button>
      <h2 style="margin-top:0;">Citation</h2>
      <div id="citation-meta" style="font-size:13px;color:#5b6573;"></div>
      <img id="citation-image" alt=""
           style="width:100%;margin-top:12px;border:1px solid #d6d8db;border-radius:4px;" />
      <div id="citation-status" role="status" aria-live="polite" style="margin-top:8px;color:#a73a35;"></div>
    `;
    document.body.appendChild(panel);

    const close = panel.querySelector('#citation-close');
    close.addEventListener('click', function () {
      panel.style.display = 'none';
    });

    return panel;
  }

  async function loadPreview({citationId, bearerToken, sidecarBaseUrl, panel}) {
    const status = panel.querySelector('#citation-status');
    const image = panel.querySelector('#citation-image');
    const meta = panel.querySelector('#citation-meta');

    status.textContent = '';
    image.src = '';
    meta.textContent = `Loading citation ${citationId}...`;

    const url = `${sidecarBaseUrl.replace(/\/$/, '')}/agent-api/v1/citations/${encodeURIComponent(citationId)}/preview.png`;

    try {
      const response = await fetch(url, {
        headers: {Authorization: `Bearer ${bearerToken}`},
        credentials: 'omit',
      });
      if (!response.ok) {
        throw new Error(`status=${response.status}`);
      }
      const blob = await response.blob();
      const objectUrl = URL.createObjectURL(blob);
      image.src = objectUrl;
      meta.textContent = `Citation ${citationId}`;
    } catch (err) {
      status.textContent = `Could not load citation: ${err.message}. ` +
        'Check that your session is still active.';
    }
  }

  function bind(root, options) {
    const opts = options || {};
    const sidecarBaseUrl = opts.sidecarBaseUrl || window.location.origin;
    const bearerToken = opts.bearerToken;
    if (!bearerToken) {
      // Fall through silently rather than throwing — the chat UI
      // surfaces auth errors elsewhere; we just no-op until a token
      // is wired in.
      return;
    }

    const panel = document.getElementById('citation-panel') || buildPanelElement();

    const chips = root.querySelectorAll('[data-citation-id]');
    chips.forEach(function (chip) {
      if (chip.dataset.citationBound === '1') {
        return;
      }
      chip.dataset.citationBound = '1';
      chip.style.cursor = 'pointer';
      chip.setAttribute('role', 'button');
      chip.setAttribute('tabindex', '0');
      chip.addEventListener('click', function (event) {
        event.preventDefault();
        panel.style.display = 'block';
        loadPreview({
          citationId: chip.dataset.citationId,
          bearerToken: bearerToken,
          sidecarBaseUrl: sidecarBaseUrl,
          panel: panel,
        });
      });
      chip.addEventListener('keydown', function (event) {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          chip.click();
        }
      });
    });
  }

  window.CitationPanel = {bind: bind};
})();
