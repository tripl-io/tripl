// Starts Swagger UI on /docs. A file of its own rather than an inline <script>,
// so the page runs under script-src 'self' with no hash or nonce to keep in
// step. The page puts the document's URL (root path included) on the element.
(function () {
  var element = document.getElementById("swagger-ui");
  window.ui = SwaggerUIBundle({
    url: element.getAttribute("data-openapi-url"),
    dom_id: "#swagger-ui",
    layout: "BaseLayout",
    deepLinking: true,
    showExtensions: true,
    showCommonExtensions: true,
    // The default posts the document to validator.swagger.io for a badge: a
    // third-party request the page's policy refuses, and none of its business.
    validatorUrl: null,
    presets: [SwaggerUIBundle.presets.apis],
  });
})();
