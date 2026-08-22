(function () {
  function makeOverlay(title) {
    var overlay = document.getElementById("updates-overlay");
    if (!overlay) {
      overlay = document.createElement("div");
      overlay.id = "updates-overlay";
      overlay.className = "deploy-overlay";
      overlay.hidden = true;
      overlay.innerHTML =
        '<div class="deploy-modal" role="dialog" aria-modal="true" aria-labelledby="updates-title">' +
        '<h2 id="updates-title">Updating</h2>' +
        '<p class="muted deploy-status" aria-live="polite">Starting…</p>' +
        '<div class="deploy-bar"><div class="deploy-bar-fill" style="width:0%"></div></div>' +
        '<pre class="deploy-log" aria-live="polite"></pre>' +
        '<div class="deploy-actions">' +
        '<button type="button" class="deploy-close" hidden>Close</button>' +
        "</div></div>";
      document.body.appendChild(overlay);
      overlay.querySelector(".deploy-close").addEventListener("click", function () {
        overlay.hidden = true;
        window.location.href = "/?refresh_updates=1";
      });
    }
    overlay.querySelector("#updates-title").textContent = title || "Updating";
    return overlay;
  }

  function update(overlay, progress, status) {
    overlay.querySelector(".deploy-bar-fill").style.width =
      Math.max(0, Math.min(100, progress || 0)) + "%";
    if (status) overlay.querySelector(".deploy-status").textContent = status;
  }

  function appendLogs(overlay, logs, seen) {
    var log = overlay.querySelector(".deploy-log");
    for (var i = seen; i < logs.length; i++) {
      log.textContent += "[" + logs[i].t + "] " + logs[i].msg + "\n";
    }
    log.scrollTop = log.scrollHeight;
    return logs.length;
  }

  function poll(overlay, jobId, seen) {
    fetch("/api/updates/jobs/" + encodeURIComponent(jobId), {
      credentials: "same-origin",
    })
      .then(function (r) {
        return r.json();
      })
      .then(function (job) {
        if (job.error && !job.status) throw new Error(job.error);
        seen = appendLogs(overlay, job.logs || [], seen);
        if (job.status === "done" || job.status === "error") {
          update(
            overlay,
            job.progress || (job.status === "error" ? 0 : 100),
            job.status === "error" ? "Update failed" : "Update finished"
          );
          overlay.querySelector(".deploy-close").hidden = false;
          return;
        }
        update(overlay, job.progress || 0, "Running…");
        window.setTimeout(function () {
          poll(overlay, jobId, seen);
        }, 1000);
      })
      .catch(function (err) {
        overlay.querySelector(".deploy-log").textContent +=
          "\nERROR: " + String(err.message || err);
        update(overlay, 0, "Update failed");
        overlay.querySelector(".deploy-close").hidden = false;
      });
  }

  function start(apiPath, title, confirmMsg) {
    if (confirmMsg && !window.confirm(confirmMsg)) return;
    var overlay = makeOverlay(title);
    overlay.hidden = false;
    overlay.querySelector(".deploy-log").textContent = "";
    overlay.querySelector(".deploy-close").hidden = true;
    update(overlay, 1, "Starting…");
    fetch(apiPath, { method: "POST", credentials: "same-origin" })
      .then(function (r) {
        return r.json();
      })
      .then(function (data) {
        if (!data.job_id) throw new Error(data.error || "Could not start update");
        poll(overlay, data.job_id, 0);
      })
      .catch(function (err) {
        overlay.querySelector(".deploy-log").textContent =
          "ERROR: " + String(err.message || err);
        update(overlay, 0, "Update failed");
        overlay.querySelector(".deploy-close").hidden = false;
      });
  }

  document.addEventListener("DOMContentLoaded", function () {
    var osBtn = document.querySelector("[data-os-upgrade]");
    if (osBtn) {
      osBtn.addEventListener("click", function () {
        start(
          "/api/updates/os",
          "Host OS updates",
          "Apply available host OS package updates now? This can take several minutes."
        );
      });
    }
    var panelBtn = document.querySelector("[data-panel-upgrade]");
    if (panelBtn) {
      panelBtn.addEventListener("click", function () {
        start(
          "/api/updates/panel",
          "mrmpanel upgrade",
          "Upgrade mrmpanel from the release mirror? A backup runs first, then the panel restarts."
        );
      });
    }
  });
})();
