"""在线聊天室 - Socket.IO + 图片分享 + 自动清理"""
import os
import io
import uuid
import glob
import mimetypes
from datetime import datetime
from email.parser import BytesParser
from email import policy

from socketio import AsyncServer, ASGIApp

# ============ 配置 ============
HERE = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(HERE, "uploads")
MAX_IMAGE_SIZE = 10 * 1024 * 1024       # 单张图片最大 10MB
MAX_TOTAL_SIZE = 1536 * 1024 * 1024     # 总存储上限 1.5GB
ALLOWED_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}

os.makedirs(UPLOAD_DIR, exist_ok=True)

# ============ Socket.IO ============
sio = AsyncServer(cors_allowed_origins="*", async_mode="asgi", socketio_path="socket.io")
sio_app = ASGIApp(sio, socketio_path="socket.io")

# 内存存储消息
messages: list[dict] = []
users: dict[str, str] = {}  # sid -> username

# ============ 图片清理 ============
def _all_images():
    return glob.glob(os.path.join(UPLOAD_DIR, "*"))

def _dir_size():
    return sum(os.path.getsize(f) for f in _all_images() if os.path.isfile(f))

def enforce_quota():
    """总大小超过 1.5G 时，删除最早的一批图片，直到低于上限"""
    while True:
        files = [f for f in _all_images() if os.path.isfile(f)]
        if _dir_size() <= MAX_TOTAL_SIZE or not files:
            break
        oldest = min(files, key=os.path.getmtime)
        try:
            sz = os.path.getsize(oldest)
            os.remove(oldest)
            print(f"[cleanup] 删除最旧图片 {os.path.basename(oldest)} ({sz//1024}KB)")
        except OSError:
            break

# ============ ASGI 根应用（分发 HTTP / Socket.IO） ============
html_content = open(os.path.join(HERE, "chat.html"), encoding="utf-8").read()

async def send_http(send, status, content_type, body: bytes, extra_headers=None):
    headers = [[b"content-type", content_type.encode()], [b"cache-control", b"no-cache"]]
    if extra_headers:
        headers += extra_headers
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": headers,
    })
    await send({"type": "http.response.body", "body": body})

async def read_body(receive):
    body = b""
    while True:
        msg = await receive()
        body += msg.get("body", b"")
        if not msg.get("more_body"):
            return body

def parse_upload(body: bytes, content_type: str):
    """用标准库 email 解析 multipart/form-data，返回 (filename, data)"""
    if "boundary=" not in content_type:
        return None, None
    boundary = content_type.split("boundary=", 1)[1].split(";", 1)[0].strip().strip('"')
    prefix = f'Content-Type: multipart/form-data; boundary={boundary}\r\n\r\n'.encode()
    msg = BytesParser(policy=policy.HTTP).parsebytes(prefix + body)
    for part in msg.iter_parts():
        if part.get_param("name", header="content-disposition") == "file":
            return part.get_filename(), part.get_payload(decode=True)
    return None, None

async def handle_upload(scope, receive, send):
    content_type = ""
    for k, v in scope["headers"]:
        if k == b"content-type":
            content_type = v.decode()
    body = await read_body(receive)

    filename, data = parse_upload(body, content_type)
    if not filename or not data:
        await send_http(send, 400, "application/json", b'{"error":"no file"}')
        return

    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_EXTS:
        await send_http(send, 400, "application/json", b'{"error":"bad type"}')
        return
    if len(data) > MAX_IMAGE_SIZE:
        await send_http(send, 413, "application/json", b'{"error":"too large"}')
        return

    # 清理现有文件，保证总大小不超限后再存新的
    enforce_quota()

    new_name = uuid.uuid4().hex + ext
    path = os.path.join(UPLOAD_DIR, new_name)
    with open(path, "wb") as f:
        f.write(data)

    # 存完后如果超限，删除最旧（通常删了才存，不会超）
    enforce_quota()

    import json
    resp = json.dumps({"url": "/uploads/" + new_name}).encode()
    await send_http(send, 200, "application/json", resp)

async def handle_static_file(scope, send, path):
    full = os.path.normpath(os.path.join(UPLOAD_DIR, path))
    if not full.startswith(UPLOAD_DIR) or not os.path.isfile(full):
        await send_http(send, 404, "text/plain", b"Not Found")
        return
    mime = mimetypes.guess_type(full)[0] or "application/octet-stream"
    with open(full, "rb") as f:
        content = f.read()
    await send_http(send, 200, mime, content, extra_headers=[[b"cache-control", b"public, max-age=3600"]])

class RootApp:
    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await sio_app(scope, receive, send)

        path = scope["path"]

        # Socket.IO 走 socketio app
        if path.startswith("/socket.io"):
            return await sio_app(scope, receive, send)

        method = scope["method"]

        # 图片上传
        if path == "/upload" and method == "POST":
            return await handle_upload(scope, receive, send)

        # 图片静态访问
        if path.startswith("/uploads/"):
            return await handle_static_file(scope, send, path[len("/uploads/"):])

        # 首页
        if path == "/" or path == "/chat.html":
            return await send_http(send, 200, "text/html; charset=utf-8", html_content.encode("utf-8"))

        await send_http(send, 404, "text/plain", b"Not Found")

app = RootApp()

# ============ Socket.IO 事件 ============
@sio.event
async def connect(sid, environ, auth):
    username = users.get(sid, "匿名用户")
    messages.append({
        "type": "system",
        "text": f"🎉 {username} 加入了聊天室",
        "time": datetime.now().strftime("%H:%M:%S")
    })
    await sio.emit("system", {"text": messages[-1]["text"]}, room=sid)
    await sio.emit("history", {"messages": messages[-50:]}, room=sid)
    await sio.emit("online_users", {"users": list(set(users.values()))}, broadcast=True)
    print(f"[+] {username} 连接 ({sid})")

@sio.event
async def disconnect(sid):
    username = users.pop(sid, "匿名用户")
    messages.append({
        "type": "system",
        "text": f"👋 {username} 离开了聊天室",
        "time": datetime.now().strftime("%H:%M:%S")
    })
    await sio.emit("system", {"text": messages[-1]["text"]}, broadcast=True)
    await sio.emit("online_users", {"users": list(set(users.values()))}, broadcast=True)
    print(f"[-] {username} 断开")

@sio.event
async def set_name(sid, data):
    old_name = users.get(sid, "未知")
    new_name = data.get("name", "匿名用户").strip() or "匿名用户"
    users[sid] = new_name
    messages.append({
        "type": "system",
        "text": f"📛 {old_name} 更名为 {new_name}",
        "time": datetime.now().strftime("%H:%M:%S")
    })
    await sio.emit("system", {"text": messages[-1]["text"]}, broadcast=True)
    await sio.emit("online_users", {"users": list(set(users.values()))}, broadcast=True)

@sio.event
async def chat_message(sid, data):
    username = users.get(sid, "匿名用户")
    text = data.get("text", "").strip()
    image = data.get("image", "").strip()
    if not text and not image:
        return
    msg = {
        "type": "message",
        "user": username,
        "text": text,
        "image": image,
        "time": datetime.now().strftime("%H:%M:%S")
    }
    messages.append(msg)
    await sio.emit("message", msg, broadcast=True, include_self=True)

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 7860))
    print(f"🚀 聊天室运行在 http://0.0.0.0:{port}")
    print(f"📁 图片上限: 单张 10MB | 总存储 1.5GB 自动清理最旧")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")