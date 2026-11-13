import json

from flatwatch.sources.facebook import (
    clean_title,
    group_slug,
    iter_json_objects,
    merge_stream,
    parse_payloads,
)

G = "https://www.facebook.com/groups/123/"


def story(pid, text=None, t=None, actor="Test User", url=True, **extra):
    node = {"__typename": "Story", "id": f"S:{pid}", "post_id": pid, "comet_sections": {}}
    content = {"story": {"actors": [{"__typename": "User", "name": actor}]}}
    if text is not None:
        content["story"]["message"] = {"text": text}
    if url:
        content["story"]["wwwURL"] = f"{G}posts/{pid}/"
    node["comet_sections"]["content"] = content
    if t is not None:
        node["comet_sections"]["context_layout"] = {
            "story": {"comet_sections": {"metadata": [{"story": {"creation_time": t}}]}}
        }
    node.update(extra)
    return node


def feed(*nodes):
    return {"data": {"node": {"group_feed": {"edges": [{"node": n} for n in nodes]}}}}


def test_iter_json_objects_handles_prefix_and_multiple():
    body = 'for (;;);{"a": 1}\n{"b": 2}\n\n{"c": [3]}'
    assert list(iter_json_objects(body)) == [{"a": 1}, {"b": 2}, {"c": [3]}]


def test_iter_json_objects_skips_garbage_line():
    assert list(iter_json_objects('{"a":1}\nnot json\n{"b":2}')) == [{"a": 1}, {"b": 2}]


def test_basic_parse_sorted_newest_first():
    body = json.dumps(feed(story("1", "older 1BHK", 1000), story("2", "newer 2BHK flatmate", 2000)))
    posts = parse_payloads([body], [], G)
    assert [p.post_id for p in posts] == ["2", "1"]
    assert posts[0].text == "newer 2BHK flatmate"
    assert posts[0].author == "Test User"
    assert posts[0].url == f"{G}posts/2/"
    assert posts[0].created_at.timestamp() == 2000


def test_deferred_chunk_merged_by_path():
    base = feed(story("9", None, 1500))
    chunk = {
        "label": "CometFeedStory_message$defer",
        "path": ["node", "group_feed", "edges", 0, "node", "comet_sections", "content", "story"],
        "data": {"message": {"text": "Full text arriving later. HSR Layout 1BHK 25k"}},
    }
    posts = parse_payloads([json.dumps(base) + "\n" + json.dumps(chunk)], [], G)
    assert posts[0].text.startswith("Full text arriving later")


def test_streamed_edge_appended():
    base = feed(story("1", "first", 100))
    chunk = {"label": "x$stream", "path": ["node", "group_feed", "edges", 1], "data": {"node": story("2", "second", 200)}}
    trees = merge_stream([base, chunk])
    assert len(trees) == 1
    assert len(trees[0]["node"]["group_feed"]["edges"]) == 2
    assert {p.post_id for p in parse_payloads([json.dumps(base), json.dumps(chunk)], [], G)} == {"1", "2"}


def test_orphan_chunk_still_parsed():
    chunk = {"label": "x", "path": ["nope", 3], "data": {"node": story("5", "orphan", 50)}}
    assert [p.post_id for p in parse_payloads([json.dumps(chunk)], [], G)] == ["5"]


def test_shared_story_and_comments_ignored():
    shared = story("777", "ORIGINAL SHARED TEXT", 1)
    s = story("3", "my repost", 300, attached_story=shared,
              feedback={"comments": [{"__typename": "Comment", "message": {"text": "COMMENT"}}]})
    posts = parse_payloads([json.dumps(feed(s))], [], G)
    assert [p.post_id for p in posts] == ["3"]
    assert posts[0].text == "my repost"


def test_reshare_without_own_text_records_original():
    shared = {"id": "X", "permalink_url": "https://www.facebook.com/someone/posts/555/", "comet_sections": {}}
    s = story("11", None, 110, attached_story=shared)
    p = parse_payloads([json.dumps(feed(s))], [], G)[0]
    assert p.text == ""
    assert p.shared_from == "https://www.facebook.com/someone/posts/555/"


def test_longest_message_wins_and_fallback_url():
    s = story("4", "short", 10, url=False)
    s["comet_sections"]["extra"] = {"story": {"post_id": "4", "__typename": "Story", "message": {"text": "short but complete text"}}}
    p = parse_payloads([json.dumps(feed(s))], [], G)[0]
    assert p.text == "short but complete text"
    assert p.url == f"{G}posts/4/"


def test_images_from_attachments():
    s = story("6", "", 60, attachments=[{"media": {"photo_image": {"uri": "https://scontent/x.jpg"}}}])
    p = parse_payloads([json.dumps(feed(s))], [], G)[0]
    assert p.image_urls == ["https://scontent/x.jpg"]


def test_html_bbox_and_graphql_merged():
    html = json.dumps({"require": [["ScheduledServerJS", "handle", None, [{"__bbox": {"result": feed(story("8", "html text", 800))}}]]]})
    gql = json.dumps(feed(story("8", "html text, longer graphql version", None)))
    p = parse_payloads([gql], [html], G)
    assert len(p) == 1
    assert p[0].text == "html text, longer graphql version"
    assert p[0].created_at.timestamp() == 800


def test_stub_without_content_dropped():
    stub = {"__typename": "Story", "post_id": "10"}
    assert parse_payloads([json.dumps({"data": {"x": stub}})], [], G) == []


def test_helpers():
    assert group_slug("https://www.facebook.com/groups/flatsandflatmates/") == "flatsandflatmates"
    assert clean_title("(12) Flats and Flatmates Bangalore | Facebook") == "Flats and Flatmates Bangalore"
