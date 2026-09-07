const esc = (s) =>
  String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

export function page(title, body) {
  return `<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>${esc(title)}</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; background: #101114; color: #e9e9ec;
    font: 16px/1.5 -apple-system, Segoe UI, Roboto, sans-serif; }
  main { max-width: 640px; margin: 0 auto; padding: 32px 20px 80px; }
  h1 { font-size: 22px; margin: 0 0 2px; }
  .tag { color: #9aa0a6; margin: 0 0 28px; }
  h2 { font-size: 15px; text-transform: uppercase; letter-spacing: .06em; color: #9aa0a6; margin: 32px 0 12px; }
  .card { border: 1px solid #2a2c31; border-radius: 12px; padding: 16px; margin: 10px 0; background: #16171b; }
  .pick { border-color: #1db954; }
  .meta { color: #9aa0a6; font-size: 14px; }
  .reason { margin: 8px 0 0; }
  .skip { padding: 10px 0; border-bottom: 1px solid #23252a; }
  .skip:last-child { border-bottom: 0; }
  button, .btn { font: inherit; background: #1db954; color: #06240f; border: 0;
    border-radius: 999px; padding: 10px 18px; font-weight: 600; cursor: pointer; text-decoration: none; display: inline-block; }
  button.secondary { background: #2a2c31; color: #e9e9ec; }
  input[type=number] { font: inherit; background: #16171b; color: #e9e9ec; border: 1px solid #2a2c31;
    border-radius: 8px; padding: 9px 12px; width: 90px; }
  label { display: block; margin: 6px 0; }
  .row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
  textarea { width: 100%; font: inherit; background: #16171b; color: #e9e9ec; border: 1px solid #2a2c31; border-radius: 8px; padding: 10px; }
  a { color: #6fd08c; }
  pre { white-space: pre-wrap; word-break: break-word; }
</style></head><body><main>
<h1>Podsen</h1><p class="tag">Listen to what's worth it.</p>
${body}
</main></body></html>`;
}

export function landing() {
  return `<p>Podsen looks at the unheard episodes piling up across shows you already follow,
and — given how much time you have right now — tells you the 1&ndash;2 worth playing today,
plus what's safe to skip. It never deletes or skips anything without you confirming.</p>
<p style="margin-top:24px"><a class="btn" href="/login">Connect Spotify</a></p>`;
}

export function timeForm(user) {
  return `<p>Connected as <strong>${esc(user?.name)}</strong>. <a href="/logout">Log out</a></p>
<h2>How much time do you have right now?</h2>
<form method="post" action="/triage" class="row">
  <button name="minutes" value="15">15 min</button>
  <button name="minutes" value="30">30 min</button>
  <button name="minutes" value="60">60 min</button>
</form>
<form method="post" action="/triage" class="row" style="margin-top:12px">
  <input type="number" name="minutes" min="1" max="600" value="45">
  <button class="secondary">Go</button>
</form>
<p class="meta" style="margin-top:20px">Scanning your backlog + ranking takes ~10&ndash;20 seconds.</p>`;
}

export function results(sid, minutes, picks, skips, backlog, showsScanned) {
  const pick = (p) => `<div class="card pick">
    <label class="row" style="align-items:flex-start">
      <input type="checkbox" name="pick" value="${esc(p.id)}" checked style="margin-top:5px">
      <span><strong>${esc(p.title)}</strong><br>
      <span class="meta">${esc(p.show)} · ${p.duration_min} min</span>
      <span class="reason">${esc(p.reason)}</span></span>
    </label></div>`;

  const skip = (s) => `<div class="skip"><strong>${esc(s.title)}</strong>
    <span class="meta">— ${esc(s.show)}</span><br><span class="reason">${esc(s.reason)}</span></div>`;

  return `<p class="meta">${backlog} unheard episodes across ${showsScanned} shows · ${minutes} min budget</p>
<form method="post" action="/confirm">
  <input type="hidden" name="sid" value="${esc(sid)}">
  <h2>Worth it today</h2>
  ${picks.map(pick).join('') || '<p>No strong pick — everything in the backlog is low-priority right now.</p>'}
  <h2>Safe to skip</h2>
  ${skips.map(skip).join('')}
  <p style="margin-top:24px"><label><input type="checkbox" name="make_playlist" checked>
    Put the checked picks in a private Spotify playlist</label></p>
  <button>Confirm</button>
  <a class="btn secondary" href="/app" style="margin-left:8px">Back</a>
</form>`;
}

export function done(sid, picks, playlist) {
  const list = picks.map((p) => `<li><strong>${esc(p.title)}</strong> — ${esc(p.show)}</li>`).join('');
  const pl = playlist?.url
    ? `<p style="margin:16px 0"><a class="btn" href="${esc(playlist.url)}" target="_blank" rel="noopener">Open playlist in Spotify</a></p>`
    : '';
  return `<h2>Confirmed</h2>
${picks.length ? `<ul>${list}</ul>` : '<p>Nothing selected.</p>'}
${pl}
<h2>One question</h2>
<form method="post" action="/feedback">
  <input type="hidden" name="sid" value="${esc(sid)}">
  <p>Was this useful?</p>
  <label><input type="radio" name="useful" value="yes" required> Yes, I'd use it again</label>
  <label><input type="radio" name="useful" value="somewhat"> Somewhat</label>
  <label><input type="radio" name="useful" value="no"> No</label>
  <p style="margin-top:16px">Would you pay for this?</p>
  <label><input type="radio" name="pay" value="yes" required> Yes</label>
  <label><input type="radio" name="pay" value="maybe"> Maybe</label>
  <label><input type="radio" name="pay" value="no"> No</label>
  <p style="margin-top:16px"><textarea name="note" rows="3" placeholder="Anything else? (optional)"></textarea></p>
  <button>Send</button>
</form>`;
}

export function thanks() {
  return `<p>Thanks — noted.</p><p><a href="/app">Run it again</a></p>`;
}

export { esc };
