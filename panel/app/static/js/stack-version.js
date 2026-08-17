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

  function syncVersionField(form) {
    var versionField = form.querySelector("[data-version-field]");
    var versionSelect = form.querySelector("[data-version-select]");
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
      syncWordpressFields(form);
      syncLaravelFields(form);
    }
    if (stackSelect.tagName === "SELECT") {
      stackSelect.addEventListener("change", syncStack);
    }
    form.querySelectorAll("[data-stack-choice]").forEach(function (choice) {
      choice.addEventListener("change", syncStack);
    });
    syncStack();
  });
})();
