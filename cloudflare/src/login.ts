/** Anmeldeseite des Dashboards. Eigenes Formular statt des Browser-Dialogs von Basic-Auth. */

function escapeHtml(s: string): string {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c] as string);
}

const STYLE = `
:root { --bg: #fbfbfd; --card: #ffffff; --text: #1d1d1f; --muted: #6e6e73; --line: #d2d2d7; --blue: #0071e3; --red: #d70015; color-scheme: light dark; }
@media (prefers-color-scheme: dark) { :root { --bg: #000000; --card: #1c1c1e; --text: #f5f5f7; --muted: #98989d; --line: #38383a; --blue: #2997ff; --red: #ff453a; } }
* { box-sizing: border-box; }
html, body { margin: 0; height: 100%; }
body { background: var(--bg); color: var(--text); font: 17px/1.47 -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", system-ui, sans-serif;
  display: grid; place-items: center; padding: 24px 16px; -webkit-font-smoothing: antialiased; }
main { width: 100%; max-width: 360px; text-align: center; }
svg { width: 72px; height: 72px; border-radius: 16px; box-shadow: 0 12px 40px rgba(0,0,0,.12); }
h1 { font-size: 34px; line-height: 1.1; font-weight: 700; letter-spacing: -0.03em; margin: 28px 0 8px; }
p.sub { color: var(--muted); margin: 0 0 32px; }
form { display: grid; gap: 12px; text-align: left; }
label span { display: block; font-size: 13px; color: var(--muted); margin: 0 0 6px 4px; }
input { width: 100%; font: inherit; color: var(--text); background: var(--card); border: 1px solid var(--line); border-radius: 12px;
  padding: 14px 16px; min-height: 52px; transition: border-color .2s ease, box-shadow .2s ease; }
input:focus { outline: none; border-color: var(--blue); box-shadow: 0 0 0 4px color-mix(in srgb, var(--blue) 22%, transparent); }
button { margin-top: 12px; font: inherit; font-weight: 600; color: #fff; background: var(--blue); border: 0; border-radius: 980px;
  min-height: 52px; cursor: pointer; transition: opacity .2s ease; }
button:hover { opacity: .9; }
button:focus-visible { outline: 3px solid color-mix(in srgb, var(--blue) 45%, transparent); outline-offset: 3px; }
.error { color: var(--red); font-size: 15px; margin: 0 0 4px; text-align: center; }
`;

const ICON = `<svg viewBox="0 0 512 512" aria-hidden="true"><rect width="512" height="512" rx="112" fill="#121419"/><g transform="translate(64 80) scale(12)">
<rect x="1" y="14" width="4" height="9" rx="1" fill="#8c92a0"/><rect x="7.5" y="10" width="4" height="13" rx="1" fill="#8c92a0"/>
<rect x="16.5" y="9" width="4" height="14" rx="1" fill="#5db4f5"/><rect x="23" y="5" width="4" height="18" rx="1" fill="#5db4f5"/>
<rect x="0" y="25.5" width="28" height="1.5" rx=".75" fill="#e9e7e2"/></g></svg>`;

export function loginPage(error: string | null, user = ""): string {
  return `<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#fbfbfd" media="(prefers-color-scheme: light)"><meta name="theme-color" content="#000000" media="(prefers-color-scheme: dark)">
<title>Anmelden – Trading Bot</title><style>${STYLE}</style></head><body><main>
${ICON}
<h1>Trading Bot.</h1>
<p class="sub">Melde dich an, um deinen Bot zu sehen.</p>
<form method="post" action="/login">
${error ? `<p class="error" role="alert">${escapeHtml(error)}</p>` : ""}
<label><span>Benutzer</span><input name="user" autocomplete="username" autocapitalize="none" spellcheck="false" required value="${escapeHtml(user)}"></label>
<label><span>Passwort</span><input name="password" type="password" autocomplete="current-password" required></label>
<button type="submit">Anmelden</button>
</form></main></body></html>`;
}
