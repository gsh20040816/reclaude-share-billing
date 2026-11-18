"""Local HTTP endpoint for an HTTPS reverse proxy. Uses the existing bot writer."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from . import allocation, evictor, rerec


def serve(bot, port):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            try:
                self.connection.settimeout(10)
                if self.path != "/":
                    return self.send_error(404)
                scheme, _, token = self.headers.get("Authorization", "").partition(" ")
                if scheme != "Bearer" or not token:
                    return self.respond(401, {"error": "请私聊 Bot 发送 /rerec，重新配对"})
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 16384:
                    return self.send_error(400)
                data = json.loads(self.rfile.read(length))
                result = (
                    bot._workers["write"].submit(self.run_command, rerec.token_id(token), data).result()
                )
                self.respond(200, result)
            except PermissionError:
                self.respond(401, {"error": "配对密钥已失效，请重新配对"})
            except (KeyError, TypeError, ValueError, allocation.NoCapacity, evictor.EvictionError) as e:
                self.respond(400, {"error": str(e)})
            except Exception as e:
                print(f"rerec request failed: {type(e).__name__}", flush=True)
                self.respond(502, {"error": "上游操作失败，请检查 Bot 日志和设备状态"})
            finally:
                bot.conn.close()

        def run_command(self, key, data):
            try:
                row = bot.conn.execute(
                    "SELECT user_id FROM rerec_tokens WHERE token_id=?", (key,)
                ).fetchone()
                if row is None:
                    raise PermissionError("配对密钥无效")
                return rerec.handle(bot, row["user_id"], data)
            finally:
                bot.conn.close()

        def respond(self, status, result):
            body = json.dumps(result).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = HTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
