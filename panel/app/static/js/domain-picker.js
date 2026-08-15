(function () {
  function updateSubdomain(form) {
    var kind = form.querySelector("[data-site-kind]");
    var field = form.querySelector("[data-subdomain-field]");
    var input = form.querySelector("[data-subdomain-input]");
    if (!kind || !field || !input) return;
    var enabled = kind.value === "subdomain";
    field.hidden = !enabled;
    input.required = enabled;
    input.disabled = !enabled;
  }

  function updateDomains(form) {
    var owner = form.querySelector("[data-owner-select]");
    var domains = form.querySelector("[data-domain-select]");
    if (!owner || !domains) return;
    var first = null;
    Array.prototype.forEach.call(domains.options, function (option) {
      var show = option.getAttribute("data-owner") === owner.value;
      option.hidden = !show;
      option.disabled = !show;
      if (show && !first) first = option;
    });
    if (first) domains.value = first.value;
    var submit = form.querySelector('button[type="submit"]');
    if (submit) submit.disabled = !first;
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("[data-domain-form]").forEach(function (form) {
      var owner = form.querySelector("[data-owner-select]");
      var kind = form.querySelector("[data-site-kind]");
      if (owner) {
        owner.addEventListener("change", function () {
          updateDomains(form);
        });
      }
      if (kind) {
        kind.addEventListener("change", function () {
          updateSubdomain(form);
        });
      }
      updateDomains(form);
      updateSubdomain(form);
    });
  });
})();
