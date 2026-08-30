"""在线聊天室 - Socket.IO + 静态文件"""
import os
from datetime import datetime
from socketio import AsyncServer, ASGIApp

sio = AsyncServer(cors_allowed_origins="*", async_mode="asgi", socketio_path="socket.io")
app = ASGIApp(sio)

# 内存存储消息
messages: list[dict] = []
users: dict[str, str] = {}  # sid -> username

here = os.path.dirname(os.path.abspath(__file__))
html_content = open(os.path.join(here, "chat.html"), encoding="utf-8").read()

# 自定义中间件来处理根路径
class StaticMiddleware:
    def __init__(self, app):
        self.app = app
    
    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            if scope["path"] == "/" or scope["path"] == "/chat.html":
                await send({
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [[b"content-type", b"text/html; charset=utf-8"]]
                })
                await send({
                    "type": "http.response.body",
                    "body": html_content.encode("utf-8")
                })
                return
        await self.app(scope, receive, send)

app = StaticMiddleware(app)

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
    msg = {
        "type": "message",
        "user": username,
        "text": data.get("text", "").strip(),
        "time": datetime.now().strftime("%H:%M:%S")
    }
    if msg["text"]:
        messages.append(msg)
        await sio.emit("message", msg, broadcast=True, include_self=False)

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 80))
    print(f"🚀 聊天室运行在 http://0.0.0.0:{port}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
