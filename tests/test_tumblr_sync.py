import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("tumblr_sync", ROOT / "scripts/tumblr_sync.py")
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)


class ConversionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = json.loads((ROOT / ".github/tumblr.json").read_text(encoding="utf-8"))
        cls.source = (ROOT / cls.config["source"]).read_text(encoding="utf-8")

    def test_all_photos_and_bilingual_captions_survive(self):
        payload, counts = sync.build_post(self.source, self.config)
        self.assertEqual(counts["images"], 10)
        self.assertEqual(counts["ja_captions"], 10)
        self.assertEqual(counts["zh_captions"], 10)
        source_images = sync.SourceParser(self.source).root.find(lambda n: n.tag == "main")[0].find(lambda n: n.tag == "img")
        urls = [b["media"][0]["url"] for b in payload["content"] if b["type"] == "image"]
        self.assertEqual(urls, [n.attrs["src"] for n in source_images])
        text = "\n".join(b.get("text", "") for b in payload["content"])
        for node in sync.SourceParser(self.source).root.find(lambda n: n.has("figure-caption")):
            self.assertIn(node.text(), text)

    def test_article_and_notes_are_taken_from_current_source(self):
        payload, _ = sync.build_post(self.source, self.config)
        combined = "\n".join(b.get("text", "") for b in payload["content"])
        main = sync.SourceParser(self.source).root.find(lambda n: n.tag == "main")[0]
        for node in main.find(lambda n: n.tag in {"h2", "h3", "h4", "p"} and not n.has("playlist-fallback")):
            self.assertIn(node.text(), combined)
        self.assertIn("第二天確實很耗體力", combined)
        self.assertIn("2日目はかなり体力を使った", combined)
        self.assertNotIn("function switchLang", combined)
        self.assertEqual({b["type"] for b in payload["content"]}, {"text", "link", "image"})

    def test_playlist_is_a_single_native_link_with_no_iframe(self):
        payload, _ = sync.build_post(self.source, self.config)
        urls = [b["url"] for b in payload["content"] if b["type"] == "link"]
        self.assertEqual(urls, [self.config["canonical_url"], "https://open.spotify.com/playlist/2wACTIO0ZjuIvjWktL0L2q"])
        self.assertNotIn("<iframe", json.dumps(payload))

    def test_missing_translation_stops_conversion(self):
        broken = self.source.replace('figure-caption handwritten lang-ja', 'figure-caption handwritten', 1)
        with self.assertRaises(sync.SyncError):
            sync.build_post(broken, self.config)

    def test_missing_article_container_stops_conversion(self):
        with self.assertRaises(sync.SyncError):
            sync.build_post(self.source.replace("<main>", "<div>").replace("</main>", "</div>"), self.config)

    def test_unsafe_media_url_stops_conversion(self):
        with self.assertRaises(sync.SyncError):
            sync.build_post(self.source.replace("https://res.cloudinary.com", "javascript:bad", 1), self.config)

    def test_unknown_iframe_stops_conversion(self):
        with self.assertRaises(sync.SyncError):
            sync.build_post(self.source.replace("https://open.spotify.com/embed", "https://example.com/embed", 1), self.config)

    def test_preview_escapes_html(self):
        html = sync.preview_html({"content": [{"type": "text", "text": "<script>bad()</script>"}]})
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_dry_run_makes_no_network_request(self):
        with patch.object(sync.NoRedirects, "open", side_effect=AssertionError("Network used")), patch("sys.argv", ["tumblr_sync.py", "--dry-run"]):
            self.assertEqual(sync.main(), 0)


class OAuthTests(unittest.TestCase):
    def test_signature_matches_oauth_published_reference_vector(self):
        credentials = ("dpf43f3p2l4k3l03", "kd94hf93k423kf44", "nnch734d00sl2jdk", "pfkkdhi9sl3r4s00")
        header = sync.oauth_header("GET", "http://photos.example.net/photos?file=vacation.jpg&size=original", credentials,
                                   nonce="kllo9940pd9333jh", timestamp=1191242096)
        params = dict(part.strip().split("=", 1) for part in header[6:].split(","))
        self.assertEqual(unquote(params["oauth_signature"].strip('"')), "tR3+Ty81lMeYAr/Fid0kMTYa/WM=")
        self.assertNotIn(credentials[1], header)
        self.assertNotIn(credentials[3], header)


class PublishingTests(unittest.TestCase):
    def setUp(self):
        self.config = {"blog": "halafish.tumblr.com", "post_id": "822847655413792768"}
        self.payload = {"content": [{"type": "text", "text": "日本語｜新しい記録"},
                                    {"type": "image", "alt_text": "甲子園", "media": [{"url": "https://example.com/photo.jpg"}]}],
                        "layout": [], "source_url": "https://live.itigre.com/AIM/"}
        self.before = {"id_string": self.config["post_id"], "blog_name": "halafish", "state": "published",
                       "is_blocks_post_format": True, "type": "blocks", "trail": [],
                       "tags": ["AIMYON", "甲子園"], "slug": "original-slug", "content": [{"type": "text", "text": "Old"}]}

    def client(self, before=None, bad_readback=False):
        before = copy.deepcopy(self.before if before is None else before)
        after = copy.deepcopy(before)
        after["content"] = copy.deepcopy(self.payload["content"])
        after["content"][1]["media"][0]["url"] = "https://64.media.tumblr.com/rehosted.jpg"
        if bad_readback:
            after["content"][0]["text"] = "Truncated"

        class Client:
            def __init__(self):
                self.calls = []

            def call(client, method, path, payload=None):
                client.calls.append((method, path, payload))
                if method == "PUT":
                    return {"id": self.config["post_id"]}
                return copy.deepcopy(before if len(client.calls) == 1 else after)
        return Client()

    def test_one_existing_post_is_updated_backed_up_and_verified(self):
        client = self.client()
        with tempfile.TemporaryDirectory() as tmp:
            sync.publish(client, self.payload, self.config, Path(tmp))
            self.assertTrue((Path(tmp) / "before.json").exists())
            self.assertTrue((Path(tmp) / "after.json").exists())
        self.assertEqual([call[0] for call in client.calls], ["GET", "PUT", "GET"])
        self.assertIn(self.config["post_id"], client.calls[1][1])
        update = client.calls[1][2]
        self.assertEqual(update["tags"], "AIMYON,甲子園")
        self.assertEqual(update["slug"], "original-slug")
        self.assertNotIn("date", update)

    def test_wrong_blog_or_post_id_is_never_written(self):
        for key, value in (("blog_name", "someone-else"), ("id_string", "1")):
            before = dict(self.before, **{key: value})
            client = self.client(before=before)
            with tempfile.TemporaryDirectory() as tmp, self.assertRaises(sync.SyncError):
                sync.publish(client, self.payload, self.config, Path(tmp))
            self.assertEqual([call[0] for call in client.calls], ["GET"])

    def test_private_draft_and_reblog_targets_are_never_written(self):
        for changes in ({"state": "private"}, {"state": "draft"}, {"trail": [{"blog": "other"}]}):
            client = self.client(before=dict(self.before, **changes))
            with tempfile.TemporaryDirectory() as tmp, self.assertRaises(sync.SyncError):
                sync.publish(client, self.payload, self.config, Path(tmp))
            self.assertEqual(len(client.calls), 1)

    def test_changed_readback_is_reported_as_failure(self):
        client = self.client(bad_readback=True)
        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(sync.SyncError, "readback differs"):
            sync.publish(client, self.payload, self.config, Path(tmp))


if __name__ == "__main__":
    unittest.main()
