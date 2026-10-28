from __future__ import annotations

import io
import json
import sqlite3
import time

import yaml

from .clash import parse_clash
from .links import b64decode, parse_link
from .singbox import parse_singbox


def entries(data: str):
    data = data.strip("\ufeff \r\n\t")
    if not data:
        return
    if "://" not in data and not data.startswith(("{", "[", "proxies:")):
        try:
            decoded = b64decode("".join(data.split()))
            if "://" in decoded:
                data = decoded
        except (ValueError, UnicodeError):
            pass
    if data.startswith(("{", "[")) or ("proxies:" in data and not data.startswith(("vless://", "vmess://", "ss://", "trojan://", "hy2://", "hysteria2://", "tuic://"))):
        try:
            value = json.loads(data) if data.startswith(("{", "[")) else yaml.safe_load(data)
            if not isinstance(value, dict):
                raise ValueError("Ожидается объект proxies или outbounds")
            key = "proxies" if "proxies" in value else "outbounds"
            items = value[key]
            if not isinstance(items, list):
                raise ValueError("Ожидается список конфигов")
            parser = parse_clash if key == "proxies" else parse_singbox
            for n, item in enumerate(items, 1):
                yield n, json.dumps(item, ensure_ascii=False), parser, item
        except (ValueError, KeyError, yaml.YAMLError):
            yield 1, data, None, None
    else:
        for n, line in enumerate(io.StringIO(data), 1):
            line = line.strip()
            if line and not line.startswith("#"):
                yield n, line, parse_link, line


def import_configs(db, data, source="", protocols=None):
    protocols = protocols or {"vless", "vmess", "ss", "trojan"}
    counts = dict(received=0, accepted=0, duplicates=0, unsupported=0, parse_errors=0, errors=[])
    batch = []

    def flush():
        with db.transaction() as c:
            for record in batch:
                n, row = record
                try:
                    c.execute("""INSERT INTO proxies(hash,raw,config,protocol,host,remark,source,state,reason,created_at,updated_at)
                              VALUES(?,?,?,?,?,?,?,?,?,?,?)""", row)
                    state = row[7]
                    counts[{"new": "accepted", "unsupported": "unsupported", "parse_error": "parse_errors"}[state]] += 1
                    if state != "new" and len(counts["errors"]) < 100:
                        counts["errors"].append({"line": n, "outcome": state, "reason": row[8]})
                except sqlite3.IntegrityError:
                    counts["duplicates"] += 1
        batch.clear()

    for n, raw, parser, value in entries(data):
        counts["received"] += 1
        now = time.time()
        try:
            if parser is None:
                raise ValueError("Некорректный документ")
            config = parser(value)
            reason = config.support_reason(protocols)
            row = (config.hash, raw, json.dumps(config.asdict()), config.protocol, config.host, config.remark, source,
                   "unsupported" if reason else "new", reason, now, now)
        except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
            # Parser exceptions may contain passwords; keep only a fixed public message.
            row = (None, raw, None, None, None, "", source, "parse_error", "invalid_config: проверьте формат и обязательные поля", now, now)
        batch.append((n, row))
        if len(batch) >= 500:
            flush()
    flush()
    return counts
