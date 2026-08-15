(function () {
  function syncVersionField(form) {
    var stackSelect = form.querySelector("[data-stack-select]");
    var versionField = form.querySelector("[data-version-field]");
    var versionSelect = form.querySelector("[data-version-select]");
    if (!stackSelect || !versionField || !versionSelect) return;

    var opt = stackSelect.options[stackSelect.selectedIndex];
    var versions = (opt.getAttribute("data-versions") || "")
      .split(",")
      .map(function (v) {
        return v.trim();
      })
      .filter(Boolean);
    var def = opt.getAttribute("data-default") || "";

    versionSelect.innerHTML = "";
    if (!versions.length) {
      versionField.hidden = true;
      versionSelect.removeAttribute("required");
      versionSelect.value = "";
      return;
    }

    versions.forEach(function (v) {
      var o = document.createElement("option");
      o.value = v;
      o.textContent = v;
      if (v === def) o.selected = true;
      versionSelect.appendChild(o);
    });
    if (!def && versions.length) versionSelect.selectedIndex = versions.length - 1;
    versionField.hidden = false;
    versionSelect.setAttribute("required", "required");
  }

  function syncWordpressFields(form) {
    var stackSelect = form.querySelector("[data-stack-select]");
    var wpFields = form.querySelector("[data-wordpress-fields]");
    var wpPass = form.querySelector("[data-wp-pass]");
    if (!stackSelect || !wpFields) return;
    var isWp = stackSelect.value === "wordpress";
    wpFields.hidden = !isWp;
    if (wpPass) {
      if (isWp) {
        wpPass.removeAttribute("disabled");
        wpPass.setAttribute("required", "required");
      } else {
        wpPass.removeAttribute("required");
        wpPass.setAttribute("disabled", "disabled");
        wpPass.value = "";
      }
    }
  }

  function syncLaravelFields(form) {
    var stackSelect = form.querySelector("[data-stack-select]");
    var laravelFields = form.querySelector("[data-laravel-fields]");
    if (!stackSelect || !laravelFields) return;
    laravelFields.hidden = stackSelect.value !== "laravel";
  }

  document.querySelectorAll("[data-version-form]").forEach(function (form) {
    var stackSelect = form.querySelector("[data-stack-select]");
    if (!stackSelect) return;
    stackSelect.addEventListener("change", function () {
      syncVersionField(form);
      syncWordpressFields(form);
      syncLaravelFields(form);
    });
    syncVersionField(form);
    syncWordpressFields(form);
    syncLaravelFields(form);
  });
})();
