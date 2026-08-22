(function () {
  function selectedStack(form) {
    var choice = form.querySelector("[data-stack-choice]:checked");
    var stackInput = form.querySelector("[data-stack-select]");
    if (choice && stackInput) {
      stackInput.value = choice.value;
      return choice;
    }
    if (stackInput && stackInput.options) {
      return stackInput.options[stackInput.selectedIndex];
    }
    return null;
  }

  function parseAppVersions(opt) {
    var raw = opt.getAttribute("data-app-versions") || "[]";
    try {
      var parsed = JSON.parse(raw);
      if (!Array.isArray(parsed)) return [];
      return parsed
        .map(function (entry) {
          if (!entry || !entry.id) return null;
          return {
            id: String(entry.id),
            php: (entry.php || []).map(String),
          };
        })
        .filter(Boolean);
    } catch (e) {
      return [];
    }
  }

  function syncVersionField(form) {
    var versionField = form.querySelector("[data-version-field]");
    var versionSelect = form.querySelector("[data-version-select]");
    var versionLabel = form.querySelector("[data-version-label]");
    if (!versionField || !versionSelect) return;

    var opt = selectedStack(form);
    if (!opt) return;
    var versions = (opt.getAttribute("data-versions") || "")
      .split(",")
      .map(function (v) {
        return v.trim();
      })
      .filter(Boolean);
    var def = opt.getAttribute("data-default") || "";
    var apps = parseAppVersions(opt);

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
    if (versionLabel) {
      versionLabel.textContent = apps.length ? "PHP version" : "Runtime version";
    }
  }

  function syncAppVersionField(form) {
    var appField = form.querySelector("[data-app-version-field]");
    var appSelect = form.querySelector("[data-app-version-select]");
    var appLabel = form.querySelector("[data-app-version-label]");
    if (!appField || !appSelect) return;

    var opt = selectedStack(form);
    if (!opt) return;
    var apps = parseAppVersions(opt);
    var defApp = opt.getAttribute("data-default-app") || "";
    var label = opt.getAttribute("data-app-label") || "App version";
    var versionSelect = form.querySelector("[data-version-select]");
    var php = versionSelect ? versionSelect.value : "";

    appSelect.innerHTML = "";
    if (!apps.length) {
      appField.hidden = true;
      appSelect.removeAttribute("required");
      appSelect.value = "";
      return;
    }

    var compatible = apps.filter(function (a) {
      return !php || a.php.indexOf(php) !== -1;
    });
    if (!compatible.length) {
      appField.hidden = true;
      appSelect.removeAttribute("required");
      appSelect.value = "";
      return;
    }

    compatible.forEach(function (a) {
      var o = document.createElement("option");
      o.value = a.id;
      o.textContent = a.id;
      if (a.id === defApp) o.selected = true;
      appSelect.appendChild(o);
    });
    if (!appSelect.value && compatible.length) {
      var prefer = compatible.find(function (a) {
        return a.id === defApp;
      });
      appSelect.value = prefer ? prefer.id : compatible[0].id;
    }
    if (appLabel) appLabel.textContent = label;
    appField.hidden = false;
    appSelect.setAttribute("required", "required");
  }

  function syncWordpressFields(form) {
    var wpFields = form.querySelector("[data-wordpress-fields]");
    var wpPass = form.querySelector("[data-wp-pass]");
    var stack = selectedStack(form);
    if (!stack || !wpFields) return;
    var isWp = stack.value === "wordpress";
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
    var laravelFields = form.querySelector("[data-laravel-fields]");
    var stack = selectedStack(form);
    if (!stack || !laravelFields) return;
    laravelFields.hidden = stack.value !== "laravel";
  }

  document.querySelectorAll("[data-version-form]").forEach(function (form) {
    var stackSelect = form.querySelector("[data-stack-select]");
    if (!stackSelect) return;
    function syncStack() {
      syncVersionField(form);
      syncAppVersionField(form);
      syncWordpressFields(form);
      syncLaravelFields(form);
    }
    if (stackSelect.tagName === "SELECT") {
      stackSelect.addEventListener("change", syncStack);
    }
    form.querySelectorAll("[data-stack-choice]").forEach(function (choice) {
      choice.addEventListener("change", syncStack);
    });
    var versionSelect = form.querySelector("[data-version-select]");
    if (versionSelect) {
      versionSelect.addEventListener("change", function () {
        syncAppVersionField(form);
      });
    }
    syncStack();
  });
})();
