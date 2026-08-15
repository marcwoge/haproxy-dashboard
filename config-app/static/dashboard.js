// Live-Aktualisierung der Backend-Status-Punkte auf dem Dashboard.
(function () {
  "use strict";
  var grid = document.querySelector(".grid[data-show-status='1']");
  if (!grid) return;

  function apply(list) {
    (list || []).forEach(function (s) {
      var tile = grid.querySelector('.tile[data-path="' + (s.path || "").replace(/"/g, '\\"') + '"]');
      if (!tile) return;
      var dot = tile.querySelector(".tile-status");
      if (!dot) return;
      dot.className = "tile-status dot dot-" + (s.dot || "unknown");
      dot.title = "Status: " + (s.dot || "unbekannt");
    });
  }

  async function poll() {
    try {
      var res = await fetch("/status", { credentials: "same-origin" });
      if (!res.ok) return;
      var data = await res.json();
      apply(data.services);
    } catch (e) { /* still ignorieren */ }
  }

  poll();
  setInterval(poll, 5000);
})();
