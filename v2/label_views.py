"""HTML for /label: paste feeds -> label episodes -> save. Strings only, no client JS.

Every interpolated value goes through esc(): RSS titles, descriptions and even feed
URLs are untrusted.
"""

from views import esc

LABEL_CHOICES = (
    ("wanted", "Want"),
    ("not_wanted", "Don't want"),
    ("would_regret_skipping", "Would regret skipping"),
)

_STYLE = """<style>
  .ep { border-bottom: 1px solid #23252a; padding: 10px 0; }
  .ep:last-child { border-bottom: 0; }
  .ep .lab { display: flex; flex-wrap: wrap; gap: 4px 14px; margin-top: 6px; align-items: center; }
  .ep .lab label { display: inline-flex; gap: 5px; margin: 0; font-size: 15px; }
  .ep details { margin-top: 4px; font-size: 14px; color: #c4c7cc; }
  .ep details summary { cursor: pointer; color: #9aa0a6; }
  input[type=text], input[type=url] { font: inherit; background: #16171b; color: #e9e9ec;
    border: 1px solid #2a2c31; border-radius: 8px; padding: 7px 10px; }
  input.grp { width: 56px; padding: 4px 8px; text-transform: uppercase; }
  .num { width: 90px; }
  .field { margin: 0 0 16px; }
  .field > span { display: block; color: #9aa0a6; font-size: 14px; margin-bottom: 4px; }
  .err { border: 1px solid #6b2a33; background: #2a1418; color: #f1c4cb;
    border-radius: 10px; padding: 12px 14px; margin: 12px 0; font-size: 14px; }
</style>"""


def step1(values: dict | None = None, error: str | None = None, qs: str = "") -> str:
    v = values or {}
    err = f'<p class="err">{esc(error)}</p>' if error else ""
    return f"""{_STYLE}
<p class="standfirst">Label a backlog</p>
<p class="meta">For evaluating V2. Uses public RSS feeds only &mdash; no account needed.
Labels are saved on this machine under data/labels/ and never committed.</p>
{err}
<form method="post" action="/label/fetch{esc(qs)}">
  <label class="field"><span>Name for this backlog</span>
    <input type="text" name="name" value="{esc(v.get('name', ''))}" placeholder="e.g. weekday commute" style="width:100%"></label>
  <label class="field"><span>RSS feed URLs, one per line</span>
    <textarea name="feeds" rows="6" required placeholder="https://example.com/feed.xml">{esc(v.get('feeds', ''))}</textarea></label>
  <div class="row">
    <label class="field"><span>Time budget (min)</span>
      <input class="num" type="number" name="budget" min="1" max="600" required value="{esc(v.get('budget', '45'))}"></label>
    <label class="field"><span>Newest episodes per feed</span>
      <input class="num" type="number" name="per_feed" min="1" max="50" value="{esc(v.get('per_feed', '10'))}"></label>
  </div>
  <label class="field"><span>Intent: what do you want from this listening slot?</span>
    <textarea name="intent" rows="2" placeholder="e.g. something I can follow while driving, nothing too dense">{esc(v.get('intent', ''))}</textarea></label>
  <button>Load episodes</button>
</form>
<p class="meta" style="margin-top:16px">Aim for 30&ndash;60 episodes in total: about 5 feeds &times; 10 each.</p>"""


def _feed_summary(draft: dict) -> str:
    feeds = draft.get("feeds") or []
    failed = draft.get("failed") or []
    parts = []
    ok = "".join(
        f"<li>{esc(f['show'])} &middot; {esc(f['episodes'])} episodes"
        + (f" ({esc(f['skipped_no_duration'])} without a duration skipped)" if f.get("skipped_no_duration") else "")
        + "</li>"
        for f in feeds
    )
    if ok:
        parts.append(f'<ul class="meta">{ok}</ul>')
    if failed:
        items = "".join(f"<li>{esc(f['url'])}: {esc(f['error'])}</li>" for f in failed)
        parts.append(
            f'<div class="warn">Could not load {len(failed)} feed(s); their episodes are '
            f"<strong>not</strong> in this backlog:<ul>{items}</ul></div>"
        )
    if draft.get("duplicate_ids"):
        parts.append(
            f'<p class="meta">{esc(draft["duplicate_ids"])} episode(s) appeared in more than one '
            "feed and are listed once.</p>"
        )
    return "".join(parts)


def _episode_row(i: int, ep: dict, label: str | None, group: str) -> str:
    radios = "".join(
        f'<label><input type="radio" name="label_{i}" value="{value}"'
        f'{" checked" if label == value else ""}{" required" if n == 0 else ""}> {text}</label>'
        for n, (value, text) in enumerate(LABEL_CHOICES)
    )
    meta = " &middot; ".join(
        x
        for x in (
            esc(ep.get("show")),
            f"{esc(ep.get('duration_min'))} min",
            esc(ep.get("released") or "undated"),
            "transcript" if ep.get("has_transcript") else "",
        )
        if x
    )
    desc = (
        f"<details><summary>description</summary>{esc(ep['description'])}</details>"
        if ep.get("description")
        else ""
    )
    return f"""<div class="ep">
  <strong>{esc(ep.get('title'))}</strong><br><span class="meta">{meta}</span>
  {desc}
  <div class="lab">{radios}
    <label title="Same letter on episodes that cover the same thing">dup group
      <input class="grp" type="text" name="group_{i}" maxlength="3" value="{esc(group)}"></label>
  </div>
</div>"""


def step2(draft_id: str, draft: dict, labels=None, groups=None, error: str | None = None, qs: str = "") -> str:
    labels, groups = labels or {}, groups or {}
    eps = draft["episodes"]
    total = sum(e["duration_min"] for e in eps)
    rows = "".join(_episode_row(i, ep, labels.get(i), groups.get(i, "")) for i, ep in enumerate(eps))
    err = f'<p class="err">{esc(error)}</p>' if error else ""
    return f"""{_STYLE}
<p class="standfirst">{esc(draft['name'])}</p>
<p class="meta">{len(eps)} episodes &middot; {esc(total)} min in total &middot;
budget {esc(draft['budget_minutes'])} min</p>
{f'<p class="meta">Intent: {esc(draft["intent"])}</p>' if draft.get("intent") else ""}
{_feed_summary(draft)}
{err}
<p class="meta">Label every episode for this budget and intent. If two or more episodes
cover the same thing, give them the same letter in <em>dup group</em> (e.g. A).</p>
<form method="post" action="/label/save{esc(qs)}">
  <input type="hidden" name="draft" value="{esc(draft_id)}">
  {rows}
  <p style="margin-top:20px"><button>Save labels</button>
  <a class="btn secondary" href="/label{esc(qs)}" style="margin-left:8px">Start over</a></p>
</form>"""


def no_episodes(draft: dict, qs: str = "") -> str:
    return f"""{_STYLE}
<p class="warn">No episodes with a duration were found in these feeds.</p>
{_feed_summary(draft)}
<p style="margin-top:20px"><a class="btn" href="/label{esc(qs)}">Try other feeds</a></p>"""


def saved(path: str, n_episodes: int, n_groups: int, qs: str = "") -> str:
    return f"""<h2>Saved</h2>
<p>{esc(n_episodes)} labeled episodes and {esc(n_groups)} duplicate group(s) written to
<code>{esc(path)}</code>.</p>
<p class="meta">Run the comparison with: <code>python -m v2.report</code></p>
<p style="margin-top:20px"><a class="btn" href="/label{esc(qs)}">Label another backlog</a></p>"""
