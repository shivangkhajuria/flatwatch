from flatwatch.sources.reddit import parse_dom_items, parse_listing_json


def child(pid, title, selftext="", created=1_700_000_000, **extra):
    d = {"id": pid, "title": title, "selftext": selftext, "author": "u_test",
         "permalink": f"/r/testsub/comments/{pid}/slug/", "created_utc": created, "url": "https://www.reddit.com/x"}
    d.update(extra)
    return {"kind": "t3", "data": d}


def listing(*children):
    return {"kind": "Listing", "data": {"children": list(children)}}


def test_json_parse_newest_first_title_plus_body():
    posts = parse_listing_json(listing(child("a1", "Old post", "body A", 100), child("b2", "New 1BHK HSR", "25k rent", 200)), "testsub")
    assert [p.post_id for p in posts] == ["b2", "a1"]
    assert posts[0].text == "New 1BHK HSR\n\n25k rent"
    assert posts[0].url == "https://www.reddit.com/r/testsub/comments/b2/slug/"
    assert posts[0].created_at.timestamp() == 200
    assert posts[0].via == "json" and posts[0].id == "reddit:b2"


def test_json_skips_stickied_and_non_posts():
    posts = parse_listing_json(listing(child("s1", "Rules", stickied=True), {"kind": "t1", "data": {"id": "c"}}, child("ok", "Room")), "testsub")
    assert [p.post_id for p in posts] == ["ok"]


def test_json_title_only_and_images():
    gallery = {"m1": {"s": {"u": "https://preview.redd.it/1.jpg"}}}
    posts = parse_listing_json(listing(
        child("i1", "Pic post", url="https://i.redd.it/abc.png"),
        child("g1", "Gallery", media_metadata=gallery, preview={"images": [{"source": {"url": "https://preview.redd.it/p.jpg"}}]}),
    ), "testsub")
    by = {p.post_id: p for p in posts}
    assert by["i1"].text == "Pic post" and by["i1"].image_urls == ["https://i.redd.it/abc.png"]
    assert by["g1"].image_urls == ["https://preview.redd.it/p.jpg", "https://preview.redd.it/1.jpg"]


def test_json_empty_or_bad_shape():
    assert parse_listing_json({}, "x") == []
    assert parse_listing_json(None, "x") == []


def test_dom_parse():
    items = [
        {"id": "t3_aa", "permalink": "/r/testsub/comments/aa/x/", "title": "Flatmate needed", "author": "bob",
         "created": "2026-11-12T10:00:00.000000+0000", "body": "Indiranagar, 18k", "images": []},
        {"id": "t3_bb", "permalink": "/r/testsub/comments/bb/y/", "title": "Pinned", "stickied": True},
        {"id": "t3_cc", "permalink": "/r/testsub/comments/cc/z/", "title": "Newer", "author": "eve",
         "created": "2026-11-12T11:00:00Z", "body": "", "images": ["https://i.redd.it/q.jpg"]},
    ]
    posts = parse_dom_items(items, "testsub")
    assert [p.post_id for p in posts] == ["cc", "aa"]
    assert posts[1].text == "Flatmate needed\n\nIndiranagar, 18k"
    assert posts[0].text == "Newer" and posts[0].image_urls == ["https://i.redd.it/q.jpg"]
    assert posts[1].created_at.hour == 10 and posts[1].via == "dom"


def test_flair_included_in_text():
    items = [{"id": "t3_f1", "permalink": "/r/x/comments/f1/a/", "title": "Room in 3BHK", "author": "a",
              "created": "2026-11-12T10:00:00Z", "body": "", "flair": "Looking for a flatmate (Male)"},
             {"id": "t3_f2", "permalink": "/r/x/comments/f2/b/", "title": "1BHK HSR", "author": "b",
              "created": "2026-11-12T09:00:00Z", "body": "25k", "flair": "1BHK"}]
    a, b = parse_dom_items(items, "x")
    assert a.text == "Room in 3BHK\n[Looking for a flatmate (Male)]"
    assert b.text == "1BHK HSR\n[1BHK]\n\n25k"
    j = parse_listing_json(listing(child("j1", "Flat", "body", link_flair_text="2BHK")), "x")[0]
    assert j.text == "Flat\n[2BHK]\n\nbody"
