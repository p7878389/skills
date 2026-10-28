#!/usr/bin/env python3
"""Yearning v3.0.x 只读查询客户端（仅依赖 Python 标准库）。

接口来源：Yearning v3.0.4 源码
  - POST /login | /ldap                      登录，payload.token 为 JWT（8 小时有效）
  - GET  /api/v2/fetch/source?tp=query       有查询权限的数据源
  - GET  /api/v2/fetch/query_status          payload=true 表示没有生效中的查询会话（或已过期）
  - GET  /api/v2/fetch/is_query              payload.status=是否开启查询审核
  - POST /api/v2/query/post                  创建查询工单（未开启审核时自动通过）
  - GET  /api/v2/query/schema|tables         库 / 表列表
  - WS   /api/v2/query/results?user=<用户名>  执行查询，消息体为 msgpack，
         token 放在 Sec-WebSocket-Protocol 头
"""

import argparse
import base64
import csv
import io
import json
import os
import re
import socket
import ssl
import struct
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_CONFIG = os.path.expanduser("~/.config/yearning/config.json")
CACHE_DIR = os.path.expanduser("~/.config/yearning/.cache")
VERBOSE = False


class YearningError(Exception):
    def __init__(self, msg, exit_code=1):
        super().__init__(msg)
        self.exit_code = exit_code


def log(msg):
    if VERBOSE:
        print("[debug] " + msg, file=sys.stderr)


# ---------------------------------------------------------------- 配置与鉴权

def load_profile(name=None):
    path = os.environ.get("YEARNING_CONFIG") or DEFAULT_CONFIG
    cfg = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    profiles = cfg.get("profiles") or {}
    name = name or os.environ.get("YEARNING_PROFILE") or cfg.get("default") or "default"
    prof = dict(profiles.get(name) or {})
    for key, env in (("url", "YEARNING_URL"), ("username", "YEARNING_USERNAME"),
                     ("password", "YEARNING_PASSWORD"), ("token", "YEARNING_TOKEN")):
        if os.environ.get(env):
            prof[key] = os.environ[env]
    if os.environ.get("YEARNING_LDAP"):
        prof["ldap"] = os.environ["YEARNING_LDAP"].lower() in ("1", "true", "yes")
    if not prof.get("url"):
        raise YearningError("未配置 Yearning 地址：编辑 %s（模板见 skill 目录 config.example.json）"
                            "或设置环境变量 YEARNING_URL" % path, 2)
    prof["url"] = prof["url"].rstrip("/")
    prof["_name"] = name
    prof["_path"] = path
    return prof


def jwt_claims(token):
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part))
    except Exception:
        return {}


class Client:
    def __init__(self, prof, timeout=60):
        self.prof = prof
        self.base = prof["url"]
        self.timeout = timeout
        # 内网地址直连，不走 http_proxy（本机代理常不认内网域名）
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.token = None
        self.user = None

    # -- token 缓存
    def _cache_path(self):
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", self.prof["_name"])
        return os.path.join(CACHE_DIR, safe + ".json")

    def _use_token(self, token):
        claims = jwt_claims(token)
        self.token = token
        self.user = claims.get("name") or self.prof.get("username")
        return claims

    def ensure_login(self, force=False):
        if self.token and not force:
            return
        if self.prof.get("token") and not force:
            self._use_token(self.prof["token"])
            return
        cache = self._cache_path()
        if not force and os.path.exists(cache):
            try:
                with open(cache, encoding="utf-8") as f:
                    data = json.load(f)
                if (data.get("url") == self.base and data.get("username") == self.prof.get("username")
                        and data.get("exp", 0) - time.time() > 300):
                    self._use_token(data["token"])
                    log("使用缓存 token")
                    return
            except Exception:
                pass
        self.login()

    def login(self):
        username, password = self.prof.get("username"), self.prof.get("password")
        if not username or not password:
            raise YearningError("未配置账号密码：在 %s 的 profile 中填写 username/password，"
                                "或设置 YEARNING_USERNAME/YEARNING_PASSWORD（也可直接给 YEARNING_TOKEN）"
                                % self.prof["_path"], 2)
        path = "/ldap" if self.prof.get("ldap") else "/login"
        body = {"username": username, "password": password}
        if self.prof.get("ldap"):
            body["is_ldap"] = True
        try:
            resp = self._request("POST", path, body=body, auth=False)
        except YearningError as e:
            raise YearningError("登录失败：%s" % e, 2)
        token = (resp.get("payload") or {}).get("token")
        if not token:
            raise YearningError("登录失败：%s" % resp.get("text"), 2)
        claims = self._use_token(token)
        os.makedirs(CACHE_DIR, exist_ok=True)
        cache = self._cache_path()
        fd = os.open(cache, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"url": self.base, "username": username, "token": token,
                       "exp": claims.get("exp", time.time() + 3600)}, f)
        log("登录成功，token 已缓存")

    # -- HTTP
    def _request(self, method, path, params=None, body=None, auth=True):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if auth:
            req.add_header("Authorization", "Bearer " + self.token)
        log("%s %s" % (method, url))
        try:
            with self.opener.open(req, timeout=self.timeout) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise _Unauthorized()
            raise YearningError("HTTP %s %s: %s" % (e.code, path, e.read()[:300].decode("utf-8", "replace")))
        except urllib.error.URLError as e:
            raise YearningError("无法连接 %s：%s" % (self.base, e.reason))
        if not raw.strip():
            return {"code": 1200, "payload": None, "text": ""}
        try:
            resp = json.loads(raw)
        except ValueError:
            raise YearningError("非 JSON 响应 %s: %s" % (path, raw[:300].decode("utf-8", "replace")))
        if isinstance(resp, dict) and resp.get("code", 1200) != 1200:
            raise YearningError("Yearning 返回 %s：%s" % (resp.get("code"), resp.get("text")))
        return resp

    def api(self, method, path, params=None, body=None):
        self.ensure_login()
        try:
            return self._request(method, path, params, body)
        except _Unauthorized:
            if self.prof.get("token") and not self.prof.get("password"):
                raise YearningError("token 已失效（401），请更新 YEARNING_TOKEN 或改用账号密码", 2)
            self.ensure_login(force=True)
            try:
                return self._request(method, path, params, body)
            except _Unauthorized:
                raise YearningError("鉴权失败（401）", 2)

    # -- 业务接口
    def sources(self):
        return self.api("GET", "/api/v2/fetch/source", {"tp": "query"}).get("payload") or []

    def resolve_source(self, key):
        items = self.sources()
        if not key:
            raise YearningError("缺少 --source；可选：\n" + format_sources(items))
        for it in items:
            if key in (it.get("source_id"), it.get("source")):
                return it
        low = key.lower()
        hits = [it for it in items if low in (it.get("source") or "").lower()]
        if len(hits) == 1:
            return hits[0]
        raise YearningError("数据源 %r %s；当前账号可查询的数据源：\n%s"
                            % (key, "匹配到多个" if hits else "不存在或无查询权限",
                               format_sources(hits or items)))

    def query_status(self):
        expired = self.api("GET", "/api/v2/fetch/query_status").get("payload")
        audit = self.api("GET", "/api/v2/fetch/is_query").get("payload") or {}
        return {"session_active": not bool(expired), "audit_enabled": bool(audit.get("status")),
                "export_enabled": bool(audit.get("export"))}

    def ensure_session(self):
        """与前端进入查询页的逻辑一致：无生效会话且未开启审核时自动开启会话。"""
        st = self.query_status()
        if st["session_active"]:
            return st
        if st["audit_enabled"]:
            raise YearningError(
                "实例开启了查询审核，当前账号没有已批准的查询工单（或已过期）。"
                "需先提交查询申请并等待审核人批准：yearning.py apply --source <数据源> --reason <原因> --yes",
                3)
        self.api("POST", "/api/v2/query/post", body={"export": 0})
        log("未开启查询审核，已自动开启查询会话")
        return self.query_status()


class _Unauthorized(Exception):
    pass


def format_sources(items):
    if not items:
        return "  （空）"
    return "\n".join("  - %s  source_id=%s  环境=%s" % (it.get("source"), it.get("source_id"),
                                                        it.get("idc") or it.get("id_c") or "-")
                     for it in items)


# ---------------------------------------------------------------- msgpack

def mp_pack(obj):
    if obj is None:
        return b"\xc0"
    if obj is True:
        return b"\xc3"
    if obj is False:
        return b"\xc2"
    if isinstance(obj, int):
        if 0 <= obj < 128:
            return struct.pack("B", obj)
        if -32 <= obj < 0:
            return struct.pack("b", obj)
        if 0 <= obj < 2 ** 32:
            return b"\xce" + struct.pack(">I", obj)
        return b"\xd3" + struct.pack(">q", obj)
    if isinstance(obj, float):
        return b"\xcb" + struct.pack(">d", obj)
    if isinstance(obj, str):
        b = obj.encode("utf-8")
        n = len(b)
        if n < 32:
            return struct.pack("B", 0xA0 | n) + b
        if n < 256:
            return b"\xd9" + struct.pack("B", n) + b
        if n < 65536:
            return b"\xda" + struct.pack(">H", n) + b
        return b"\xdb" + struct.pack(">I", n) + b
    if isinstance(obj, (bytes, bytearray)):
        return b"\xc6" + struct.pack(">I", len(obj)) + bytes(obj)
    if isinstance(obj, (list, tuple)):
        head = struct.pack("B", 0x90 | len(obj)) if len(obj) < 16 else b"\xdd" + struct.pack(">I", len(obj))
        return head + b"".join(mp_pack(x) for x in obj)
    if isinstance(obj, dict):
        head = struct.pack("B", 0x80 | len(obj)) if len(obj) < 16 else b"\xdf" + struct.pack(">I", len(obj))
        return head + b"".join(mp_pack(k) + mp_pack(v) for k, v in obj.items())
    raise TypeError("msgpack 不支持的类型: %r" % type(obj))


def mp_unpack(data):
    obj, pos = _mp_read(data, 0)
    return obj


def _mp_read(d, p):
    b = d[p]
    p += 1
    if b <= 0x7F:
        return b, p
    if 0x80 <= b <= 0x8F:
        return _mp_map(d, p, b & 0x0F)
    if 0x90 <= b <= 0x9F:
        return _mp_arr(d, p, b & 0x0F)
    if 0xA0 <= b <= 0xBF:
        n = b & 0x1F
        return d[p:p + n].decode("utf-8", "replace"), p + n
    if b >= 0xE0:
        return b - 0x100, p
    if b == 0xC0:
        return None, p
    if b == 0xC2:
        return False, p
    if b == 0xC3:
        return True, p
    fixed = {0xCA: ">f", 0xCB: ">d", 0xCC: ">B", 0xCD: ">H", 0xCE: ">I", 0xCF: ">Q",
             0xD0: ">b", 0xD1: ">h", 0xD2: ">i", 0xD3: ">q"}
    if b in fixed:
        fmt = fixed[b]
        size = struct.calcsize(fmt)
        return struct.unpack(fmt, d[p:p + size])[0], p + size
    lens = {0xD9: ">B", 0xDA: ">H", 0xDB: ">I", 0xC4: ">B", 0xC5: ">H", 0xC6: ">I"}
    if b in lens:
        fmt = lens[b]
        size = struct.calcsize(fmt)
        n = struct.unpack(fmt, d[p:p + size])[0]
        p += size
        raw = d[p:p + n]
        return (raw.decode("utf-8", "replace") if b in (0xD9, 0xDA, 0xDB) else raw), p + n
    if b in (0xDC, 0xDD):
        fmt = ">H" if b == 0xDC else ">I"
        size = struct.calcsize(fmt)
        return _mp_arr(d, p + size, struct.unpack(fmt, d[p:p + size])[0])
    if b in (0xDE, 0xDF):
        fmt = ">H" if b == 0xDE else ">I"
        size = struct.calcsize(fmt)
        return _mp_map(d, p + size, struct.unpack(fmt, d[p:p + size])[0])
    ext_fixed = {0xD4: 1, 0xD5: 2, 0xD6: 4, 0xD7: 8, 0xD8: 16}
    if b in ext_fixed or b in (0xC7, 0xC8, 0xC9):
        if b in ext_fixed:
            n = ext_fixed[b]
        else:
            fmt = {0xC7: ">B", 0xC8: ">H", 0xC9: ">I"}[b]
            size = struct.calcsize(fmt)
            n = struct.unpack(fmt, d[p:p + size])[0]
            p += size
        etype = struct.unpack("b", d[p:p + 1])[0]
        raw = d[p + 1:p + 1 + n]
        return _mp_ext(etype, raw), p + 1 + n
    raise YearningError("无法解析 msgpack 字节 0x%02x" % b)


def _mp_ext(etype, raw):
    if etype == -1:  # timestamp
        if len(raw) == 4:
            sec, nsec = struct.unpack(">I", raw)[0], 0
        elif len(raw) == 8:
            v = struct.unpack(">Q", raw)[0]
            sec, nsec = v & 0x3FFFFFFFF, v >> 34
        else:
            nsec, sec = struct.unpack(">Iq", raw)
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(sec + nsec / 1e9))
    return raw.hex()


def _mp_arr(d, p, n):
    out = []
    for _ in range(n):
        v, p = _mp_read(d, p)
        out.append(v)
    return out, p


def _mp_map(d, p, n):
    out = {}
    for _ in range(n):
        k, p = _mp_read(d, p)
        v, p = _mp_read(d, p)
        out[k] = v
    return out, p


# ---------------------------------------------------------------- WebSocket（RFC 6455 客户端，最小实现）

class WebSocket:
    def __init__(self, base_url, path, token, timeout):
        u = urllib.parse.urlparse(base_url)
        tls = u.scheme == "https"
        host = u.hostname
        port = u.port or (443 if tls else 80)
        sock = socket.create_connection((host, port), timeout=timeout)
        if tls:
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
        self.sock = sock
        self.buf = b""
        key = base64.b64encode(os.urandom(16)).decode()
        req = ("GET %s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Protocol: %s\r\n"
               "Authorization: Bearer %s\r\nOrigin: %s://%s\r\n\r\n"
               % (path, u.netloc, key, token, token, u.scheme, u.netloc))
        log("WS GET %s" % path)
        sock.sendall(req.encode())
        while b"\r\n\r\n" not in self.buf:
            chunk = sock.recv(4096)
            if not chunk:
                break
            self.buf += chunk
        head, _, self.buf = self.buf.partition(b"\r\n\r\n")
        status = head.split(b"\r\n", 1)[0].decode("latin-1")
        if " 101 " not in status + " ":
            if " 401 " in status:
                raise _Unauthorized()
            raise YearningError("WebSocket 握手失败：%s %s" % (status, self.buf[:200].decode("utf-8", "replace")))

    def _recv_exact(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise YearningError("WebSocket 连接被服务端关闭")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send(self, payload, opcode=0x2):
        mask = os.urandom(4)
        n = len(payload)
        if n < 126:
            head = struct.pack("!BB", 0x80 | opcode, 0x80 | n)
        elif n < 65536:
            head = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, n)
        else:
            head = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(head + mask + masked)

    def recv(self):
        message = b""
        while True:
            b1, b2 = self._recv_exact(2)
            fin, opcode = b1 & 0x80, b1 & 0x0F
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack("!H", self._recv_exact(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", self._recv_exact(8))[0]
            mask = self._recv_exact(4) if b2 & 0x80 else None
            data = self._recv_exact(n)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if opcode == 0x8:
                raise YearningError("WebSocket 被服务端关闭（token 无效时服务端会直接断开，可先 check --relogin）")
            if opcode == 0x9:
                self.send(data, 0xA)
                continue
            if opcode == 0xA:
                continue
            message += data
            if fin:
                return message

    def close(self):
        try:
            self.send(b"", 0x8)
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


def run_ws_query(client, source_id, schema, sql, timeout):
    path = "/api/v2/query/results?" + urllib.parse.urlencode({"user": client.user})
    ws = WebSocket(client.base, path, client.token, timeout)
    try:
        ws.send(mp_pack({"type": "0", "sql": sql, "schema": schema, "source_id": source_id, "heartbeat": 0}))
        while True:
            msg = mp_unpack(ws.recv())
            if msg.get("heartbeat") == 1:
                continue
            try:
                ws.send(mp_pack({"type": "1"}))
            except Exception:
                pass
            return msg
    finally:
        ws.close()


# ---------------------------------------------------------------- SQL 只读校验

READ_FIRST = {"select", "show", "desc", "describe", "explain", "with"}
FORBID_PATTERNS = [
    (r"\bfor\s+update\b", "SELECT ... FOR UPDATE 会加锁"),
    (r"\bfor\s+share\b", "FOR SHARE 会加锁"),
    (r"\block\s+in\s+share\s+mode\b", "LOCK IN SHARE MODE 会加锁"),
    (r"\binto\s+(outfile|dumpfile)\b", "禁止导出到文件"),
    (r"\binto\s+@", "禁止 SELECT INTO 变量"),
    (r"\b(sleep|benchmark|get_lock|release_lock)\s*\(", "禁止 sleep/benchmark/锁函数"),
]
WITH_FORBID = r"\b(insert|update|delete|replace)\b"


def scan_sql(sql):
    """返回 (去注释的 SQL, 同长度且字符串/标识符内容被空白化的 SQL)。"""
    out, blank = [], []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if c == "#" or (c == "-" and nxt == "-" and (i + 2 >= n or sql[i + 2] in " \t\r\n")):
            j = sql.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i))
            blank.append(" " * (j - i))
            i = j
            continue
        if c == "/" and nxt == "*":
            j = sql.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append(" " * (j - i))
            blank.append(" " * (j - i))
            i = j
            continue
        if c in "'\"`":
            j = i + 1
            while j < n:
                if sql[j] == "\\" and c != "`":
                    j += 2
                    continue
                if sql[j] == c:
                    if j + 1 < n and sql[j + 1] == c:
                        j += 2
                        continue
                    break
                j += 1
            j = min(j + 1, n)
            out.append(sql[i:j])
            blank.append(c + " " * max(j - i - 2, 0) + (c if j - i >= 2 else ""))
            i = j
            continue
        out.append(c)
        blank.append(c)
        i += 1
    return "".join(out), "".join(blank)


def prepare_sql(sql, limit):
    """校验只读并为缺少 LIMIT 的 SELECT/WITH 追加 LIMIT；返回 (发送的 SQL, 提示列表)。"""
    clean, blank = scan_sql(sql)
    stmts, notes, start = [], [], 0
    for idx in [m.start() for m in re.finditer(";", blank)] + [len(blank)]:
        s_clean, s_blank = clean[start:idx].strip(), blank[start:idx].strip().lower()
        start = idx + 1
        if not s_clean:
            continue
        first = re.match(r"[a-z]+", s_blank)
        first = first.group(0) if first else ""
        if first not in READ_FIRST:
            raise YearningError("只允许只读语句（SELECT/SHOW/DESC/EXPLAIN/WITH），拒绝执行：%s" % s_clean[:120])
        for pat, why in FORBID_PATTERNS:
            if re.search(pat, s_blank):
                raise YearningError("%s：%s" % (why, s_clean[:120]))
        if first == "with" and re.search(WITH_FORBID, s_blank):
            raise YearningError("WITH 语句中出现写操作关键字，拒绝执行：%s" % s_clean[:120])
        if limit and first in ("select", "with") and not re.search(r"\blimit\b", s_blank):
            s_clean = "%s LIMIT %d" % (s_clean, limit)
            notes.append("已自动追加 LIMIT %d：%s" % (limit, s_clean[:100]))
        stmts.append(s_clean)
    if not stmts:
        raise YearningError("SQL 为空")
    return ";\n".join(stmts) + ";", notes


# ---------------------------------------------------------------- 输出

def cell_text(v):
    if v is None:
        return "NULL"
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def disp_width(s):
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in s)


def clip(s, width):
    s = s.replace("\r", " ").replace("\n", "\\n").replace("\t", " ")
    if disp_width(s) <= width:
        return s
    out, w = "", 0
    for ch in s:
        cw = 2 if unicodedata.east_asian_width(ch) in "WF" else 1
        if w + cw > width - 1:
            break
        out += ch
        w += cw
    return out + "…"


def result_sets(msg):
    sets = []
    for r in msg.get("results") or []:
        cols = [f.get("title") or f.get("dataIndex") for f in (r.get("field") or [])]
        rows = r.get("data") or []
        if not cols and rows:
            cols = list(rows[0].keys())
        sets.append((cols, [[row.get(c) for c in cols] for row in rows]))
    return sets


def render(sets, fmt, max_rows, max_width):
    buf = io.StringIO()
    if fmt == "json":
        json.dump([{"columns": c, "rows": [[cell_text(v) if v is not None else None for v in r] for r in rows],
                    "row_count": len(rows)} for c, rows in sets], buf, ensure_ascii=False, indent=1)
        buf.write("\n")
        return buf.getvalue()
    for i, (cols, rows) in enumerate(sets, 1):
        if len(sets) > 1:
            buf.write("# 结果集 %d\n" % i)
        if fmt == "vertical":
            shown = rows[:max_rows] if max_rows else rows
            pad = max([disp_width(str(c)) for c in cols] or [0])
            for k, r in enumerate(shown, 1):
                buf.write("*************************** %d. row ***************************\n" % k)
                for c, v in zip(cols, r):
                    buf.write(" " * (pad - disp_width(str(c))) + "%s: %s\n" % (c, cell_text(v)))
            if len(shown) < len(rows):
                buf.write("…（共 %d 行，仅显示前 %d 行；用 --out 保存完整结果）\n" % (len(rows), len(shown)))
        elif fmt in ("csv", "tsv"):
            w = csv.writer(buf, delimiter="," if fmt == "csv" else "\t", lineterminator="\n")
            w.writerow(cols)
            for r in rows:
                w.writerow([cell_text(v) if v is not None else "" for v in r])
        else:
            shown = rows[:max_rows] if max_rows else rows
            table = [[clip(str(c), max_width) for c in cols]] + \
                    [[clip(cell_text(v), max_width) for v in r] for r in shown]
            widths = [max(disp_width(row[j]) for row in table) for j in range(len(cols))]
            for k, row in enumerate(table):
                buf.write(" | ".join(v + " " * (widths[j] - disp_width(v)) for j, v in enumerate(row)).rstrip() + "\n")
                if k == 0:
                    buf.write("-+-".join("-" * w for w in widths) + "\n")
            if len(shown) < len(rows):
                buf.write("…（共 %d 行，仅显示前 %d 行；用 --out 保存完整结果）\n" % (len(rows), len(shown)))
        if len(sets) > 1:
            buf.write("\n")
    return buf.getvalue()


# ---------------------------------------------------------------- 命令

def cmd_config(client, args):
    p = client.prof
    masked = {k: v for k, v in p.items() if not k.startswith("_")}
    for k in ("password", "token"):
        if masked.get(k):
            masked[k] = "***（已配置）"
    print(json.dumps({"config_file": p["_path"], "profile": p["_name"], "settings": masked},
                     ensure_ascii=False, indent=1))


def cmd_check(client, args):
    client.ensure_login(force=args.relogin)
    st = client.query_status()
    claims = jwt_claims(client.token)
    exp = claims.get("exp")
    print("连接：%s  用户：%s（%s）" % (client.base, client.user, claims.get("real_name", "")))
    if exp:
        print("token 到期：%s" % time.strftime("%Y-%m-%d %H:%M", time.localtime(exp)))
    print("查询审核：%s  导出权限：%s  查询会话：%s" % (
        "开启" if st["audit_enabled"] else "关闭", "有" if st["export_enabled"] else "无",
        "生效中" if st["session_active"] else "未开启/已过期"))
    print("可查询数据源：\n" + format_sources(client.sources()))


def cmd_sources(client, args):
    items = client.sources()
    if args.json:
        print(json.dumps(items, ensure_ascii=False, indent=1))
    else:
        print(format_sources(items))


def cmd_schemas(client, args):
    src = client.resolve_source(args.source)
    info = client.api("GET", "/api/v2/query/schema", {"source_id": src["source_id"]}).get("payload") or {}
    for it in info.get("info") or []:
        print(it.get("title") or it.get("key") if isinstance(it, dict) else it)


def cmd_tables(client, args):
    src = client.resolve_source(args.source)
    info = client.api("GET", "/api/v2/query/tables",
                      {"source_id": src["source_id"], "schema": args.schema}).get("payload") or {}
    names = [it.get("title") or it.get("key") if isinstance(it, dict) else it for it in info.get("table") or []]
    if args.filter:
        names = [n for n in names if args.filter.lower() in str(n).lower()]
    print("\n".join(str(n) for n in names) if names else "（无匹配表）")


SYSTEM_SCHEMAS = {"information_schema", "mysql", "performance_schema", "sys"}
STAGING_HINT = re.compile(r"预|staging|stag|test|测试|dev", re.I)
TABLE_CACHE_TTL = 24 * 3600


def source_env(src):
    """按数据源名称和环境字段粗分 prod / staging。"""
    text = "%s %s" % (src.get("source") or "", src.get("idc") or src.get("id_c") or "")
    return "staging" if STAGING_HINT.search(text) else "prod"


def table_catalog(client, refresh=False):
    """所有可查询数据源的 {source_id: {"source", "env", "schemas": {schema: [table]}}}，按 profile 缓存 24 小时。"""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", client.prof["_name"])
    cache = os.path.join(CACHE_DIR, "tables-%s.json" % safe)
    if not refresh and os.path.exists(cache):
        try:
            with open(cache, encoding="utf-8") as f:
                data = json.load(f)
            if data.get("url") == client.base and time.time() - data.get("ts", 0) < TABLE_CACHE_TTL:
                log("使用表清单缓存")
                return data["catalog"], data["ts"], True
        except Exception:
            pass
    from concurrent.futures import ThreadPoolExecutor

    client.ensure_login()

    def schemas_of(src):
        info = client.api("GET", "/api/v2/query/schema", {"source_id": src["source_id"]}).get("payload") or {}
        names = [it.get("key") or it.get("title") if isinstance(it, dict) else it for it in info.get("info") or []]
        return src, [n for n in names if n and n.lower() not in SYSTEM_SCHEMAS]

    def tables_of(job):
        src, schema = job
        info = client.api("GET", "/api/v2/query/tables",
                          {"source_id": src["source_id"], "schema": schema}).get("payload") or {}
        return src["source_id"], schema, [it.get("title") or it.get("key") if isinstance(it, dict) else it
                                          for it in info.get("table") or []]

    catalog, errors = {}, []
    with ThreadPoolExecutor(max_workers=8) as pool:
        jobs = []
        for fut in [pool.submit(schemas_of, s) for s in client.sources()]:
            try:
                src, schemas = fut.result()
            except YearningError as e:
                errors.append(str(e))
                continue
            catalog[src["source_id"]] = {"source": src.get("source"), "env": source_env(src), "schemas": {}}
            jobs += [(src, sc) for sc in schemas]
        for fut in [pool.submit(tables_of, j) for j in jobs]:
            try:
                sid, schema, tables = fut.result()
            except YearningError as e:
                errors.append(str(e))
                continue
            catalog[sid]["schemas"][schema] = tables
    for e in errors:
        print("[警告] 部分数据源读取失败：%s" % e, file=sys.stderr)
    ts = time.time()
    if not errors:
        os.makedirs(CACHE_DIR, exist_ok=True)
        fd = os.open(cache, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"url": client.base, "ts": ts, "catalog": catalog}, f, ensure_ascii=False)
    return catalog, ts, False


def cmd_locate_table(client, args):
    catalog, ts, cached = table_catalog(client, args.refresh)
    want = args.table.lower()
    hits = []
    for sid, src in catalog.items():
        if args.env != "all" and src["env"] != args.env:
            continue
        for schema, tables in src["schemas"].items():
            for t in tables:
                name = str(t).lower()
                if name == want or (args.like and want in name):
                    hits.append((src["env"], src["source"], sid, schema, t))
    prefer = client.prof.get("prefer_sources") or {}
    # 条目写数据源名，或「数据源/库」精确到库
    is_default = lambda h: bool({h[1], "%s/%s" % (h[1], h[3])} & set(prefer.get(h[0]) or []))  # noqa: E731
    hits.sort(key=lambda h: (h[0] != "prod", not is_default(h), h[1], h[3], str(h[4])))
    age = int((time.time() - ts) / 60)
    print("表清单%s（%d 个数据源）" % ("来自 %d 分钟前的缓存，--refresh 刷新" % age if cached else "为实时读取",
                                  len(catalog)))
    if not hits:
        print("未找到表 %r（环境=%s）%s" % (args.table, args.env, "" if args.like else "；可加 --like 模糊匹配"))
        return
    for h in hits:
        env, source, sid, schema, t = h
        print("  [%s] 数据源=%s  库=%s  表=%s  source_id=%s%s"
              % (env, source, schema, t, sid, "  ★默认" if is_default(h) else ""))


def cmd_apply(client, args):
    src = client.resolve_source(args.source)
    st = client.query_status()
    if st["session_active"]:
        print("当前已有生效中的查询会话，无需申请。")
        return
    body = {"source_id": src["source_id"], "text": args.reason, "export": 0}
    if not args.yes:
        print("[预演] 将向 %s 提交查询申请：数据源=%s(%s) 原因=%s；查询审核=%s"
              % (client.base, src.get("source"), src["source_id"], args.reason,
                 "开启，需审核人批准" if st["audit_enabled"] else "关闭，自动通过"))
        print("确认后加 --yes 执行。")
        return
    resp = client.api("POST", "/api/v2/query/post", body=body)
    print(resp.get("text") or "已提交")


def cmd_query(client, args):
    if args.file:
        with open(args.file, encoding="utf-8") as f:
            sql = f.read()
    elif args.sql == "-" or (not args.sql and not sys.stdin.isatty()):
        sql = sys.stdin.read()
    else:
        sql = args.sql
    if not sql or not sql.strip():
        raise YearningError("缺少 SQL：用 --sql、--file 或标准输入")
    final_sql, notes = prepare_sql(sql, args.limit)
    for n in notes:
        print("[提示] " + n, file=sys.stderr)
    src = client.resolve_source(args.source)
    client.ensure_session()

    for attempt in (1, 2):
        try:
            msg = run_ws_query(client, src["source_id"], args.schema, final_sql, args.timeout)
        except _Unauthorized:
            if attempt == 2:
                raise YearningError("WebSocket 鉴权失败（401）", 2)
            client.ensure_login(force=True)
            continue
        if msg.get("status") and attempt == 1:
            client.ensure_session()  # 会话在查询途中过期：未开启审核时自动续期后重试一次
            continue
        break
    if msg.get("status"):
        raise YearningError("查询会话已过期或未批准，请重新申请查询权限", 3)
    if msg.get("error"):
        raise YearningError("查询失败：%s" % msg["error"])

    sets = result_sets(msg)
    text = render(sets, args.format, args.max_rows, args.max_width)
    summary = "数据源=%s 库=%s 耗时=%sms 结果集=%d 行数=%s" % (
        src.get("source"), args.schema, msg.get("query_time"), len(sets), ",".join(str(len(r)) for _, r in sets))
    if args.out:
        full = render(sets, args.format, 0, 10 ** 6) if args.format in ("table", "vertical") else text
        with open(args.out, "w", encoding="utf-8", newline="") as f:
            f.write(full)
        print("%s\n完整结果已写入 %s" % (summary, os.path.abspath(args.out)))
    else:
        sys.stdout.write(text)
        print("\n" + summary, file=sys.stderr)


def build_parser():
    p = argparse.ArgumentParser(description="Yearning 只读查询客户端")
    p.add_argument("--profile", help="配置中的 profile 名")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("config", help="查看当前配置（已脱敏）")
    c = sub.add_parser("check", help="检查登录、查询会话与可查询数据源")
    c.add_argument("--relogin", action="store_true", help="忽略缓存强制重新登录")
    c = sub.add_parser("sources", help="列出可查询数据源")
    c.add_argument("--json", action="store_true")
    c = sub.add_parser("schemas", help="列出数据源下的库")
    c.add_argument("-s", "--source", required=True, help="数据源名称或 source_id")
    c = sub.add_parser("tables", help="列出库中的表")
    c.add_argument("-s", "--source", required=True)
    c.add_argument("-d", "--schema", required=True)
    c.add_argument("-f", "--filter", help="按表名子串过滤")
    c = sub.add_parser("locate-table", help="在所有可查询数据源中定位表所在的数据源和库")
    c.add_argument("table", help="表名（默认精确匹配，忽略大小写）")
    c.add_argument("--like", action="store_true", help="按子串模糊匹配")
    c.add_argument("--env", choices=["all", "prod", "staging"], default="all",
                   help="按数据源名称/环境粗分：含 预/staging/test/测试/dev 的视为 staging，其余视为 prod")
    c.add_argument("--refresh", action="store_true", help="忽略 24 小时缓存重新读取")
    c = sub.add_parser("apply", help="提交查询申请（开启查询审核时需审核人批准）")
    c.add_argument("-s", "--source", required=True)
    c.add_argument("--reason", required=True, help="申请原因")
    c.add_argument("--yes", action="store_true", help="真正提交（默认只预演）")
    c = sub.add_parser("query", help="执行只读查询")
    c.add_argument("-s", "--source", required=True)
    c.add_argument("-d", "--schema", required=True, help="库名")
    c.add_argument("--sql", help="SQL 文本，'-' 表示从标准输入读取")
    c.add_argument("--file", help="从文件读取 SQL")
    c.add_argument("--limit", type=int, default=100, help="SELECT 无 LIMIT 时自动追加，0 关闭（默认 100）")
    c.add_argument("--format", choices=["table", "vertical", "json", "csv", "tsv"], default="table",
                   help="vertical 类似 mysql \\G，适合宽行与 SHOW CREATE TABLE")
    c.add_argument("--max-rows", type=int, default=50, help="table/vertical 格式显示的最大行数（默认 50）")
    c.add_argument("--max-width", type=int, default=60, help="table 格式单元格最大宽度")
    c.add_argument("--out", help="把完整结果写入文件")
    c.add_argument("--timeout", type=int, default=120, help="查询超时秒数")
    return p


def main(argv=None):
    global VERBOSE
    args = build_parser().parse_args(argv)
    VERBOSE = args.verbose
    try:
        client = Client(load_profile(args.profile))
        {"config": cmd_config, "check": cmd_check, "sources": cmd_sources, "schemas": cmd_schemas,
         "tables": cmd_tables, "locate-table": cmd_locate_table, "apply": cmd_apply,
         "query": cmd_query}[args.cmd](client, args)
    except YearningError as e:
        print("错误：%s" % e, file=sys.stderr)
        return e.exit_code
    except (socket.timeout, TimeoutError):
        print("错误：请求超时（可调大 --timeout，或缩小查询范围）", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
