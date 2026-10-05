/* Copied into the generated site by autoform render. */
(function () {
  if (window.location.hostname !== "127.0.0.1" && window.location.hostname !== "localhost") return;
  var endpoint = "/__autoform/live.json";
  var pollDelayMs = 3000;

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

  function activity(payload) {
    var landing = document.querySelector(".bp-landing");
    if (!landing) return;
    var panel = document.getElementById("bp-live-activity");
    if (!panel) {
      panel = document.createElement("section");
      panel.id = "bp-live-activity";
      panel.className = "bp-live-activity";
      var map = landing.querySelector(".bp-map");
      if (map && map.parentNode === landing) landing.insertBefore(panel, map);
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
    clear();
    payload.claims = payload.claims || [];
    payload.claims.forEach(function (claim) {
      document.querySelectorAll("[data-autoform-node-id]").forEach(function (node) {
        if (node.dataset.autoformNodeId !== claim.node_id) return;
        node.classList.add("bp-live-claimed");
        var host = node.querySelector(".bp-thmheading") || node;
        host.appendChild(badge(claim));
      });
    });
    activity(payload);
  }

  function refresh() {
    fetch(endpoint, { cache: "no-store" })
      .then(function (response) {
        if (!response.ok) throw new Error("live overlay unavailable");
        return response.json();
      })
      .then(render)
      .catch(function () { render({ claims: [], error: "Live overlay unavailable" }); })
      .then(function () { window.setTimeout(refresh, pollDelayMs); });
  }

  refresh();
})();
