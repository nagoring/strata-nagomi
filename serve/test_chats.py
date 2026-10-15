"""serve/test_chats.py - the web app's chat history (config "chat_dir", GET/POST/DELETE /api/chats), against the
mock engine: off without the key, a conversation saved, listed (the last changed first), searched, read and deleted,
bad ids refused, and only Strata's own page may read or change them.

    python -m unittest serve.test_chats
"""
from __future__ import annotations

import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from serve.chats import ChatStore
from serve.frontend import ChatTemplate
from serve.server import ByteTokenizer, MockEngine, Service, serve

ROOT = Path(__file__).resolve().parents[1]


def chat(title, text, updated):
    return {"title": title, "created": updated - 10, "updated": updated,
            "messages": [{"role": "user", "text": text, "time": updated - 10},
                         {"role": "assistant", "text": "ok", "reasoning": "", "time": updated}]}


class Store(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = ChatStore(Path(self.dir.name) / "chats")

    def tearDown(self):
        self.dir.cleanup()

    def test_round_trip(self):
        self.store.put("aaaaaaaa", chat("first", "hello", 100))
        self.store.put("bbbbbbbb", chat("second", "world", 200))
        self.assertEqual([h["id"] for h in self.store.list()], ["bbbbbbbb", "aaaaaaaa"])
        self.assertEqual(self.store.list()[0]["messages"], 2)
        self.assertEqual(self.store.get("aaaaaaaa")["messages"][0]["text"], "hello")
        self.assertEqual([h["id"] for h in self.store.list("WORLD")], ["bbbbbbbb"])
        self.assertEqual([h["id"] for h in self.store.list("first")], ["aaaaaaaa"])
        self.store.put("aaaaaaaa", chat("first", "hello again", 300))         # a save replaces the file whole
        self.assertEqual(self.store.list()[0]["id"], "aaaaaaaa")
        self.assertTrue(self.store.delete("aaaaaaaa"))
        self.assertFalse(self.store.delete("aaaaaaaa"))
        self.assertIsNone(self.store.get("aaaaaaaa"))
        self.assertEqual([h["id"] for h in self.store.list()], ["bbbbbbbb"])
        self.assertEqual(sorted(p.name for p in self.store.dir.iterdir()), ["bbbbbbbb.json"])   # no temporary files

    def test_refused(self):
        for bad in ("short", "../../etc/passwd", "a" * 65, "abc/defgh", "abcdefgh.json", "", None):
            with self.subTest(id=bad), self.assertRaises(ValueError):
                self.store.get(bad)
        for body in (None, [], {"title": "x"}, {"messages": "x"}):
            with self.subTest(body=body), self.assertRaises(ValueError):
                self.store.put("aaaaaaaa", body)

    def test_a_broken_file_is_skipped(self):
        self.store.put("aaaaaaaa", chat("ok", "x", 1))
        (self.store.dir / "cccccccc.json").write_text("{not json", encoding="utf-8")
        self.assertEqual([h["id"] for h in self.store.list()], ["aaaaaaaa"])


class Http(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        tok = ByteTokenizer()
        self.svc = Service(MockEngine(tok, "ok", max_context=4096), tok, ChatTemplate(ROOT / "serve/chat_template.jinja"))
        self.svc.chats = ChatStore(Path(self.dir.name))
        self.httpd = serve(self.svc, port=0)
        self.host = f"127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.dir.cleanup()

    def call(self, method, path, body=None, headers=None):
        h = {"Content-Type": "application/json", **(headers or {})}
        req = urllib.request.Request(f"http://{self.host}{path}", method=method, headers=h,
                                     data=None if body is None else json.dumps(body).encode())
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())

    def test_save_list_read_delete(self):
        own = {"Origin": f"http://{self.host}"}
        status, head = self.call("POST", "/api/chats/aaaaaaaa", chat("日本語のタイトル", "こんにちは", 100), own)
        self.assertEqual((status, head["title"], head["messages"]), (200, "日本語のタイトル", 2))
        status, v = self.call("GET", "/api/chats", headers=own)
        self.assertEqual((status, [h["id"] for h in v["chats"]]), (200, ["aaaaaaaa"]))
        status, v = self.call("GET", "/api/chats?q=%E3%81%93%E3%82%93", headers=own)          # "こん"
        self.assertEqual(len(v["chats"]), 1)
        status, v = self.call("GET", "/api/chats/aaaaaaaa", headers=own)
        self.assertEqual((status, v["messages"][0]["text"]), (200, "こんにちは"))
        self.assertEqual(self.call("GET", "/api/chats/bbbbbbbb", headers=own)[0], 404)
        self.assertEqual(self.call("GET", "/api/chats/..%2F..%2Fx", headers=own)[0], 400)
        self.assertEqual(self.call("DELETE", "/api/chats/aaaaaaaa", headers=own), (200, {"deleted": True}))
        self.assertEqual(self.call("DELETE", "/api/chats/aaaaaaaa", headers=own), (404, {"deleted": False}))

    def test_only_from_strata_s_own_page(self):
        self.call("POST", "/api/chats/aaaaaaaa", chat("t", "x", 1))                         # no Origin: a local script
        evil = {"Origin": "http://evil.example"}
        self.assertEqual(self.call("GET", "/api/chats", headers=evil)[0], 403)
        self.assertEqual(self.call("GET", "/api/chats/aaaaaaaa", headers=evil)[0], 403)
        self.assertEqual(self.call("POST", "/api/chats/aaaaaaaa", chat("t", "y", 2), evil)[0], 403)
        self.assertEqual(self.call("DELETE", "/api/chats/aaaaaaaa", headers=evil)[0], 403)
        self.assertEqual(self.call("POST", "/api/chats/aaaaaaaa", chat("t", "y", 2),
                                   {"Content-Type": "text/plain"})[0], 415)              # a form from any site
        self.assertEqual(self.svc.chats.get("aaaaaaaa")["messages"][0]["text"], "x")

    def test_api_key(self):
        self.svc.api_key = "secret"
        self.assertEqual(self.call("GET", "/api/chats")[0], 401)
        self.assertEqual(self.call("GET", "/api/chats", headers={"Authorization": "Bearer secret"})[0], 200)

    def test_off_without_chat_dir(self):
        self.svc.chats = None
        self.assertEqual(self.call("GET", "/api/chats")[0], 404)
        self.assertEqual(self.call("POST", "/api/chats/aaaaaaaa", chat("t", "x", 1))[0], 404)
        self.assertEqual(self.call("DELETE", "/api/chats/aaaaaaaa")[0], 404)


if __name__ == "__main__":
    unittest.main()
