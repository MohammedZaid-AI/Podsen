// Plain, reliable code: OAuth + data fetch + playlist creation. No AI here.
import process from 'node:process';

const ACCOUNTS = 'https://accounts.spotify.com';
const API = 'https://api.spotify.com/v1';
const SCOPES = [
  'user-library-read', // saved/followed shows + saved episodes
  'user-read-playback-position', // episode resume_point -> heard/unheard
  'user-read-recently-played',
  'playlist-modify-private',
  'playlist-modify-public',
  'user-read-private', // user id + country (market)
].join(' ');

const CID = () => process.env.SPOTIFY_CLIENT_ID;
const SECRET = () => process.env.SPOTIFY_CLIENT_SECRET;
const REDIRECT = () => `${process.env.BASE_URL}/callback`;
const basicAuth = () => 'Basic ' + Buffer.from(`${CID()}:${SECRET()}`).toString('base64');

export function authUrl(state) {
  const p = new URLSearchParams({
    client_id: CID(),
    response_type: 'code',
    redirect_uri: REDIRECT(),
    scope: SCOPES,
    state,
  });
  return `${ACCOUNTS}/authorize?${p}`;
}

function tokenize(j) {
  return {
    access_token: j.access_token,
    refresh_token: j.refresh_token,
    expires_at: Date.now() + (j.expires_in ?? 3600) * 1000 - 60_000,
  };
}

export async function exchangeCode(code) {
  const r = await fetch(`${ACCOUNTS}/api/token`, {
    method: 'POST',
    headers: { Authorization: basicAuth(), 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ grant_type: 'authorization_code', code, redirect_uri: REDIRECT() }),
  });
  if (!r.ok) throw new Error(`token exchange failed: ${r.status} ${await r.text()}`);
  return tokenize(await r.json());
}

export async function refresh(refresh_token) {
  const r = await fetch(`${ACCOUNTS}/api/token`, {
    method: 'POST',
    headers: { Authorization: basicAuth(), 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ grant_type: 'refresh_token', refresh_token }),
  });
  if (!r.ok) throw new Error(`token refresh failed: ${r.status}`);
  const t = tokenize(await r.json());
  if (!t.refresh_token) t.refresh_token = refresh_token; // Spotify often omits it on refresh
  return t;
}

// `session` is req.session; we mutate session.tokens in place when refreshing.
async function call(session, path, opts = {}, retry = true) {
  if (Date.now() > session.tokens.expires_at) {
    session.tokens = await refresh(session.tokens.refresh_token);
  }
  const r = await fetch(path.startsWith('http') ? path : API + path, {
    ...opts,
    headers: {
      Authorization: `Bearer ${session.tokens.access_token}`,
      'Content-Type': 'application/json',
      ...opts.headers,
    },
  });
  if (r.status === 401 && retry) {
    session.tokens = await refresh(session.tokens.refresh_token);
    return call(session, path, opts, false);
  }
  if (r.status === 429 && retry) {
    const wait = Math.min(Number(r.headers.get('retry-after') || 2), 10);
    await new Promise((res) => setTimeout(res, wait * 1000));
    return call(session, path, opts, false);
  }
  if (!r.ok) throw new Error(`spotify ${r.status} on ${path}: ${(await r.text()).slice(0, 300)}`);
  if (r.status === 204) return null;
  return r.json();
}

export async function getMe(session) {
  const j = await call(session, '/me');
  return { id: j.id, name: j.display_name || j.id, country: j.country || 'US' };
}

async function pool(items, n, fn) {
  const out = [];
  let i = 0;
  const worker = async () => {
    while (i < items.length) {
      const idx = i++;
      out[idx] = await fn(items[idx]);
    }
  };
  await Promise.all(Array.from({ length: Math.min(n, items.length || 1) }, worker));
  return out;
}

// An episode is "unheard backlog" if playback never started and it isn't finished.
// If Spotify returns no resume_point (scope/market quirks) we treat it as unheard —
// degraded but usable; recently-played ids below still filter obvious repeats.
// ponytail: resume_point + recently-played is the whole heard-detection heuristic.
// Upgrade path: per-episode /episodes/{id} lookups if false "unheard" hurts picks.
export function isUnheard(e, playedIds = new Set()) {
  if (!e || e.is_playable === false) return false;
  if (playedIds.has(e.id)) return false;
  const rp = e.resume_point || {};
  return !rp.fully_played && (rp.resume_position_ms || 0) === 0;
}

async function recentlyPlayedEpisodeIds(session) {
  try {
    const j = await call(session, '/me/player/recently-played?limit=50');
    const ids = new Set();
    for (const it of j.items || []) {
      const t = it.track || it.item || {};
      if (t.type === 'episode' && t.id) ids.add(t.id);
    }
    return ids;
  } catch {
    return new Set();
  }
}

export async function getBacklog(session, { maxShows = 50, perShow = 20, market = 'US' } = {}) {
  const shows = [];
  let url = '/me/shows?limit=50';
  while (url && shows.length < maxShows) {
    const j = await call(session, url);
    for (const it of j.items) shows.push(it.show);
    url = j.next;
  }
  const use = shows.slice(0, maxShows);
  const playedIds = await recentlyPlayedEpisodeIds(session);

  const perShowEps = await pool(use, 8, async (show) => {
    try {
      const j = await call(session, `/shows/${show.id}/episodes?limit=${perShow}&market=${market}`);
      return (j.items || []).map((e) => ({ ...e, _show: show }));
    } catch {
      return [];
    }
  });

  const episodes = [];
  for (const list of perShowEps) {
    for (const e of list) {
      if (!isUnheard(e, playedIds)) continue;
      episodes.push({
        id: e.id,
        uri: e.uri,
        title: e.name,
        show: e._show.name,
        publisher: e._show.publisher,
        description: (e.description || '').replace(/\s+/g, ' ').slice(0, 600),
        duration_min: Math.round((e.duration_ms || 0) / 60000),
        released: e.release_date,
      });
    }
  }
  episodes.sort((a, b) => (b.released || '').localeCompare(a.released || ''));
  return { episodes, showsScanned: use.length };
}

export async function createPlaylist(session, userId, name, description, uris) {
  const pl = await call(session, `/users/${encodeURIComponent(userId)}/playlists`, {
    method: 'POST',
    body: JSON.stringify({ name, public: false, description }),
  });
  if (uris.length) {
    await call(session, `/playlists/${pl.id}/tracks`, {
      method: 'POST',
      body: JSON.stringify({ uris }),
    });
  }
  return { url: pl.external_urls?.spotify, id: pl.id };
}
