"""V2 Phase 0: RSS loader, label files, baselines, metrics, report, /label, data rule.

No network: feeds come from the saved XML below, fetch is injected, and the LLM
transport is stubbed. Nothing here needs, reads or sends a credential.
"""

import ast
import copy
import json
import os
import random
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import rank as rank_mod  # noqa: E402
from v2 import baselines, dataset, metrics, report, rss  # noqa: E402

SAMPLE_DIR = ROOT / "v2" / "sample_data"

FEED_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
     xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"
     xmlns:podcast="https://podcastindex.org/namespace/1.0">
<channel>
  <title>Test   Show</title>
  <item>
    <title>Seconds episode</title>
    <guid isPermaLink="false">guid-seconds</guid>
    <pubDate>Mon, 12 Oct 2026 06:00:00 +0000</pubDate>
    <itunes:duration>3600</itunes:duration>
    <description><![CDATA[<p>Hello   <b>world</b>&amp; friends</p>]]></description>
    <podcast:transcript url="https://example.com/t.vtt" type="text/vtt"/>
  </item>
  <item>
    <title>MMSS episode</title>
    <guid>guid-mmss</guid>
    <pubDate>Sun, 11 Oct 2026 06:00:00 +0000</pubDate>
    <itunes:duration>45:30</itunes:duration>
    <description>%LONG%</description>
  </item>
  <item>
    <title>HHMMSS episode &lt;script&gt;alert(1)&lt;/script&gt;</title>
    <guid>guid-hhmmss</guid>
    <pubDate>Sat, 10 Oct 2026 06:00:00 +0000</pubDate>
    <itunes:duration>1:02:03</itunes:duration>
  </item>
  <item>
    <title>No duration, must be skipped</title>
    <guid>guid-nodur</guid>
    <pubDate>Fri, 09 Oct 2026 06:00:00 +0000</pubDate>
  </item>
  <item>
    <title>No guid, enclosure fallback</title>
    <enclosure url="https://example.com/ep5.mp3" type="audio/mpeg" length="1"/>
    <pubDate>Thu, 08 Oct 2026 06:00:00 +0000</pubDate>
    <itunes:duration>600</itunes:duration>
  </item>
</channel>
</rss>""".replace("%LONG%", "word " * 400)


def parsed():
    return rss.parse_feed(FEED_XML.encode("utf-8"))


# --- RSS loader ----------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,minutes",
    [
        ("3600", 60),
        ("3600.4", 61),  # rounded up: the budget is a hard cap
        ("59", 1),
        ("45:30", 46),
        ("45:00", 45),
        ("1:02:03", 63),
        ("01:00:00", 60),
        (" 12:00 ", 12),
        ("", None),
        (None, None),
        ("0", None),
        ("abc", None),
        ("1:2:3:4", None),
        ("-5", None),
    ],
)
def test_duration_parsing_all_forms(raw, minutes):
    assert rss.parse_duration(raw) == minutes


def test_guid_becomes_id_and_fields_match_v1_names():
    eps = parsed()["episodes"]
    assert [e["id"] for e in eps] == [
        "guid-seconds", "guid-mmss", "guid-hhmmss", "https://example.com/ep5.mp3"
    ]
    for e in eps:
        assert tuple(e) == rss.EPISODE_FIELDS
        assert e["show"] == "Test Show"
    assert eps[0]["released"] == "2026-10-12"
    assert [e["duration_min"] for e in eps] == [60, 46, 63, 10]


def test_transcript_detection():
    by_id = {e["id"]: e for e in parsed()["episodes"]}
    assert by_id["guid-seconds"]["has_transcript"] is True
    assert by_id["guid-mmss"]["has_transcript"] is False


def test_episodes_without_duration_are_skipped_and_counted():
    out = parsed()
    assert "guid-nodur" not in {e["id"] for e in out["episodes"]}
    assert out["skipped_no_duration"] == 1


def test_description_is_stripped_collapsed_and_capped():
    eps = {e["id"]: e for e in parsed()["episodes"]}
    assert eps["guid-seconds"]["description"] == "Hello world & friends"
    assert len(eps["guid-mmss"]["description"]) == rss.DESC_CHARS
    assert "  " not in eps["guid-mmss"]["description"]


def test_per_feed_keeps_the_newest():
    out = rss.parse_feed(FEED_XML.encode("utf-8"), per_feed=2)
    assert [e["id"] for e in out["episodes"]] == ["guid-seconds", "guid-mmss"]


def test_failing_feed_is_reported_not_dropped():
    def fetch(url):
        if "bad" in url:
            raise OSError("connection refused")
        return FEED_XML.encode("utf-8")

    out = rss.load_feeds(["https://ok.example/feed", "https://bad.example/feed"], fetch=fetch)
    assert [f["url"] for f in out["failed"]] == ["https://bad.example/feed"]
    assert "connection refused" in out["failed"][0]["error"]
    assert len(out["episodes"]) == 4
    assert out["skipped_no_duration"] == 1


def test_same_guid_in_two_feeds_is_listed_once_and_counted():
    out = rss.load_feeds(
        ["https://a.example/feed", "https://b.example/feed"],
        fetch=lambda url: FEED_XML.encode("utf-8"),
    )
    assert len(out["episodes"]) == 4
    assert out["duplicate_ids"] == 4


@pytest.mark.parametrize(
    "url", ["file:///etc/passwd", "ftp://example.com/feed", "http://127.0.0.1/feed", "http://10.0.0.5/feed"]
)
def test_only_public_http_feeds_are_fetched(url):
    # IP literals resolve locally, so this makes no network call.
    with pytest.raises(ValueError):
        rss.fetch_feed(url)


# --- label files -----------------------------------------------------------------

def valid_backlog():
    return {
        "name": "tiny",
        "budget_minutes": 30,
        "intent": "",
        "episodes": [
            {"id": "a", "title": "A", "show": "S", "duration_min": 10},
            {"id": "b", "title": "B", "show": "S", "duration_min": 20},
            {"id": "c", "title": "C", "show": "T", "duration_min": 25},
        ],
        "labels": {"a": "wanted", "b": "not_wanted", "c": "would_regret_skipping"},
        "duplicate_groups": [["a", "b"]],
    }


def test_valid_label_file_loads(tmp_path):
    p = tmp_path / "x.json"
    p.write_text(json.dumps(valid_backlog()), encoding="utf-8")
    assert dataset.load(p)["name"] == "tiny"


def _mut(fn):
    d = copy.deepcopy(valid_backlog())
    fn(d)
    return d


@pytest.mark.parametrize(
    "data,message",
    [
        (_mut(lambda d: d.pop("labels")), "missing required key"),
        (_mut(lambda d: d.update(budget_minutes=0)), "budget_minutes"),
        (_mut(lambda d: d.update(budget_minutes="30")), "budget_minutes"),
        (_mut(lambda d: d["labels"].update(a="maybe")), "must be one of"),
        (_mut(lambda d: d["labels"].pop("b")), "have no label"),
        (_mut(lambda d: d["labels"].update(zzz="wanted")), "unknown episode"),
        (_mut(lambda d: d["episodes"].append(dict(d["episodes"][0]))), "duplicate episode id"),
        (_mut(lambda d: d["episodes"][0].update(duration_min=0)), "duration_min"),
        (_mut(lambda d: d.update(duplicate_groups=[["a"]])), "at least 2"),
        (_mut(lambda d: d.update(duplicate_groups=[["a", "nope"]])), "unknown episode id"),
        (_mut(lambda d: d.update(duplicate_groups=[["a", "b"], ["a", "c"]])), "more than one"),
        (_mut(lambda d: d.update(surprise=1)), "unknown key"),
        (_mut(lambda d: d.update(episodes=[])), "non-empty"),
    ],
)
def test_bad_label_files_give_clear_errors(tmp_path, data, message):
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(dataset.DatasetError, match=message) as e:
        dataset.load(p)
    assert "bad.json" in str(e.value), "the error must name the file"


def test_invalid_json_names_the_line(tmp_path):
    p = tmp_path / "broken.json"
    p.write_text('{"name": "x",\n  oops}', encoding="utf-8")
    with pytest.raises(dataset.DatasetError, match="not valid JSON.*line 2"):
        dataset.load(p)


def test_load_dir_ignores_drafts_subfolder(tmp_path):
    (tmp_path / "drafts").mkdir()
    (tmp_path / "drafts" / "half.json").write_text("{}", encoding="utf-8")
    (tmp_path / "one.json").write_text(json.dumps(valid_backlog()), encoding="utf-8")
    assert len(dataset.load_dir(tmp_path)) == 1


def test_save_never_overwrites(tmp_path):
    first = dataset.save(valid_backlog(), tmp_path)
    second = dataset.save(valid_backlog(), tmp_path)
    assert first != second and first.exists() and second.exists()


def test_sample_data_loads_and_is_marked_fake():
    files = sorted(SAMPLE_DIR.glob("*.json"))
    assert len(files) == 3
    for f in files:
        assert f.name.startswith("FAKE_")
        data = dataset.load(f)
        assert data["fake"] is True and "FAKE" in data["note"] and data["name"].startswith("FAKE")


# --- baselines: the budget is a hard cap ----------------------------------------

def random_backlog(rng, n=30):
    return [
        {
            "id": f"e{i}",
            "title": f"T{i}",
            "show": f"S{i % 5}",
            "duration_min": rng.choice([3, 8, 15, 22, 31, 45, 58, 61, 75, 95, 140]),
            "released": f"2026-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}",
        }
        for i in range(n)
    ]


@pytest.fixture
def stub_v1(monkeypatch):
    """v1_llm with a fake key and a greedy fake rank(): it picks the longest episodes it
    can, within V1's 10% per-episode tolerance, so the hard cap has to do the work."""
    monkeypatch.setenv("RANK_BACKEND", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "placeholder-not-a-real-key")
    calls = []

    def fake_rank(episodes, minutes):
        calls.append(minutes)
        ok = [e for e in episodes if e["duration_min"] <= minutes * rank_mod.BUDGET_TOLERANCE]
        ok.sort(key=lambda e: -e["duration_min"])
        return {"picks": [dict(e, reason="r") for e in ok[:2]], "skips": [], "shortest_unfit": None}

    monkeypatch.setattr(baselines.rank, "rank", fake_rank)
    return calls


@pytest.mark.parametrize("budget", [5, 15, 30, 45, 60, 90, 200])
def test_no_method_ever_goes_over_budget(budget, stub_v1):
    rng = random.Random(budget)
    for _ in range(25):
        bl = random_backlog(rng)
        runs = [baselines.newest_first(bl, budget), baselines.v1_llm(bl, budget)]
        runs += [baselines.random_fill(bl, budget, seed=s) for s in range(20)]
        for picks, skips in runs:
            assert sum(p["duration_min"] for p in picks) <= budget
            assert {p["id"] for p in picks} | {s["id"] for s in skips} == {e["id"] for e in bl}
            assert not {p["id"] for p in picks} & {s["id"] for s in skips}


def test_v1_over_budget_picks_are_dropped_and_counted(stub_v1):
    bl = [
        {"id": "a", "title": "A", "show": "S", "duration_min": 32, "released": "2026-10-01"},
        {"id": "b", "title": "B", "show": "S", "duration_min": 25, "released": "2026-10-02"},
    ]
    picks, skips, dropped = baselines.v1_llm_with_stats(bl, 30)
    # V1 allows 32 min for a 30 min budget (10% tolerance); V2 does not.
    assert picks == [bl[1]] and [d["id"] for d in dropped] == ["a"]
    assert [s["id"] for s in skips] == ["a"]
    assert "reason" not in picks[0], "V2 picks are the backlog's own episodes"


def test_newest_first_passes_over_what_does_not_fit_and_keeps_filling():
    bl = [
        {"id": "new_long", "duration_min": 50, "released": "2026-10-03"},
        {"id": "mid", "duration_min": 20, "released": "2026-10-02"},
        {"id": "old", "duration_min": 10, "released": "2026-10-01"},
    ]
    picks, _ = baselines.newest_first(bl, 30)
    assert [p["id"] for p in picks] == ["mid", "old"]


def test_same_seed_same_random_result():
    bl = random_backlog(random.Random(1), n=40)
    a = baselines.random_fill(bl, 60, seed=7)
    b = baselines.random_fill(bl, 60, seed=7)
    assert [p["id"] for p in a[0]] == [p["id"] for p in b[0]]
    others = {tuple(p["id"] for p in baselines.random_fill(bl, 60, seed=s)[0]) for s in range(10)}
    assert len(others) > 1, "different seeds should give different sets"


def test_v1_llm_without_a_key_is_skipped_not_failed(monkeypatch):
    monkeypatch.setenv("RANK_BACKEND", "groq")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setattr(rank_mod, "GROQ_API_KEY", "")

    def must_not_call(*a, **k):
        raise AssertionError("v1_llm must not run without a key")

    monkeypatch.setattr(baselines.rank, "rank", must_not_call)
    backlogs = dataset.load_dir(SAMPLE_DIR)
    results = report.evaluate(backlogs, rounds=200)
    assert results["v1_llm"]["status"] == "skipped"
    assert "SKIPPED, no API key" in report.render(results, backlogs)
    assert results["newest_first"]["status"] == "ok"


# --- metrics ---------------------------------------------------------------------

def test_metric_math_on_a_tiny_case():
    eps = {e["id"]: e for e in [
        {"id": "a", "duration_min": 10}, {"id": "b", "duration_min": 15},
        {"id": "c", "duration_min": 20}, {"id": "d", "duration_min": 5},
        {"id": "e", "duration_min": 30},
    ]}
    labels = {"a": "wanted", "b": "not_wanted", "c": "would_regret_skipping",
              "d": "would_regret_skipping", "e": "not_wanted"}
    picks = [eps["a"], eps["b"], eps["c"]]
    skips = [eps["d"], eps["e"]]
    groups = [["a", "b"], ["c", "d"], ["d", "e"]]
    s = metrics.score(picks, skips, labels, 50, groups)
    assert s["pick_hit_rate"] == pytest.approx(2 / 3)  # a wanted, c regret-skip counts as hit
    assert s["regret_rate"] == pytest.approx(1 / 2)
    assert s["budget_used"] == pytest.approx(45 / 50)
    assert s["duplicate_catch_rate"] == pytest.approx(2 / 3)  # a+b both picked: missed


def test_metric_edge_cases_are_na_not_zero():
    s = metrics.score([], [{"id": "x", "duration_min": 5}], {"x": "wanted"}, 30, [])
    assert s["pick_hit_rate"] is None
    assert s["duplicate_catch_rate"] is None
    assert s["budget_used"] == 0
    full = metrics.score([{"id": "x", "duration_min": 40}], [], {"x": "wanted"}, 30)
    assert full["budget_used"] == 1.0 and full["regret_rate"] is None


def test_bootstrap_is_repeatable_with_a_fixed_seed():
    vals = [0.1, 0.4, 0.35, 0.8, 0.55, 0.6, None]
    a = metrics.bootstrap_ci(vals, rounds=2000, seed=3)
    b = metrics.bootstrap_ci(vals, rounds=2000, seed=3)
    assert a == b
    assert a[0] <= metrics.mean(vals) <= a[1]
    assert metrics.bootstrap_ci([0.5], rounds=2000) is None
    assert metrics.bootstrap_ci([None, None]) is None


# --- report ------------------------------------------------------------------------

def test_report_on_sample_data_runs_offline(monkeypatch, capsys):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setattr(rank_mod, "GROQ_API_KEY", "")
    monkeypatch.setenv("RANK_BACKEND", "groq")
    assert report.main(["--dir", str(SAMPLE_DIR)]) == 0
    out = capsys.readouterr().out
    assert "FAKE SAMPLE DATA" in out
    assert "Backlogs: 3" in out and "Labeled episodes: 42" in out
    assert "Not a meaningful difference yet." in out
    for name in ("newest_first", "random_fill", "v1_llm"):
        assert name in out


def test_report_counts_v1_drops(stub_v1):
    backlogs = dataset.load_dir(SAMPLE_DIR)
    results = report.evaluate(backlogs, methods=["newest_first", "v1_llm"], rounds=200)
    assert results["v1_llm"]["status"] == "ok"
    assert "over-budget picks dropped by the hard cap" in report.render(results, backlogs)


def test_report_never_calls_a_difference_on_too_few_backlogs():
    results = {
        "newest_first": {"status": "ok", "summary": {m: (0.1, (0.0, 0.2), 3) for m in metrics.METRICS}},
        "other": {"status": "ok", "summary": {m: (0.9, (0.8, 1.0), 3) for m in metrics.METRICS}},
    }
    lines = report._compare(results)
    assert all("Not a meaningful difference yet." in s for s in lines)
    assert not any("BETTER" in s or "WORSE" in s for s in lines)


def test_report_rejects_a_bad_folder(tmp_path, capsys):
    (tmp_path / "bad.json").write_text("{}", encoding="utf-8")
    assert report.main(["--dir", str(tmp_path)]) == 2
    assert "missing required key" in capsys.readouterr().err


# --- /label page --------------------------------------------------------------------

@pytest.fixture
def label_client(monkeypatch, tmp_path):
    import app as app_mod

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    real_load_feeds = rss.load_feeds

    def fake_load_feeds(urls, per_feed=None, fetch=None):
        return real_load_feeds(urls, per_feed=per_feed, fetch=lambda u: FEED_XML.encode("utf-8"))

    monkeypatch.setattr(app_mod.v2_rss, "load_feeds", fake_load_feeds)
    app_mod.app.config["TESTING"] = True
    return app_mod.app.test_client(), tmp_path


def test_label_page_renders_without_login(label_client):
    c, _ = label_client
    r = c.get("/label")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert 'action="/label/fetch"' in html and "Podsen" in html


def test_label_page_escapes_rss_titles(label_client):
    c, _ = label_client
    r = c.post("/label/fetch", data={"feeds": "https://x.example/feed", "budget": "30", "intent": "<b>x</b>"})
    html = r.get_data(as_text=True)
    assert r.status_code == 200
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<b>x</b>" not in html
    assert html.count('type="radio"') == 4 * 3


def _draft_id(html):
    return html.split('name="draft" value="')[1].split('"')[0]


def test_label_flow_saves_a_valid_file(label_client):
    c, tmp = label_client
    html = c.post(
        "/label/fetch", data={"name": "My <test>", "feeds": "https://x.example/feed", "budget": "45"}
    ).get_data(as_text=True)
    form = {"draft": _draft_id(html)}
    for i, lab in enumerate(["wanted", "not_wanted", "would_regret_skipping", "wanted"]):
        form[f"label_{i}"] = lab
    form["group_0"] = "a"
    form["group_1"] = "A "
    r = c.post("/label/save", data=form)
    assert r.status_code == 200

    files = list((tmp / "labels").glob("*.json"))
    assert len(files) == 1
    data = dataset.load(files[0])
    assert data["budget_minutes"] == 45 and data["name"] == "My <test>"
    assert data["duplicate_groups"] == [["guid-seconds", "guid-mmss"]]
    assert not list((tmp / "labels" / "drafts").glob("*.json")), "draft is cleaned up"


def test_label_save_keeps_choices_when_something_is_missing(label_client):
    c, tmp = label_client
    html = c.post("/label/fetch", data={"feeds": "https://x.example/feed", "budget": "45"}).get_data(as_text=True)
    r = c.post("/label/save", data={"draft": _draft_id(html), "label_0": "wanted", "group_0": "B"})
    body = r.get_data(as_text=True)
    assert r.status_code == 400
    assert "3 episode(s) still need a label" in body
    assert 'name="label_0" value="wanted" checked' in body
    assert 'name="group_0" maxlength="3" value="B"' in body
    assert not list((tmp / "labels").glob("*.json"))


def test_label_rejects_bad_budget_and_bad_draft_ids(label_client):
    c, _ = label_client
    r = c.post("/label/fetch", data={"feeds": "https://x.example/feed", "budget": "9999"})
    assert r.status_code == 400 and "Budget must be" in r.get_data(as_text=True)
    r = c.post("/label/save", data={"draft": "../../.env"})
    assert r.status_code == 400


# --- /label lock: this machine, or ?key=ADMIN_KEY --------------------------------------

REMOTE = {"REMOTE_ADDR": "203.0.113.9"}


def test_label_blocked_from_another_address(label_client, monkeypatch):
    c, tmp = label_client
    monkeypatch.setenv("ADMIN_KEY", "s3cret")
    assert c.get("/label", environ_base=REMOTE).status_code == 403
    assert c.post("/label/fetch", data={"feeds": "https://x.example/feed", "budget": "30"},
                  environ_base=REMOTE).status_code == 403
    assert c.post("/label/save", data={"draft": "0" * 32}, environ_base=REMOTE).status_code == 403
    assert c.get("/label?key=wrong", environ_base=REMOTE).status_code == 403
    assert not (tmp / "labels").exists(), "a blocked request must not write anything"


def test_label_blocked_through_a_tunnel_even_though_it_arrives_from_loopback(label_client, monkeypatch):
    """cloudflared connects from 127.0.0.1; its forwarding headers give it away."""
    c, _ = label_client
    monkeypatch.setenv("ADMIN_KEY", "s3cret")
    assert c.get("/label", headers={"X-Forwarded-For": "203.0.113.9"}).status_code == 403
    assert c.get("/label", headers={"CF-Connecting-IP": "203.0.113.9"}).status_code == 403


def test_label_without_admin_key_configured_never_accepts_a_key(label_client, monkeypatch):
    c, _ = label_client
    monkeypatch.setenv("ADMIN_KEY", "")
    assert c.get("/label?key=", environ_base=REMOTE).status_code == 403
    assert c.get("/label?key=anything", environ_base=REMOTE).status_code == 403


@pytest.mark.parametrize("addr", ["127.0.0.1", "::1"])
def test_label_allowed_locally(label_client, monkeypatch, addr):
    c, _ = label_client
    monkeypatch.setenv("ADMIN_KEY", "s3cret")
    r = c.get("/label", environ_base={"REMOTE_ADDR": addr})
    assert r.status_code == 200
    assert 'action="/label/fetch"' in r.get_data(as_text=True), "no key to carry locally"


def test_label_allowed_with_the_right_key_and_the_key_survives_to_save(label_client, monkeypatch):
    c, tmp = label_client
    monkeypatch.setenv("ADMIN_KEY", "s3&cret")
    qs = "?key=s3%26cret"

    r = c.get("/label" + qs, environ_base=REMOTE)
    assert r.status_code == 200
    assert f'action="/label/fetch{qs}"' in r.get_data(as_text=True)

    html = c.post("/label/fetch" + qs, data={"feeds": "https://x.example/feed", "budget": "45"},
                  environ_base=REMOTE).get_data(as_text=True)
    assert f'action="/label/save{qs}"' in html
    form = {"draft": _draft_id(html)}
    form.update({f"label_{i}": "wanted" for i in range(4)})
    r = c.post("/label/save" + qs, data=form, environ_base=REMOTE)
    assert r.status_code == 200
    assert f'href="/label{qs}"' in r.get_data(as_text=True)
    assert len(list((tmp / "labels").glob("*.json"))) == 1


# --- groups_touched: picking nothing from a group is not catching a duplicate ----------

def test_groups_touched_counts_groups_with_at_least_one_pick():
    eps = {x: {"id": x, "duration_min": 5} for x in "abcde"}
    groups = [["a", "b"], ["c", "d"], ["d", "e"]]
    s = metrics.score([eps["a"], eps["b"], eps["c"]], [eps["d"], eps["e"]], {}, 50, groups)
    assert (s["groups_touched"], s["groups_total"]) == (2, 3)


def test_picking_nothing_from_a_group_is_not_touched():
    s = metrics.score([], [{"id": "a", "duration_min": 5}, {"id": "b", "duration_min": 5}],
                      {}, 30, [["a", "b"]])
    assert s["duplicate_catch_rate"] == 1.0  # "at most one" is trivially true...
    assert s["groups_touched"] == 0          # ...which is why this is printed beside it


def test_report_prints_groups_touched_beside_duplicate_catch_rate():
    backlogs = dataset.load_dir(SAMPLE_DIR)
    results = report.evaluate(backlogs, methods=["newest_first", "random_fill"], rounds=200)
    table = report._table(results).splitlines()
    assert table[0].split()[-2:] == ["duplicate_catch_rate", "groups_touched"]
    assert results["newest_first"]["groups"] == (2, 2)
    assert table[2].endswith("2 of 2")
    touched, total = results["random_fill"]["groups"]
    assert total == 2 and 0 < touched < 2 and not float(touched).is_integer()


# --- data rule: V2 never touches Spotify data -----------------------------------------
# Spotify's developer policy forbids feeding Spotify data into AI/ML models. V2 may use
# only public RSS data and labels typed in by the user.

FORBIDDEN_MODULES = {"spotify", "app"}
FORBIDDEN_NAMES = {
    "uri", "tokens", "access_token", "refresh_token", "external_urls",
    "resume_point", "duration_ms", "release_date", "session",
}
FORBIDDEN_SUBSTRINGS = ("spotify:", "open.spotify.com", "api.spotify.com", "accounts.spotify.com")


def _violations(tree, where):
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [f"{where}: import {a.name}" for a in node.names
                      if a.name.split(".")[0] in FORBIDDEN_MODULES]
        elif isinstance(node, ast.ImportFrom):
            mod = (node.module or "").split(".")[0]
            if mod in FORBIDDEN_MODULES:
                found.append(f"{where}: from {node.module} import ...")
            found += [f"{where}: imports name {a.name}" for a in node.names if a.name in FORBIDDEN_NAMES]
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            found.append(f"{where}: uses name {node.id}")
        elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_NAMES:
            found.append(f"{where}: reads attribute .{node.attr}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value in FORBIDDEN_NAMES:
                found.append(f"{where}: reads field {node.value!r}")
            low = node.value.lower()
            found += [f"{where}: string mentions {s}" for s in FORBIDDEN_SUBSTRINGS if s in low]
    return found


def test_v2_modules_never_import_spotify_or_read_spotify_fields():
    files = sorted((ROOT / "v2").rglob("*.py"))
    assert len(files) >= 6, "scan must actually cover the V2 package"
    problems = []
    for f in files:
        problems += _violations(ast.parse(f.read_text(encoding="utf-8")), f.relative_to(ROOT))
    assert problems == []


def test_v2_does_not_pull_in_spotify_transitively():
    code = (
        "import sys; import v2, v2.rss, v2.dataset, v2.baselines, v2.metrics, "
        "v2.report, v2.label_views; print('spotify' in sys.modules)"
    )
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"


def test_label_routes_never_touch_the_spotify_session():
    tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
    funcs = [n for n in tree.body if isinstance(n, ast.FunctionDef)
             and (n.name.startswith("label_") or n.name == "_draft_path")]
    assert {f.name for f in funcs} >= {"label_home", "label_fetch", "label_save"}
    problems = []
    for f in funcs:
        problems += _violations(f, f"app.py:{f.name}")
        problems += [f"app.py:{f.name}: uses spotify" for n in ast.walk(f)
                     if isinstance(n, ast.Name) and n.id == "spotify"]
    assert problems == []


def test_v2_data_files_hold_only_rss_fields():
    for f in SAMPLE_DIR.glob("*.json"):
        for ep in json.loads(f.read_text(encoding="utf-8"))["episodes"]:
            assert set(ep) <= set(rss.EPISODE_FIELDS), f.name


def test_label_file_with_a_spotify_field_is_rejected(tmp_path):
    d = valid_backlog()
    d["episodes"][0]["uri"] = "spotify:episode:abc"
    p = tmp_path / "leak.json"
    p.write_text(json.dumps(d), encoding="utf-8")
    with pytest.raises(dataset.DatasetError, match="not allowed in V2 data: uri"):
        dataset.load(p)


def test_the_rule_check_itself_catches_a_violation():
    """Guard against the scan silently passing everything."""
    bad = ast.parse("import spotify\nx = ep['uri']\nfrom flask import session\n")
    found = _violations(bad, "bad.py")
    assert any("import spotify" in s for s in found)
    assert any("'uri'" in s for s in found)
    assert any("session" in s for s in found)
