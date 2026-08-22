(function () {
  function csrfToken() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute("content") || "" : "";
  }

  function injectForms() {
    var token = csrfToken();
    if (!token) return;
    document.querySelectorAll("form").forEach(function (form) {
      var method = (form.getAttribute("method") || "get").toLowerCase();
      if (method === "get") return;
      if (form.querySelector('input[name="csrf_token"]')) return;
      var input = document.createElement("input");
      input.type = "hidden";
      input.name = "csrf_token";
      input.value = token;
      form.appendChild(input);
    });
  }

  var originalFetch = window.fetch;
  window.fetch = function (input, init) {
    init = init || {};
    var method = (init.method || "GET").toUpperCase();
    if (method !== "GET" && method !== "HEAD" && method !== "OPTIONS" && method !== "TRACE") {
      var headers = new Headers(init.headers || {});
      if (!headers.has("X-CSRF-Token")) {
        headers.set("X-CSRF-Token", csrfToken());
      }
      init.headers = headers;
    }
    return originalFetch.call(this, input, init);
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", injectForms);
  } else {
    injectForms();
  }
})();
