"""追加式审计日志（事件溯源）。

- 所有状态变更只追加，不修改、不删除；事件一经写入其 seq/hash 即固化。
- 每条事件含 prev_hash，形成哈希链，任何篡改/缺序在重放时立刻暴露。
- 撤回不是删除事件，而是追加一条对应的撤回事件；迟到反馈以事件发生时间入账，
  但 seq 反映实际到达顺序，两套时间线都保留。
- 内存模式用于单测；文件模式每行一个 JSON，进程重启后原样重建。
"""

import json
import os
import threading

from common import DomainError, now_ts

_CHAIN_SEP = "|"


def _event_hash(prev_hash, payload_text):
    import hashlib
    return hashlib.sha256((prev_hash + _CHAIN_SEP + payload_text).encode("utf-8")).hexdigest()


class Journal:
    def __init__(self, path=None):
        self._path = path
        self._lock = threading.RLock()
        self._events = []
        self._listeners = []
        if path:
            os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
            self._load()

    def _load(self):
        if not os.path.exists(self._path):
            return
        prev = "0" * 64
        with open(self._path, "r", encoding="utf-8") as fh:
            for line_no, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise DomainError("journal_corrupt", f"日志第 {line_no} 行无法解析") from exc
                payload_text = json.dumps(event["payload"], ensure_ascii=False, sort_keys=True,
                                          separators=(",", ":"))
                expected = _event_hash(prev, payload_text)
                if event.get("prev_hash") != prev or event.get("hash") != expected:
                    raise DomainError("journal_corrupt", f"日志哈希链在第 {line_no} 行断裂")
                self._events.append(event)
                prev = event["hash"]

    @property
    def events(self):
        with self._lock:
            return list(self._events)

    def subscribe(self, listener):
        """注册重放/写入时的事件消费者（聚合根）。"""
        self._listeners.append(listener)

    def append(self, kind, payload, *, actor=None, ts=None):
        """写入一条事件。ts 是业务发生时间；记录到达时间另存 recorded_at。

        反馈类事件允许 ts 早于已有事件（迟到/乱序），但哈希链始终按写入顺序。
        """
        event_ts = now_ts() if ts is None else ts
        record = {**payload}
        with self._lock:
            prev = self._events[-1]["hash"] if self._events else "0" * 64
            payload_text = json.dumps(record, ensure_ascii=False, sort_keys=True,
                                      separators=(",", ":"))
            event = {
                "seq": len(self._events) + 1,
                "kind": kind,
                "actor": actor,
                "ts": event_ts,
                "recorded_at": now_ts(),
                "prev_hash": prev,
                "payload": record,
            }
            event["hash"] = _event_hash(prev, payload_text)
            if self._path:
                with open(self._path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())
            self._events.append(event)
            for listener in self._listeners:
                listener(event)
            return event

    def replay(self, listener):
        """把历史事件按 seq 顺序喂给新建的聚合根。"""
        with self._lock:
            for event in list(self._events):
                listener(event)

    def tail_seq(self):
        with self._lock:
            return len(self._events)
