(function () {
  function makeOverlay() {
    var overlay = document.getElementById("ssl-overlay");
    if (overlay) return overlay;

    overlay = document.createElement("div");
    overlay.id = "ssl-overlay";
    overlay.className = "deploy-overlay";
    overlay.hidden = true;
    overlay.innerHTML =
      '<div class="deploy-modal" role="dialog" aria-modal="true" aria-labelledby="ssl-title">' +
      '<h2 id="ssl-title">Activating SSL</h2>' +
      '<p class="muted deploy-status" aria-live="polite">Starting…</p>' +
      '<div class="deploy-bar"><div class="deploy-bar-fill" style="width:0%"></div></div>' +
      '<pre class="deploy-log" aria-live="polite"></pre>' +
      '<div class="deploy-actions">' +
      '<a class="deploy-open" href="#" hidden>Open secure dashboard</a>' +
      '<button type="button" class="deploy-close" hidden>Close</button>' +
      "</div></div>";
    document.body.appendChild(overlay);
    overlay.querySelector(".deploy-close").addEventListener("click", function () {
      overlay.hidden = true;
      window.location.reload();
    });
    return overlay;
  }

  function update(overlay, progress, status) {
    overlay.querySelector(".deploy-bar-fill").style.width =
      Math.max(0, Math.min(100, progress || 0)) + "%";
    overlay.querySelector(".deploy-status").textContent = status;
  }

  function appendLogs(overlay, logs, seen) {
    var log = overlay.querySelector(".deploy-log");
    for (var i = seen; i < logs.length; i++) {
      log.textContent += "[" + logs[i].t + "] " + logs[i].msg + "\n";
    }
    log.scrollTop = log.scrollHeight;
    return logs.length;
  }

  function finish(overlay, button, job) {
    var failed = job.status === "error";
    update(
      overlay,
      job.progress || (failed ? 0 : 100),
      failed ? "SSL activation failed" : "SSL activated successfully"
    );
    overlay.querySelector(".deploy-close").hidden = false;
    button.disabled = false;

    if (!failed && job.result && job.result.url) {
      var open = overlay.querySelector(".deploy-open");
      open.href = job.result.url;
      open.hidden = false;
    }
  }

  function poll(overlay, button, jobId, seen) {
    fetch("/api/ssl/jobs/" + encodeURIComponent(jobId), {
      credentials: "same-origin",
    })
      .then(function (response) {
        return response.json();
      })
      .then(function (job) {
        if (!job.status) throw new Error(job.error || "Could not read SSL status");
        seen = appendLogs(overlay, job.logs || [], seen);
        if (job.status === "done" || job.status === "error") {
          finish(overlay, button, job);
          return;
        }
        update(overlay, job.progress || 0, "Activating SSL…");
        window.setTimeout(function () {
          poll(overlay, button, jobId, seen);
        }, 800);
      })
      .catch(function (error) {
        overlay.querySelector(".deploy-log").textContent +=
          "\nERROR: " + String(error.message || error);
        finish(overlay, button, { status: "error", progress: 0 });
      });
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("[data-ssl-activate]").forEach(function (button) {
      button.addEventListener("click", function () {
        button.disabled = true;
        var overlay = makeOverlay();
        overlay.hidden = false;
        overlay.querySelector(".deploy-log").textContent = "";
        overlay.querySelector(".deploy-open").hidden = true;
        overlay.querySelector(".deploy-close").hidden = true;
        update(overlay, 1, "Starting SSL activation…");

        fetch("/api/ssl/activate", {
          method: "POST",
          credentials: "same-origin",
        })
          .then(function (response) {
            return response.json();
          })
          .then(function (data) {
            if (!data.job_id) throw new Error(data.error || "Could not start SSL");
            poll(overlay, button, data.job_id, 0);
          })
          .catch(function (error) {
            overlay.querySelector(".deploy-log").textContent =
              "ERROR: " + String(error.message || error);
            finish(overlay, button, { status: "error", progress: 0 });
          });
      });
    });
  });
})();
