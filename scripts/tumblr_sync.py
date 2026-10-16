"""Derive a bilingual NPF post from AIM HTML and update one existing Tumblr post.

Only Python's standard library is needed. Preview is the default. Authentication
is OAuth 1.0a; credentials are read from environment variables only.
"""

import argparse
import base64
import hashlib
import hmac
import html
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import secrets
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, urlencode, urlsplit
from urllib.request import Request

ROOT = Path(__file__).resolve().parents[1]
CREDENTIALS = (
    "TUMBLR_CONSUMER_KEY", "TUMBLR_CONSUMER_SECRET",
    "TUMBLR_OAUTH_TOKEN", "TUMBLR_OAUTH_TOKEN_SECRET",
)
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


class SyncError(Exception):
    pass


class Node:
    def __init__(self, tag="", attrs=()):
        self.tag = tag
        self.attrs = dict(attrs)
        self.children = []

    def has(self, name):
        return name in self.attrs.get("class", "").split()

    def find(self, predicate):
        result = []
        for child in self.children:
            if isinstance(child, Node):
                if predicate(child):
                    result.append(child)
                result.extend(child.find(predicate))
        return result

    def text(self):
        def raw(node):
            if isinstance(node, str):
                return node
            if node.tag == "br":
                return "\n"
            return "".join(raw(c) for c in node.children)
        return "\n".join(
            re.sub(r"[ \t\r\f\v]+", " ", line).strip()
            for line in raw(self).split("\n")
        ).strip()


class SourceParser(HTMLParser):
    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.root = Node("document")
        self.stack = [self.root]
        self.feed(source)
        self.close()

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs)
        self.stack[-1].children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                self.stack = self.stack[:i]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def one(nodes, label):
    if len(nodes) != 1:
        raise SyncError(f"Expected exactly one {label}; found {len(nodes)}. No post was changed.")
    return nodes[0]


def https_url(value, label):
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise SyncError(f"Invalid HTTPS URL for {label}.")
    return value


def text_block(text, subtype=None):
    block = {"type": "text", "text": text}
    if subtype:
        block["subtype"] = subtype
    if not text or len(text) > 4096:
        raise SyncError("A source text block is empty or exceeds Tumblr's 4096-character limit.")
    return block


def build_post(source, config):
    root = SourceParser(source).root
    main = one(root.find(lambda n: n.tag == "main"), "main element")
    for card in main.find(lambda n: n.has("figure-card")):
        one(card.find(lambda n: n.tag == "img"), "image in a photo card")
        for language in ("ja", "zh"):
            one(card.find(lambda n: n.has("figure-caption") and n.has(f"lang-{language}")),
                f"{language} caption in a photo card")
    heading = one(root.find(lambda n: n.tag == "h1"), "page heading").text()
    canonical = https_url(config["canonical_url"], "canonical page")
    hero = one(root.find(lambda n: n.has("hero")), "hero")
    eyebrow = one(hero.find(lambda n: n.has("eyebrow")), "event date").text()
    blocks = [text_block(heading, "heading1"), text_block(eyebrow)]
    blocks.append({"type": "link", "url": canonical,
                   "title": "完整互動紀錄 / 日本語・繁體中文のライブレポート"})
    counts = {"images": 0, "ja_captions": 0, "zh_captions": 0, "spotify_links": 0}

    def walk(node, language=None):
        if not isinstance(node, Node):
            return
        if node.has("lang-ja"):
            language = "ja"
        elif node.has("lang-zh"):
            language = "zh"
        if node.tag in {"script", "style", "canvas", "button"} or node.has("playlist-fallback"):
            return
        if node.tag == "img":
            url = https_url(node.attrs.get("src", ""), "image")
            blocks.append({"type": "image", "media": [{"url": url}],
                           "alt_text": node.attrs.get("alt", "")})
            counts["images"] += 1
            return
        if node.tag == "iframe":
            parsed = urlsplit(node.attrs.get("src", ""))
            if parsed.scheme != "https" or parsed.hostname != "open.spotify.com" or not re.fullmatch(r"/embed/playlist/[A-Za-z0-9]+", parsed.path):
                raise SyncError("An unsupported iframe was found. Review the source before publishing.")
            blocks.append({"type": "link", "url": "https://open.spotify.com" + parsed.path.replace("/embed", "", 1),
                           "title": "Spotify 自選播放清單 / 自分でまとめたプレイリスト"})
            counts["spotify_links"] += 1
            return
        if node.tag in {"h2", "h3", "h4", "p"} or node.has("fan-quote") or node.has("personal-note"):
            if node.has("personal-note"):
                for child in node.children:
                    walk(child, language)
                return
            label = {"ja": "日本語｜", "zh": "繁體中文｜"}.get(language, "")
            text = node.text()
            if node.has("figure-caption"):
                if language not in {"ja", "zh"}:
                    raise SyncError("A photo caption has no language marker.")
                counts[f"{language}_captions"] += 1
            blocks.append(text_block(label + text, "heading2" if node.tag.startswith("h") else None))
            return
        if node.tag == "span" and language:
            blocks.append(text_block({"ja": "日本語｜", "zh": "繁體中文｜"}[language] + node.text()))
            return
        for child in node.children:
            walk(child, language)

    walk(main)
    image_count = counts["images"]
    if image_count < config["minimum_images"] or image_count > 30:
        raise SyncError("Image count is outside the configured limits. Review the source.")
    if counts["ja_captions"] != image_count or counts["zh_captions"] != image_count:
        raise SyncError("Each photo must have both Japanese and Traditional Chinese captions.")
    if counts["spotify_links"] != 1:
        raise SyncError("Expected exactly one Spotify playlist.")
    if len(blocks) > 1000:
        raise SyncError("The post exceeds Tumblr's content-block limit.")
    payload = {"content": blocks, "layout": [], "source_url": canonical}
    if len(json.dumps(payload, ensure_ascii=False).encode()) > 950000:
        raise SyncError("The post is too large for Tumblr.")
    return payload, counts


def oauth_header(method, url, credentials, nonce=None, timestamp=None):
    consumer_key, consumer_secret, token, token_secret = credentials
    params = {"oauth_consumer_key": consumer_key, "oauth_token": token,
              "oauth_nonce": nonce or secrets.token_hex(16),
              "oauth_timestamp": str(timestamp if timestamp is not None else int(time.time())),
              "oauth_signature_method": "HMAC-SHA1", "oauth_version": "1.0"}
    escape = lambda value: quote(str(value), safe="~-._")
    parts = urlsplit(url)
    pairs = list(params.items()) + parse_qsl(parts.query, keep_blank_values=True)
    normalized = "&".join(f"{k}={v}" for k, v in sorted((escape(k), escape(v)) for k, v in pairs))
    base_url = f"{parts.scheme}://{parts.netloc}{parts.path}"
    base = "&".join(map(escape, (method.upper(), base_url, normalized)))
    key = escape(consumer_secret) + "&" + escape(token_secret)
    params["oauth_signature"] = base64.b64encode(hmac.new(key.encode(), base.encode(), hashlib.sha1).digest()).decode()
    return "OAuth " + ", ".join(f'{escape(k)}="{escape(v)}"' for k, v in sorted(params.items()))


class NoRedirects:
    """Use an explicit opener so signed API requests never follow redirects."""
    @staticmethod
    def open(request):
        from urllib.request import HTTPRedirectHandler, build_opener

        class Reject(HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                raise SyncError("Tumblr API returned a redirect; request stopped.")

        return build_opener(Reject()).open(request, timeout=45)


class TumblrClient:
    def __init__(self, credentials):
        self.credentials = credentials

    def call(self, method, path, payload=None):
        url = "https://api.tumblr.com/v2/" + path
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(url, data=body, method=method, headers={
            "Authorization": oauth_header(method, url, self.credentials),
            "Content-Type": "application/json", "Accept": "application/json",
            "User-Agent": "AIM-GitHub-Tumblr-Sync/1.0",
        })
        try:
            with NoRedirects.open(request) as response:
                result = json.load(response)
        except HTTPError as exc:
            raise SyncError(f"Tumblr API returned HTTP {exc.code}; check credentials and post access.") from None
        except (URLError, TimeoutError, OSError, ValueError):
            raise SyncError("Tumblr API request failed. Check the existing post before rerunning; no new post is ever created.") from None
        if result.get("meta", {}).get("status") != 200:
            raise SyncError("Tumblr did not confirm a successful API response.")
        return result["response"]


def validate_target(post, config):
    if str(post.get("id_string", post.get("id"))) != config["post_id"]:
        raise SyncError("Tumblr returned a different post ID.")
    if post.get("blog_name") != config["blog"].removesuffix(".tumblr.com"):
        raise SyncError("Tumblr returned a different blog.")
    if post.get("state") != "published" or post.get("trail"):
        raise SyncError("Only this original, already-public post can be updated.")
    if not post.get("is_blocks_post_format") and post.get("type") not in {"text", "blocks"}:
        raise SyncError("The existing post cannot be edited with NPF.")
    if not isinstance(post.get("content"), list):
        raise SyncError("Tumblr returned no NPF content.")


def projection(content):
    # Tumblr may rehost image URLs and add media metadata, so compare image alt
    # text and order, plus every text block and link URL instead of CDN URLs.
    result = []
    for block in content:
        kind = block.get("type")
        if kind == "text":
            result.append((kind, block.get("text"), block.get("subtype")))
        elif kind == "link":
            result.append((kind, block.get("url")))
        elif kind == "image":
            if not block.get("media"):
                raise SyncError("A returned image block has no media.")
            result.append((kind, block.get("alt_text", "")))
        else:
            raise SyncError("An unexpected content type was returned by Tumblr.")
    return result


def fetch_post(client, config):
    # The collection route returns the authoritative blog name alongside the
    # selected post. Unlike the NPF single-post route, its documented shape
    # includes the blog identity needed for our target check.
    query = urlencode({"id": config["post_id"], "npf": "true", "limit": 1})
    response = client.call("GET", f"blog/{config['blog']}/posts?{query}")
    expected_name = config["blog"].removesuffix(".tumblr.com")
    if response.get("blog", {}).get("name") != expected_name:
        raise SyncError("Tumblr returned a different blog identity.")
    posts = response.get("posts", [])
    if len(posts) != 1:
        raise SyncError("Tumblr did not return exactly one matching public post.")
    post = dict(posts[0])
    post.setdefault("blog_name", expected_name)
    validate_target(post, config)
    return post


def publish(client, payload, config, output):
    blog = config["blog"]
    post_id = config["post_id"]
    if not re.fullmatch(r"[a-z0-9-]+\.tumblr\.com", blog) or not re.fullmatch(r"[0-9]+", post_id):
        raise SyncError("Invalid configured blog or post ID.")
    path = f"blog/{blog}/posts/{post_id}"
    before = fetch_post(client, config)
    (output / "before.json").write_text(json.dumps(before, ensure_ascii=False, indent=2), encoding="utf-8")
    update = dict(payload, state="published", tags=",".join(before.get("tags", [])))
    if before.get("slug"):
        update["slug"] = before["slug"]
    response = client.call("PUT", path, update)
    if str(response.get("id", response.get("post_id"))) != post_id:
        raise SyncError("Tumblr did not confirm the expected updated post ID.")
    after = fetch_post(client, config)
    (output / "after.json").write_text(json.dumps(after, ensure_ascii=False, indent=2), encoding="utf-8")
    if projection(after["content"]) != projection(payload["content"]):
        raise SyncError("The post was updated but content readback differs. Review before.json and after.json before retrying.")
    if before.get("tags", []) != after.get("tags", []) or before.get("slug") != after.get("slug"):
        raise SyncError("The post was updated but its tags or URL slug changed. Inspect the saved backup.")
    return f"https://www.tumblr.com/{blog.removesuffix('.tumblr.com')}/{post_id}"


def preview_html(payload):
    pieces = ['<!doctype html><html lang="zh-Hant"><meta charset="utf-8">',
              '<meta name="viewport" content="width=device-width,initial-scale=1">',
              '<meta name="robots" content="noindex"><title>AIM Tumblr 發布預覽</title>',
              '<style>body{max-width:760px;margin:32px auto;padding:0 20px;font:17px/1.8 system-ui;color:#17221e;background:#fffdf6;overflow-wrap:anywhere}img{display:block;max-width:100%;height:auto}p{white-space:pre-line}a{color:#12624f}h1{font-size:1.8rem}h2{font-size:1.35rem}</style><main>']
    for block in payload["content"]:
        if block["type"] == "text":
            tag = {"heading1": "h1", "heading2": "h2"}.get(block.get("subtype"), "p")
            pieces.append(f'<{tag}>{html.escape(block["text"])}</{tag}>')
        elif block["type"] == "image":
            pieces.append(f'<img src="{html.escape(block["media"][0]["url"], quote=True)}" alt="{html.escape(block["alt_text"], quote=True)}" loading="lazy">')
        elif block["type"] == "link":
            pieces.append(f'<p><a href="{html.escape(block["url"], quote=True)}">{html.escape(block["title"])}</a></p>')
    return "\n".join(pieces) + "</main></html>\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--publish", action="store_true")
    args = parser.parse_args()
    try:
        config = json.loads((ROOT / ".github/tumblr.json").read_text(encoding="utf-8"))
        source = (ROOT / config["source"]).read_text(encoding="utf-8")
        payload, counts = build_post(source, config)
        output = ROOT / "tumblr-preview"
        output.mkdir(exist_ok=True)
        (output / "post.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        (output / "post.html").write_text(preview_html(payload), encoding="utf-8")
        status = "Preview checked. No Tumblr request was sent."
        if args.publish:
            if os.environ.get("TUMBLR_SYNC_ENABLED") != "true":
                raise SyncError("Publishing is disabled. Set the repository variable TUMBLR_SYNC_ENABLED=true after API authorization.")
            missing = [key for key in CREDENTIALS if not os.environ.get(key)]
            if missing:
                raise SyncError("Missing GitHub Actions secrets: " + ", ".join(missing))
            url = publish(TumblrClient(tuple(os.environ[key] for key in CREDENTIALS)), payload, config, output)
            status = "Updated and verified: " + url
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
        summary = (f"{status}\n\nSource: AIM/index.html\nSource SHA-256: {digest}\n"
                   f"Content blocks: {len(payload['content'])}\nImages: {counts['images']}\n"
                   f"Japanese captions: {counts['ja_captions']}\nTraditional Chinese captions: {counts['zh_captions']}\n"
                   f"Spotify links: {counts['spotify_links']}\n")
        (output / "result.txt").write_text(summary, encoding="utf-8")
        print(summary)
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as handle:
                handle.write("\n```text\n" + summary + "```\n")
        return 0
    except (SyncError, KeyError, OSError, ValueError) as exc:
        print(f"Sync stopped: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
