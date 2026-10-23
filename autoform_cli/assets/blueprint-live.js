/* Copied into the generated site by autoform render. */
(function () {
  if (window.location.hostname !== "127.0.0.1" && window.location.hostname !== "localhost") return;
  if (window.__AUTOFORM_LIVE_OVERLAY_V1__) return;
  window.__AUTOFORM_LIVE_OVERLAY_V1__ = true;
  var endpoint = "/__autoform/live.json";
  var pollDelayMs = 3000;
  var panel = null, observer = null, pollTimer = null, lastPayload = null;
  var observedSource = null, observedLanding = null, paused = false, epoch = 0;

  function attachment() {
    var reader = document.querySelector(".bp-reading-body");
    // An active article takes precedence over the old SSR landing source,
    // even when that article has no landing introduction of its own.
    return {
      source: reader ? reader.querySelector(".bp-reading-source") : document.querySelector(".bp-reading-source"),
      landing: reader ? reader.querySelector(".bp-landing-intro") : document.querySelector(".bp-landing-intro") ||
        document.querySelector(".bp-dag-intro") || document.querySelector(".bp-landing")
    };
  }

  function clear() {
    document.querySelectorAll(".bp-live-badge").forEach(function (badge) { badge.remove(); });
    document.querySelectorAll(".bp-live-claimed").forEach(function (node) {
      node.classList.remove("bp-live-claimed");
    });
  }

  function badge(claim) {
    var value = document.createElement("span");
    value.className = "bp-live-badge";
    value.textContent = "claimed by " + claim.owner;
    if (claim.note) value.title = claim.note;
    return value;
  }

  function activity(payload, landing) {
    // A reader can clone an already enhanced source. Only this locally owned
    // panel survives; neither its clone nor a hidden SSR copy is live state.
    document.querySelectorAll(".bp-live-activity").forEach(function (copy) { if (copy !== panel) copy.remove(); });
    if (!landing) { if (panel) panel.remove(); return; }
    if (!panel) {
      panel = document.createElement("section");
      // A mathematical heading may legitimately own this native anchor.
      if (!document.getElementById("bp-live-activity")) panel.id = "bp-live-activity";
      panel.className = "bp-live-activity";
    }
    if (panel.parentNode !== landing) {
      var next = landing.querySelector(".bp-next-target");
      if (next && next.parentNode === landing) landing.insertBefore(panel, next);
      else landing.appendChild(panel);
    }
    panel.replaceChildren();
    var title = document.createElement("div");
    title.className = "bp-live-activity-title";
    title.textContent = "Live local work";
    panel.appendChild(title);
    if (payload.error) {
      var error = document.createElement("div");
      error.className = "bp-live-error";
      error.textContent = payload.error;
      panel.appendChild(error);
      return;
    }
    if (!payload.claims.length) {
      var empty = document.createElement("div");
      empty.className = "bp-live-empty";
      empty.textContent = "No active claims";
      panel.appendChild(empty);
      return;
    }
    payload.claims.forEach(function (claim) {
      var row = document.createElement("div");
      row.className = "bp-live-row";
      var name = document.createElement("strong");
      name.textContent = claim.title;
      row.appendChild(name);
      row.appendChild(document.createTextNode(" — " + claim.owner));
      panel.appendChild(row);
    });
  }

  function render(payload) {
    lastPayload = payload;
    var target = attachment(); observedSource = target.source; observedLanding = target.landing;
    clear();
    payload.claims.forEach(function (claim) {
      document.querySelectorAll("[data-autoform-node-id]").forEach(function (node) {
        if (node.dataset.autoformNodeId !== claim.node_id) return;
        node.classList.add("bp-live-claimed");
        var host = node.querySelector(".bp-thmheading") || node;
        host.appendChild(badge(claim));
      });
    });
    activity(payload, target.landing);
  }

  function watchReader() {
    if (observer || typeof MutationObserver === "undefined") return;
    observer = new MutationObserver(function () {
      if (paused) return;
      var target = attachment();
      // Own badge/row mutations do not start another render or request.
      if (target.source !== observedSource || target.landing !== observedLanding) {
        if (lastPayload) render(lastPayload);
        else {
          // A router cache may still contain a clone from before the service
          // disappeared. It must not resurrect stale local work on Back.
          observedSource = target.source; observedLanding = target.landing;
          clear(); activity({claims: []}, null);
        }
      }
    });
    observer.observe(document.querySelector(".bp-explorer-page") || document.body, {childList: true, subtree: true});
  }

  function disable(release) {
    lastPayload = null; observedSource = null; observedLanding = null;
    if (release && observer) { observer.disconnect(); observer = null; }
    clear(); activity({claims: []}, null);
  }

  function refresh() {
    var request = ++epoch;
    fetch(endpoint, { cache: "no-store", credentials: "same-origin" })
      .then(function (response) {
        if (paused || request !== epoch) return null;
        // Only autoform dashboard serves the overlay; another local server answers 404.
        if (response.status === 404) { disable(); return null; }
        if (!response.ok) throw new Error("live overlay unavailable");
        return response.json();
      })
      .then(function (payload) {
        if (paused || request !== epoch || !payload) return false;
        if (payload.schema !== "autoform-live/v1" || !Array.isArray(payload.claims)) { disable(); return false; }
        render(payload); watchReader();
        return true;
      })
      .catch(function () {
        if (paused || request !== epoch) return false;
        // A failed static-preview request is not evidence of a live service.
        if (lastPayload) render({ claims: [], error: "Live overlay unavailable" });
        return true;
      })
      .then(function (poll) { if (poll && !paused && request === epoch) pollTimer = window.setTimeout(refresh, pollDelayMs); });
  }

  window.addEventListener("pagehide", function () {
    paused = true; epoch++; window.clearTimeout(pollTimer); pollTimer = null; disable(true);
  });
  window.addEventListener("pageshow", function (event) { if (event.persisted && paused) { paused = false; refresh(); } });
  refresh();
})();
