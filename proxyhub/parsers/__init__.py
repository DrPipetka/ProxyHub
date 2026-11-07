from __future__ import annotations

import io
import json
import re
import sqlite3
import time

import yaml

from .clash import parse_clash
from .links import b64decode, parse_link
from .singbox import parse_singbox


def is_html(data: str) -> bool:
    """Recognize markup before splitting a downloaded web page into config lines."""
    return bool(re.match(r"\s*(?:<!--.*?-->\s*)*<(?:!doctype\s+html\b|/?[a-z][\w:-]*(?:\s|/?>))",
                         data.lstrip("\ufeff"), re.IGNORECASE | re.DOTALL))


def clash_flow_entries(data: str):
    """Recover independent, single-line Clash mappings from a broken YAML list.

    Only accept this unambiguous layout; do not split arbitrary YAML documents
    or multiline mappings after a document-level parse failure.
    """
    lines = data.splitlines()
    if not lines or not re.fullmatch(r"proxies:[ \t]*(?:#.*)?", lines[0]):
        return None
    records = []
    indent = None
    for n, line in enumerate(lines[1:], 2):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.fullmatch(r"( *)-[ \t]+(\{.*)", line)
        if not match or (indent is not None and len(match[1]) != indent):
            return None
        indent = len(match[1])
        records.append((n, match[2]))
    return records or None


def entries(data: str):
    data = data.strip("\ufeff \r\n\t")
    if not data:
        return
    if "://" not in data and not data.startswith(("{", "[", "proxies:")):
        try:
            decoded = b64decode("".join(data.split()))
            if "://" in decoded or is_html(decoded):
                data = decoded
        except (ValueError, UnicodeError):
            pass
    if is_html(data):
        yield 1, data, None, None
    elif data.startswith(("{", "[")) or ("proxies:" in data and not data.startswith(("vless://", "vmess://", "ss://", "trojan://", "hy2://", "hysteria2://", "tuic://"))):
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
            records = clash_flow_entries(data)
            if records is None:
                yield 1, data, None, None
            else:
                for n, raw in records:
                    try:
                        item = yaml.safe_load(raw)
                        if not isinstance(item, dict):
                            raise ValueError("Ожидается объект прокси")
                    except (ValueError, yaml.YAMLError):
                        yield n, raw, None, None
                    else:
                        yield n, raw, parse_clash, item
    else:
        for n, line in enumerate(io.StringIO(data), 1):
            line = line.strip()
            if line and not line.startswith("#"):
                yield n, line, parse_link, line


def import_configs(db, data, source="", protocols=None, *, persist_errors=True,
                   subscription_id=None, seen_token=None):
    protocols = protocols or {"vless", "vmess", "ss", "trojan"}
    counts = dict(received=0, accepted=0, duplicates=0, unsupported=0, parse_errors=0, errors=[])
    batch = []

    def flush():
        with db.transaction() as c:
            for record in batch:
                n, row = record
                try:
                    cursor = c.execute("""INSERT INTO proxies(hash,raw,config,protocol,host,remark,source,state,reason,created_at,updated_at,independent_import)
                              VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", (*row, int(subscription_id is None)))
                    pid = cursor.lastrowid
                    state = row[7]
                    counts[{"new": "accepted", "unsupported": "unsupported", "parse_error": "parse_errors"}[state]] += 1
                    if state != "new" and len(counts["errors"]) < 100:
                        counts["errors"].append({"line": n, "outcome": state, "reason": row[8]})
                except sqlite3.IntegrityError:
                    existing = c.execute("SELECT id FROM proxies WHERE hash=?", (row[0],)).fetchone()
                    if existing is None:
                        raise
                    pid = existing[0]
                    counts["duplicates"] += 1
                if subscription_id is not None:
                    c.execute("""INSERT INTO subscription_proxies VALUES(?,?,?)
                        ON CONFLICT(subscription_id,proxy_id) DO UPDATE SET seen_token=excluded.seen_token""",
                              (subscription_id, pid, seen_token))
                else:
                    c.execute("UPDATE proxies SET independent_import=1 WHERE id=?", (pid,))
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
            if not persist_errors or is_html(raw):
                counts["parse_errors"] += 1
                if len(counts["errors"]) < 100:
                    reason = "html_document: укажите Raw-ссылку на файл подписки" if is_html(raw) else "invalid_config: проверьте формат и обязательные поля"
                    counts["errors"].append({"line": n, "outcome": "parse_error", "reason": reason})
                continue
            # Parser exceptions may contain passwords; keep only a fixed public message.
            row = (None, raw, None, None, None, "", source, "parse_error", "invalid_config: проверьте формат и обязательные поля", now, now)
        batch.append((n, row))
        if len(batch) >= 500:
            flush()
    flush()
    return counts
