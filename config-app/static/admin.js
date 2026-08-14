// Live-Aktualisierung von Validierungsstatus, Backend-Health und Logs.
(function () {
  "use strict";

  function dotClass(status) {
    var s = String(status || "unknown").trim().toLowerCase();
    var bucket = "unknown";
    if (s.indexOf("up") === 0) bucket = "up";
    else if (s.indexOf("down") === 0) bucket = "down";
    else if (s.indexOf("maint") === 0 || s.indexOf("resolution") !== -1) bucket = "maint";
    else if (s.indexOf("no check") !== -1) bucket = "no_check";
    else if (s === "disabled" || s === "none") bucket = s;
    return "dot dot-" + bucket;
  }

  // ---- Status-Banner + Backend-Status ----
  async function pollState() {
    try {
      const res = await fetch("/admin/state", { credentials: "same-origin" });
      if (!res.ok) return;
      const data = await res.json();
      updateBanner(data.validation);
      (data.services || []).forEach(function (s) {
        const el = document.getElementById("st-" + s.index);
        if (el) {
          el.className = dotClass(s.status);
          el.title = s.status + (s.check ? " (" + s.check + ")" : "");
        }
      });
    } catch (e) { /* Netzfehler ignorieren, naechster Tick versucht es erneut */ }
  }

  function updateBanner(v) {
    const banner = document.getElementById("val-banner");
    const text = document.getElementById("val-text");
    const msg = document.getElementById("val-message");
    if (!banner || !v) return;

    let cls = "banner ", label = "";
    if (!v.is_current) {
      cls += "banner-pending";
      label = "Validierung läuft … (HAProxy prüft die neue Config)";
      msg.textContent = "";
    } else if (v.ok) {
      cls += "banner-ok";
      label = "Konfiguration gültig und aktiv ✓";
      msg.textContent = "";
    } else {
      cls += "banner-err";
      label = "Konfiguration abgelehnt – HAProxy läuft mit der letzten gültigen Version weiter ✗";
      msg.textContent = v.message || "";
    }
    banner.className = cls;
    text.textContent = label;
  }

  // ---- Logs ----
  const logbox = document.getElementById("logbox");
  const whichSel = document.getElementById("log-which");
  const linesSel = document.getElementById("log-lines");
  const autoChk = document.getElementById("log-auto");

  async function pollLogs() {
    if (!logbox) return;
    try {
      const which = whichSel.value;
      const lines = linesSel.value;
      const res = await fetch("/admin/logs?which=" + which + "&lines=" + lines,
        { credentials: "same-origin" });
      if (!res.ok) return;
      const data = await res.json();
      const atBottom = logbox.scrollHeight - logbox.scrollTop - logbox.clientHeight < 40;
      logbox.textContent = data.text || "(leer)";
      if (atBottom) logbox.scrollTop = logbox.scrollHeight;
    } catch (e) { /* ignorieren */ }
  }

  if (document.getElementById("log-refresh")) {
    document.getElementById("log-refresh").addEventListener("click", pollLogs);
    whichSel.addEventListener("change", function () { logbox.textContent = "Lade …"; pollLogs(); });
    linesSel.addEventListener("change", pollLogs);
  }

  // ---- Update-Status / Audit ----
  const auditBox = document.getElementById("upd-audit-box");
  async function pollUpdate() {
    if (!auditBox) return;
    try {
      const res = await fetch("/admin/update/state", { credentials: "same-origin" });
      if (!res.ok) return;
      const d = await res.json();
      const atBottom = auditBox.scrollHeight - auditBox.scrollTop - auditBox.clientHeight < 40;
      auditBox.textContent = d.audit || "(noch keine Audit-Einträge)";
      if (atBottom) auditBox.scrollTop = auditBox.scrollHeight;
      // dezenter Hinweis am Versions-Badge, solange eine Anforderung offen ist
      const cur = document.getElementById("upd-current");
      if (cur) cur.title = d.pending ? "Update angefordert – läuft …"
        : (d.result ? ("letztes Ergebnis: " + (d.result.state || "")) : "");
    } catch (e) { /* ignorieren */ }
  }

  // ---- Timer ----
  pollState();
  pollLogs();
  pollUpdate();
  setInterval(pollState, 3000);
  setInterval(function () { if (!autoChk || autoChk.checked) pollLogs(); }, 3000);
  setInterval(pollUpdate, 3000);
})();
