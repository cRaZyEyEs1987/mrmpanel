(function () {
  function el(tag, className, text) {
    var n = document.createElement(tag);
    if (className) n.className = className;
    if (text != null) n.textContent = text;
    return n;
  }

  function ensureOverlay() {
    var existing = document.getElementById("deploy-overlay");
    if (existing) return existing;
    var overlay = el("div", "deploy-overlay");
    overlay.id = "deploy-overlay";
    overlay.hidden = true;
    overlay.innerHTML =
      '<div class="deploy-modal" role="dialog" aria-modal="true" aria-labelledby="deploy-title">' +
      '<h2 id="deploy-title">Deploying site</h2>' +
      '<p class="muted deploy-status">Starting…</p>' +
      '<div class="deploy-bar"><div class="deploy-bar-fill" style="width:0%"></div></div>' +
      '<pre class="deploy-log" aria-live="polite"></pre>' +
      '<div class="deploy-actions">' +
      '<a class="deploy-open" href="#" target="_blank" rel="noopener" hidden>Open site</a>' +
      '<button type="button" class="deploy-close" hidden>Close</button>' +
      "</div></div>";
    document.body.appendChild(overlay);
    overlay.querySelector(".deploy-close").addEventListener("click", function () {
      overlay.hidden = true;
      window.location.reload();
    });
    return overlay;
  }

  function setProgress(overlay, pct, status) {
    var fill = overlay.querySelector(".deploy-bar-fill");
    var st = overlay.querySelector(".deploy-status");
    fill.style.width = Math.max(0, Math.min(100, pct || 0)) + "%";
    if (status) st.textContent = status;
  }

  function appendLogs(overlay, logs, seen) {
    var pre = overlay.querySelector(".deploy-log");
    for (var i = seen; i < logs.length; i++) {
      var line = logs[i];
      pre.textContent += "[" + line.t + "] " + line.msg + "\n";
    }
    pre.scrollTop = pre.scrollHeight;
    return logs.length;
  }

  function wireForm(form, apiStart) {
    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      var btn = form.querySelector('button[type="submit"]');
      if (btn) btn.disabled = true;

      var overlay = ensureOverlay();
      overlay.hidden = false;
      overlay.querySelector(".deploy-log").textContent = "";
      overlay.querySelector(".deploy-open").hidden = true;
      overlay.querySelector(".deploy-close").hidden = true;
      setProgress(overlay, 1, "Starting deploy…");

      var body = new FormData(form);
      fetch(apiStart, {
        method: "POST",
        body: body,
        credentials: "same-origin",
      })
        .then(function (r) {
          return r.json();
        })
        .then(function (data) {
          if (!data.job_id) {
            throw new Error(data.error || "Could not start deploy");
          }
          poll(overlay, data.job_id, 0, btn);
        })
        .catch(function (err) {
          setProgress(overlay, 0, "Failed");
          overlay.querySelector(".deploy-log").textContent = String(err.message || err);
          overlay.querySelector(".deploy-close").hidden = false;
          if (btn) btn.disabled = false;
        });
    });
  }

  function poll(overlay, jobId, seen, btn) {
    fetch("/api/sites/deploy/" + encodeURIComponent(jobId), {
      credentials: "same-origin",
    })
      .then(function (r) {
        return r.json();
      })
      .then(function (job) {
        if (job.error && !job.status) {
          throw new Error(job.error);
        }
        var logs = job.logs || [];
        seen = appendLogs(overlay, logs, seen);
        setProgress(
          overlay,
          job.progress || 0,
          job.status === "done"
            ? "Deploy complete"
            : job.status === "error"
              ? "Deploy failed"
              : "Deploying…"
        );

        if (job.status === "done") {
          var open = overlay.querySelector(".deploy-open");
          var url =
            (job.result && job.result.url) ||
            (job.meta && job.meta.domain ? "http://" + job.meta.domain : "");
          if (url) {
            open.href = url;
            open.hidden = false;
            open.textContent = "Open " + url;
          }
          overlay.querySelector(".deploy-close").hidden = false;
          if (btn) btn.disabled = false;
          return;
        }
        if (job.status === "error") {
          overlay.querySelector(".deploy-close").hidden = false;
          if (btn) btn.disabled = false;
          return;
        }
        setTimeout(function () {
          poll(overlay, jobId, seen, btn);
        }, 700);
      })
      .catch(function (err) {
        setProgress(overlay, 0, "Failed");
        overlay.querySelector(".deploy-log").textContent +=
          "\n" + String(err.message || err);
        overlay.querySelector(".deploy-close").hidden = false;
        if (btn) btn.disabled = false;
      });
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("form[data-deploy-api]").forEach(function (form) {
      wireForm(form, form.getAttribute("data-deploy-api"));
    });
  });
})();
