"""yearning.py 单元测试：SQL 只读校验、msgpack 编解码，以及基于本地模拟服务端的完整查询流程。

运行：python3 -m unittest discover -s ~/.claude/skills/yearning-query/tests
"""

import base64
import hashlib
import json
import os
import socket
import struct
import sys
import tempfile
import threading
import unittest
import urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import yearning as y  # noqa: E402


class SqlGuardTest(unittest.TestCase):
    def test_auto_limit(self):
        sql, notes = y.prepare_sql("select id, name from t where a = 1;", 100)
        self.assertEqual(sql, "select id, name from t where a = 1 LIMIT 100;")
        self.assertEqual(len(notes), 1)

    def test_keep_existing_limit(self):
        sql, notes = y.prepare_sql("SELECT * FROM t LIMIT 5", 100)
        self.assertEqual(sql, "SELECT * FROM t LIMIT 5;")
        self.assertEqual(notes, [])

    def test_limit_in_string_not_counted(self):
        sql, _ = y.prepare_sql("select * from t where note = 'no limit here'", 10)
        self.assertTrue(sql.endswith("LIMIT 10;"))

    def test_trailing_comment_removed_before_limit(self):
        sql, _ = y.prepare_sql("select 1 from t -- comment", 10)
        self.assertEqual(sql, "select 1 from t LIMIT 10;")

    def test_multi_statements(self):
        sql, _ = y.prepare_sql("show tables; select a from t where b=';'", 10)
        self.assertEqual(sql, "show tables;\nselect a from t where b=';' LIMIT 10;")

    def test_reject_writes(self):
        for bad in ("update t set a=1", "delete from t", "insert into t values(1)", "drop table t",
                    "select * from t for update", "select * from t lock in share mode",
                    "select sleep(10)", "select * into outfile '/tmp/x' from t",
                    "with x as (select 1) delete from t", "set @a=1", "select 1; truncate t"):
            with self.assertRaises(y.YearningError, msg=bad):
                y.prepare_sql(bad, 100)

    def test_keywords_in_strings_and_identifiers_allowed(self):
        y.prepare_sql("select `update`, update_time from t where s = 'delete from x for update'", 100)
        y.prepare_sql("select replace(name, 'a', 'b') from t", 100)

    def test_limit_disabled(self):
        sql, _ = y.prepare_sql("select * from t", 0)
        self.assertEqual(sql, "select * from t;")


class MsgpackTest(unittest.TestCase):
    def test_roundtrip(self):
        obj = {"type": "0", "sql": "select 1" * 10, "n": 300, "neg": -5, "big": 2 ** 40, "f": 1.5,
               "none": None, "ok": True, "list": list(range(20)), "zh": "中文" * 100,
               "nested": {str(i): i for i in range(20)}}
        self.assertEqual(y.mp_unpack(y.mp_pack(obj)), obj)


# ---------------------------------------------------------------- 模拟 Yearning 服务端

TOKEN = "h." + base64.urlsafe_b64encode(json.dumps(
    {"name": "tester", "real_name": "测试", "exp": 4102444800}).encode()).decode().rstrip("=") + ".s"


def ws_send(conn, payload):
    n = len(payload)
    head = struct.pack("!BB", 0x82, n) if n < 126 else struct.pack("!BBH", 0x82, 126, n)
    conn.sendall(head + payload)


def ws_recv(conn, buf):
    def need(k):
        nonlocal buf
        while len(buf) < k:
            buf += conn.recv(65536)
        out, buf = buf[:k], buf[k:]
        return out
    b1, b2 = need(2)
    n = b2 & 0x7F
    if n == 126:
        n = struct.unpack("!H", need(2))[0]
    mask = need(4)
    data = bytes(b ^ mask[i % 4] for i, b in enumerate(need(n)))
    return b1 & 0x0F, data, buf


class MockYearning(threading.Thread):
    def __init__(self, session_active=False, audit=False):
        super().__init__(daemon=True)
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(8)
        self.port = self.srv.getsockname()[1]
        self.session_active = session_active
        self.audit = audit
        self.requests = []
        self.ws_messages = []

    def run(self):
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self.handle, args=(conn,), daemon=True).start()

    def handle(self, conn):
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += conn.recv(4096)
        head, _, rest = buf.partition(b"\r\n\r\n")
        lines = head.decode().split("\r\n")
        method, target, _ = lines[0].split(" ")
        headers = {k.lower(): v.strip() for k, v in (l.split(":", 1) for l in lines[1:])}
        path = urllib.parse.urlparse(target).path
        length = int(headers.get("content-length", 0))
        while len(rest) < length:
            rest += conn.recv(4096)
        self.requests.append((method, target, headers, rest))
        if headers.get("upgrade") == "websocket":
            return self.handle_ws(conn, headers, target)
        body = self.route(method, path, headers, rest)
        data = json.dumps(body, ensure_ascii=False).encode()
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n"
                     b"Connection: close\r\n\r\n" % len(data) + data)
        conn.close()

    def route(self, method, path, headers, body):
        ok = lambda p: {"code": 1200, "payload": p, "text": ""}  # noqa: E731
        if path == "/login":
            req = json.loads(body)
            if req["username"] == "tester" and req["password"] == "pw":
                return ok({"token": TOKEN, "user": "tester"})
            return {"code": 1301, "payload": None, "text": "账号/密码错误"}
        assert headers.get("authorization") == "Bearer " + TOKEN
        if path == "/api/v2/fetch/source":
            return ok([{"source": "prod-order", "source_id": "sid-1", "idc": "生产"},
                       {"source": "test-order", "source_id": "sid-2", "idc": "测试"}])
        if path == "/api/v2/fetch/query_status":
            return ok(not self.session_active)
        if path == "/api/v2/fetch/is_query":
            return ok({"status": self.audit, "export": False})
        if path == "/api/v2/query/post":
            if not self.audit:
                self.session_active = True
            return ok(None)
        if path == "/api/v2/query/schema":
            return ok({"info": [{"title": "order_db", "key": "order_db"}]})
        if path == "/api/v2/query/tables":
            return ok({"table": [{"title": "t_order"}, {"title": "t_user"}]})
        return {"code": 1310, "text": "非法传参"}

    def handle_ws(self, conn, headers, target):
        assert headers["sec-websocket-protocol"] == TOKEN
        assert urllib.parse.parse_qs(urllib.parse.urlparse(target).query)["user"] == ["tester"]
        accept = base64.b64encode(hashlib.sha1(
            (headers["sec-websocket-key"] + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        conn.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      "Sec-WebSocket-Accept: %s\r\n\r\n" % accept).encode())
        buf = b""
        while True:
            try:
                op, data, buf = ws_recv(conn, buf)
            except Exception:
                break
            if op == 0x8:
                break
            msg = y.mp_unpack(data)
            self.ws_messages.append(msg)
            if msg.get("type") == "1":
                continue
            if "error" in msg["sql"]:
                ws_send(conn, y.mp_pack({"error": "Error 1054: Unknown column", "results": None, "status": False}))
                continue
            rows = [{"id": i, "name": "名字%d" % i, "note": None} for i in range(3)]
            ws_send(conn, y.mp_pack({"export": False, "error": "", "query_time": 12, "status": False,
                                     "heartbeat": 0, "is_only": False,
                                     "results": [{"field": [{"title": "id"}, {"title": "name"}, {"title": "note"}],
                                                  "data": rows}]}))
        conn.close()


class EndToEndTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.server = MockYearning()
        self.server.start()
        self.cfg = os.path.join(self.tmp, "config.json")
        with open(self.cfg, "w") as f:
            json.dump({"default": "t", "profiles": {"t": {"url": "http://127.0.0.1:%d" % self.server.port,
                                                          "username": "tester", "password": "pw"}}}, f)
        os.environ["YEARNING_CONFIG"] = self.cfg
        y.CACHE_DIR = os.path.join(self.tmp, "cache")

    def tearDown(self):
        self.server.srv.close()
        os.environ.pop("YEARNING_CONFIG", None)

    def run_cli(self, *argv):
        import contextlib
        import io
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = y.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_query_flow_auto_session(self):
        code, out, err = self.run_cli("query", "-s", "prod-order", "-d", "order_db", "--sql", "select * from t_order")
        self.assertEqual(code, 0, err)
        self.assertIn("名字2", out)
        self.assertIn("NULL", out)
        self.assertIn("LIMIT 100", err)
        sent = [m for m in self.server.ws_messages if m.get("type") == "0"][0]
        self.assertEqual(sent["sql"], "select * from t_order LIMIT 100;")
        self.assertEqual(sent["source_id"], "sid-1")
        self.assertEqual(sent["schema"], "order_db")
        self.assertTrue(any(r[1] == "/api/v2/query/post" for r in self.server.requests))

    def test_json_and_out(self):
        out_file = os.path.join(self.tmp, "r.csv")
        code, out, _ = self.run_cli("query", "-s", "sid-1", "-d", "order_db", "--sql", "select 1 limit 1",
                                    "--format", "csv", "--out", out_file)
        self.assertEqual(code, 0)
        with open(out_file, encoding="utf-8") as f:
            self.assertEqual(f.readline().strip(), "id,name,note")
        code, out, _ = self.run_cli("query", "-s", "prod", "-d", "order_db", "--sql", "select 1", "--format", "json")
        self.assertEqual(json.loads(out)[0]["row_count"], 3)
        code, out, _ = self.run_cli("query", "-s", "prod", "-d", "order_db", "--sql", "select 1",
                                    "--format", "vertical", "--max-rows", "2")
        self.assertIn("2. row", out)
        self.assertIn("name: 名字1", out)
        self.assertIn("共 3 行", out)

    def test_query_error(self):
        code, _, err = self.run_cli("query", "-s", "prod-order", "-d", "order_db", "--sql", "select error_col from t")
        self.assertEqual(code, 1)
        self.assertIn("Unknown column", err)

    def test_audit_required(self):
        self.server.audit = True
        code, _, err = self.run_cli("query", "-s", "prod-order", "-d", "order_db", "--sql", "select 1")
        self.assertEqual(code, 3)
        self.assertFalse(any(r[1] == "/api/v2/query/post" for r in self.server.requests))
        code, out, _ = self.run_cli("apply", "-s", "prod-order", "--reason", "排查")
        self.assertIn("[预演]", out)
        self.assertFalse(any(r[1] == "/api/v2/query/post" for r in self.server.requests))

    def test_ambiguous_source_and_listing(self):
        code, _, err = self.run_cli("query", "-s", "order", "-d", "x", "--sql", "select 1")
        self.assertEqual(code, 1)
        self.assertIn("匹配到多个", err)
        code, out, _ = self.run_cli("tables", "-s", "prod-order", "-d", "order_db", "-f", "user")
        self.assertEqual(out.strip(), "t_user")

    def test_locate_table(self):
        code, out, err = self.run_cli("locate-table", "T_USER")
        self.assertEqual(code, 0, err)
        self.assertIn("实时读取", out)
        self.assertIn("[prod] 数据源=prod-order  库=order_db  表=t_user", out)
        self.assertIn("[staging] 数据源=test-order", out)
        schema_calls = sum(1 for r in self.server.requests if r[1].startswith("/api/v2/query/schema"))
        code, out, _ = self.run_cli("locate-table", "user", "--like", "--env", "prod")
        self.assertIn("缓存", out)
        self.assertNotIn("staging", out)
        self.assertEqual(schema_calls, sum(1 for r in self.server.requests if r[1].startswith("/api/v2/query/schema")))
        code, out, _ = self.run_cli("locate-table", "nope")
        self.assertIn("未找到", out)

    def test_locate_table_prefer_sources(self):
        with open(self.cfg) as f:
            cfg = json.load(f)
        cfg["profiles"]["t"]["prefer_sources"] = {"staging": ["test-order"]}
        with open(self.cfg, "w") as f:
            json.dump(cfg, f)
        code, out, _ = self.run_cli("locate-table", "t_user")
        self.assertIn("数据源=test-order  库=order_db  表=t_user  source_id=sid-2  ★默认", out)
        self.assertNotIn("sid-1  ★默认", out)
        cfg["profiles"]["t"]["prefer_sources"] = {"prod": ["prod-order/other_db"], "staging": ["test-order/order_db"]}
        with open(self.cfg, "w") as f:
            json.dump(cfg, f)
        code, out, _ = self.run_cli("locate-table", "t_user")
        self.assertNotIn("sid-1  ★默认", out)
        self.assertIn("sid-2  ★默认", out)

    def test_bad_password(self):
        with open(self.cfg, "w") as f:
            json.dump({"profiles": {"default": {"url": "http://127.0.0.1:%d" % self.server.port,
                                                "username": "tester", "password": "bad"}}}, f)
        code, _, err = self.run_cli("check")
        self.assertEqual(code, 2)
        self.assertIn("1301", err)


if __name__ == "__main__":
    unittest.main()
