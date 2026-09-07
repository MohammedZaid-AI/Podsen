import express from 'express';
import cookieSession from 'cookie-session';
import crypto from 'node:crypto';
import process from 'node:process';
import * as spotify from './spotify.js';
import { rank } from './rank.js';
import { append, readAll } from './store.js';
import { page, landing, timeForm, results, done, thanks, esc } from './views.js';

const app = express();
app.set('trust proxy', 1);
app.use(express.urlencoded({ extended: false }));
app.use(
  cookieSession({
    name: 'podsen',
    secret: process.env.SESSION_SECRET || 'dev-insecure-secret',
    httpOnly: true,
    sameSite: 'lax',
    secure: process.env.NODE_ENV === 'production',
    maxAge: 7 * 24 * 3600 * 1000,
  }),
);

const authed = (req) => Boolean(req.session?.tokens?.access_token);

app.get('/', (req, res) => {
  if (authed(req)) return res.redirect('/app');
  res.send(page('Podsen', landing()));
});

app.get('/login', (req, res) => {
  const state = crypto.randomBytes(16).toString('hex');
  req.session.state = state;
  res.redirect(spotify.authUrl(state));
});

app.get('/callback', async (req, res, next) => {
  try {
    if (req.query.error) {
      return res.send(page('Podsen', `<p>Spotify: ${esc(req.query.error)}</p><p><a href="/">Try again</a></p>`));
    }
    if (!req.query.state || req.query.state !== req.session.state) {
      return res.status(400).send(page('Podsen', '<p>Auth state mismatch. <a href="/login">Retry</a></p>'));
    }
    req.session.tokens = await spotify.exchangeCode(req.query.code);
    req.session.user = await spotify.getMe(req.session);
    req.session.state = undefined;
    res.redirect('/app');
  } catch (e) {
    next(e);
  }
});

app.get('/app', (req, res) => {
  if (!authed(req)) return res.redirect('/');
  res.send(page('Podsen', timeForm(req.session.user)));
});

app.post('/triage', async (req, res, next) => {
  try {
    if (!authed(req)) return res.redirect('/');
    const minutes = Math.max(1, Math.min(600, parseInt(req.body.minutes, 10) || 30));
    const { episodes, showsScanned } = await spotify.getBacklog(req.session, {
      market: req.session.user?.country || 'US',
    });
    if (!episodes.length) {
      return res.send(
        page('Podsen', `<p>No unheard episodes found across ${showsScanned} shows. Inbox zero — nice.</p><p><a href="/app">Back</a></p>`),
      );
    }
    const { picks, skips } = await rank(episodes, minutes);
    const sid = crypto.randomUUID();
    append('events.jsonl', {
      sid,
      type: 'triage',
      user: req.session.user?.id,
      minutes,
      backlog: episodes.length,
      showsScanned,
      picks: picks.map((p) => ({ id: p.id, uri: p.uri, title: p.title, show: p.show, reason: p.reason })),
      skips: skips.map((s) => ({ id: s.id, title: s.title, show: s.show, reason: s.reason })),
    });
    res.send(page('Podsen', results(sid, minutes, picks, skips, episodes.length, showsScanned)));
  } catch (e) {
    next(e);
  }
});

app.post('/confirm', async (req, res, next) => {
  try {
    if (!authed(req)) return res.redirect('/');
    const ev = readAll('events.jsonl')
      .filter((e) => e.sid === req.body.sid && e.type === 'triage')
      .pop();
    if (!ev) return res.status(400).send(page('Podsen', '<p>Unknown session. <a href="/app">Start over</a></p>'));

    const chosen = [].concat(req.body.pick || []);
    const picks = ev.picks.filter((p) => chosen.includes(p.id));

    let playlist = null;
    if (req.body.make_playlist && picks.length) {
      playlist = await spotify.createPlaylist(
        req.session,
        req.session.user.id,
        `Podsen — ${new Date().toISOString().slice(0, 10)}`,
        `Picked by Podsen for a ${ev.minutes}-min listen.`,
        picks.map((p) => p.uri),
      );
    }
    append('events.jsonl', {
      sid: req.body.sid,
      type: 'confirm',
      confirmed: picks.map((p) => p.id),
      playlist: playlist?.url || null,
    });
    res.send(page('Podsen', done(req.body.sid, picks, playlist)));
  } catch (e) {
    next(e);
  }
});

app.post('/feedback', (req, res) => {
  append('events.jsonl', {
    sid: req.body.sid,
    type: 'feedback',
    useful: req.body.useful,
    pay: req.body.pay,
    note: (req.body.note || '').slice(0, 500),
  });
  res.send(page('Podsen', thanks()));
});

app.get('/logout', (req, res) => {
  req.session = null;
  res.redirect('/');
});

app.get('/admin', (req, res) => {
  if (!process.env.ADMIN_KEY || req.query.key !== process.env.ADMIN_KEY) return res.status(403).send('nope');
  res.type('json').send(JSON.stringify(readAll('events.jsonl'), null, 2));
});

// eslint-disable-next-line no-unused-vars
app.use((err, req, res, _next) => {
  console.error(err);
  res
    .status(500)
    .send(page('Podsen', `<p>Something broke:</p><pre>${esc(err.message)}</pre><p><a href="/app">Back</a></p>`));
});

app.listen(process.env.PORT || 3000, () => console.log(`podsen on :${process.env.PORT || 3000}`));
