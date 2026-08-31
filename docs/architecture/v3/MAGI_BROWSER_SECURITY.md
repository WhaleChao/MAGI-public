# MAGI V3 Browser Security Contract

The legal business portals keep their existing login, selector, CAPTCHA and
download behavior. Security controls are applied around those flows:

- Chromium's process sandbox is never bypassed in production source.
- Playwright and browser payload installation is a candidate build step.
  A process bound to `MAGI_V3_RELEASE_MANIFEST` cannot install at runtime.
- LAF, file-review and transcript automation receive separate logical browser
  profile IDs and the minimum credential-name declarations for that portal.
- The shared Playwright wrapper restricts top-level navigations and popups to
  the component's declared hosts. Official portal subresources remain allowed
  so required CDN, CAPTCHA and static assets continue to load; browser cookies
  remain origin-scoped.
- Localhost enters an allowlist only when the caller explicitly supplies a
  localhost mock URL.

These checks are release acceptance controls. A successful source test does
not replace post-cutover LIVE login, query and download probes.
