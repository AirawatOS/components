// A second screen module — proves multiple imported files, not just one.
export function aboutScreen() {
  AOS.page('main', { title: 'About this app' },
    AOS.el('div', { class: 'card' },
      AOS.el('div', { style: 'padding:16px;line-height:1.6' }, [
        AOS.el('p', null, 'This app is split across three files — app.js + screens/accounts.js + screens/about.js — using native ES modules.'),
        AOS.el('p', { class: 'sub' }, 'No build step. It loads /shared/aos.css + aos.js (classic scripts), then app.js (type="module") imports each screen. app_host serves every file from the bundle; the certificate is a digest of the whole bundle; CSP script-src \'self\' permits same-origin module imports.'),
      ])));
}
