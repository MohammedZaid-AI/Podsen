// The only AI in the app: score fit against the time budget + write the reasons.
// Runs against Ollama's OpenAI-compatible endpoint (local by default).
import process from 'node:process';

const BASE = (process.env.OLLAMA_BASE_URL || 'http://localhost:11434/v1').replace(/\/$/, '');
const MODEL = process.env.OLLAMA_MODEL || 'gemma4:26b';
const KEY = process.env.OLLAMA_API_KEY || 'ollama'; // Ollama ignores it; a proxy in front may not
const TIMEOUT_MS = Number(process.env.OLLAMA_TIMEOUT_MS || 180_000);

// Local models are worse at long context than Claude was. Fewer candidates and
// shorter blurbs measurably improve the reasons.
// ponytail: fixed 30/350 tuned for a mid-size local model; raise both on a big
// hosted model if picks start missing good episodes deeper in the backlog.
const MAX_EPISODES = 30;
const DESC_CHARS = 350;

const SYSTEM = `You are Podsen, a podcast triage assistant. Tagline: "Listen to what's worth it."
The user already follows every show below and has a backlog of unheard episodes. Given how much time they have RIGHT NOW, pick the 1-2 episodes genuinely worth hearing today and list 3-6 that are safe to skip.
Judge on: topical substance, timeliness (news/interview-of-the-moment decays fast; evergreen deep-dives don't), and fit to the time budget (an episode much longer than the budget is a poor fit unless exceptional; a couple of short ones can stack).
This is triage of shows they chose to follow — not discovery, and never suggest unfollowing.
Every reason is ONE sentence that names something concrete from that episode's own title or description — a person, a claim, an event, a question it takes up. A reason that would still make sense pasted under a different episode is wrong. Never write filler like "sounds interesting", "a good listen", or "worth your time".

A pick's reason must say why it earns the listening time they have — what they get out of it — not merely restate the blurb.

A skip's reason must say why it KEEPS, and each one must give a different kind of reason. Draw on: it is evergreen and will be just as good next month; it is a re-run or "Best Of"; its news hook has already been overtaken; it is a niche or narrow-interest instalment of a show they otherwise like; the same ground is covered by a pick. Running time alone is NEVER a sufficient reason — do not write "it is N minutes long"; if length is the issue, say what they would be giving up the rest of the day to fit it in.`;

function extractJSON(text) {
  const s = text.indexOf('{');
  const e = text.lastIndexOf('}');
  if (s === -1 || e === -1) throw new Error('no JSON in model output');
  return JSON.parse(text.slice(s, e + 1));
}

export async function rank(episodes, minutes) {
  const candidates = episodes.slice(0, MAX_EPISODES);
  // Small models copy a short integer far more reliably than a 22-char Spotify id.
  const list = candidates.map((e, i) => ({
    n: i + 1,
    show: e.show,
    title: e.title,
    minutes: e.duration_min,
    released: e.released,
    description: (e.description || '').slice(0, DESC_CHARS),
  }));

  const user = `Time available: ${minutes} minutes.

Backlog (${list.length} unheard episodes):
${JSON.stringify(list)}

Respond with ONLY this JSON, using the "n" numbers from the list:
{"picks":[{"n":0,"reason":""}],"skips":[{"n":0,"reason":""}]}
1-2 picks, 3-6 skips. A number must not appear in both lists.`;

  const r = await fetch(`${BASE}/chat/completions`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${KEY}`, 'Content-Type': 'application/json' },
    signal: AbortSignal.timeout(TIMEOUT_MS),
    body: JSON.stringify({
      model: MODEL,
      temperature: 0.3,
      stream: false,
      response_format: { type: 'json_object' },
      messages: [
        { role: 'system', content: SYSTEM },
        { role: 'user', content: user },
      ],
    }),
  });
  if (!r.ok) throw new Error(`ollama ${r.status} at ${BASE}: ${(await r.text()).slice(0, 300)}`);

  const j = await r.json();
  const choice = j.choices?.[0] ?? {};
  const msg = choice.message ?? {};
  // Reasoning models (gemma4, deepseek-r1, gpt-oss) split their output: the answer
  // lands in `content`, the scratchpad in `reasoning`. If an early stop leaves
  // `content` empty, the JSON is often still in `reasoning`.
  const text = msg.content?.trim() || msg.reasoning?.trim() || '';

  // Ollama's default context is 4096 tokens TOTAL (prompt + output) no matter what
  // the model supports, and the /v1 endpoint ignores `options.num_ctx`. A backlog
  // prompt is ~4k on its own, so the answer gets guillotined before the JSON.
  // Without this check the symptom is a baffling "no JSON in model output".
  if (choice.finish_reason === 'length') {
    throw new Error(
      `${MODEL} hit the context limit (used ${j.usage?.total_tokens} tokens) before finishing the JSON. ` +
        `Restart Ollama with OLLAMA_CONTEXT_LENGTH=16384, or lower MAX_EPISODES in rank.js.`,
    );
  }
  if (!text) throw new Error(`empty response from ${MODEL} (finish_reason=${choice.finish_reason})`);
  const out = extractJSON(text);

  const hydrate = (arr) =>
    (arr || [])
      .map((x) => {
        const e = candidates[Number(x.n) - 1];
        return e && x.reason ? { ...e, reason: String(x.reason).trim() } : null;
      })
      .filter(Boolean);

  const picks = hydrate(out.picks).slice(0, 2);
  const pickedIds = new Set(picks.map((p) => p.id));
  const skips = hydrate(out.skips).filter((s) => !pickedIds.has(s.id)).slice(0, 6);
  return { picks, skips };
}

export { extractJSON };
