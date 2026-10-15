"""The web app's chat history on disk (config "chat_dir"): one JSON file per conversation.

Off unless the run config names a folder; then the Chat view lists, opens, saves and deletes conversations there
(GET/POST/DELETE /api/chats).  A file is {"id", "title", "created", "updated", "messages": [...]}, written whole
(a temporary file, then a rename), so a crash leaves the last complete version.  The messages are the web app's
own records: the server keeps them as they come and never reads them into a prompt."""
from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path

ID = re.compile(r"[A-Za-z0-9_-]{8,64}")
MAX_TITLE = 200
MAX_CHAT_BYTES = 64 << 20           # one conversation; the request body limit (STRATA_MAX_BODY_MIB) applies first


class ChatStore:
    def __init__(self, folder):
        self.dir = Path(folder)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self._heads = {}                # id -> (mtime_ns, size, head): the list without rereading unchanged files

    def _file(self, chat_id: str) -> Path:
        if not isinstance(chat_id, str) or not ID.fullmatch(chat_id):
            raise ValueError("a chat id is 8 to 64 letters, digits, '-' or '_'")
        return self.dir / f"{chat_id}.json"

    @staticmethod
    def _head(chat: dict, size: int) -> dict:
        return {"id": chat.get("id"), "title": chat.get("title") or "", "created": chat.get("created"),
                "updated": chat.get("updated"), "messages": len(chat.get("messages") or []), "bytes": size}

    def list(self, query: str = "") -> list[dict]:
        """Every conversation, the last changed first.  `query`: only those whose title or text contains it (any
        case)."""
        q = query.casefold().strip()
        out = []
        with self.lock:
            seen = set()
            for f in self.dir.glob("*.json"):
                chat_id = f.stem
                if not ID.fullmatch(chat_id):
                    continue
                try:
                    st = f.stat()
                    cached = self._heads.get(chat_id)
                    chat = None
                    if q or not cached or cached[:2] != (st.st_mtime_ns, st.st_size):
                        chat = json.loads(f.read_text(encoding="utf-8"))
                        if not isinstance(chat, dict):
                            continue
                        self._heads[chat_id] = (st.st_mtime_ns, st.st_size, self._head({**chat, "id": chat_id},
                                                                                       st.st_size))
                except (OSError, ValueError):
                    continue
                seen.add(chat_id)
                if q and q not in (chat.get("title") or "").casefold() and q not in _text(chat).casefold():
                    continue
                out.append(self._heads[chat_id][2])
            for gone in set(self._heads) - seen:
                del self._heads[gone]
        out.sort(key=lambda h: h.get("updated") or 0, reverse=True)
        return out

    def get(self, chat_id: str) -> dict | None:
        f = self._file(chat_id)
        try:
            chat = json.loads(f.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        return chat if isinstance(chat, dict) else None

    def put(self, chat_id: str, chat: dict) -> dict:
        f = self._file(chat_id)
        if not isinstance(chat, dict) or not isinstance(chat.get("messages"), list):
            raise ValueError("send {\"title\": ..., \"messages\": [...]}")
        title = chat.get("title")
        clean = {"id": chat_id, "title": title[:MAX_TITLE] if isinstance(title, str) else "",
                 "created": _number(chat.get("created")), "updated": _number(chat.get("updated")),
                 "messages": chat["messages"]}
        raw = json.dumps(clean, ensure_ascii=False, indent=1)
        if len(raw.encode()) > MAX_CHAT_BYTES:
            raise ValueError(f"a conversation is limited to {MAX_CHAT_BYTES >> 20} MiB")
        with self.lock:
            tmp = f.with_name(f".{f.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            tmp.write_text(raw, encoding="utf-8")
            os.replace(tmp, f)
        return self._head(clean, len(raw.encode()))

    def delete(self, chat_id: str) -> bool:
        f = self._file(chat_id)
        with self.lock:
            try:
                f.unlink()
            except FileNotFoundError:
                return False
            self._heads.pop(chat_id, None)
        return True


def _number(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _text(chat: dict) -> str:
    return "\n".join(str(m.get("text") or "") for m in chat.get("messages") or [] if isinstance(m, dict))
