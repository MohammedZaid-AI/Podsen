import assert from 'node:assert';
import { isUnheard } from './spotify.js';
import { extractJSON } from './rank.js';

// unheard detection
assert.equal(isUnheard({ id: 'a', resume_point: { fully_played: false, resume_position_ms: 0 } }), true);
assert.equal(isUnheard({ id: 'a', resume_point: { fully_played: true, resume_position_ms: 0 } }), false);
assert.equal(isUnheard({ id: 'a', resume_point: { fully_played: false, resume_position_ms: 12000 } }), false);
assert.equal(isUnheard({ id: 'a', is_playable: false, resume_point: {} }), false);
assert.equal(isUnheard({ id: 'a' }), true); // no resume data -> degraded "unheard"
assert.equal(isUnheard({ id: 'a', resume_point: {} }, new Set(['a'])), false); // filtered by recently-played

// model-output JSON extraction tolerates chatter around the object
const t = 'Sure:\n{"picks":[{"id":"x","reason":"r"}],"skips":[]}\nThat\'s it.';
assert.equal(extractJSON(t).picks[0].id, 'x');
assert.throws(() => extractJSON('no json here'));

console.log('ok');
