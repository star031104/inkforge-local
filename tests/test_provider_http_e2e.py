import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from fastapi.testclient import TestClient

from app.llama_client import chat_once, chat_stream, list_models
from app.main import app
from app.db import default_project


class MockHandler(BaseHTTPRequestHandler):
    requests=[]
    def log_message(self, *args):
        return
    def _json(self, status, payload):
        data=json.dumps(payload,ensure_ascii=False).encode()
        self.send_response(status);self.send_header("Content-Type","application/json");self.send_header("Content-Length",str(len(data)));self.end_headers();self.wfile.write(data)
    def do_GET(self):
        parsed=urlparse(self.path)
        self.__class__.requests.append({"method":"GET","path":parsed.path,"query":parse_qs(parsed.query),"auth":self.headers.get("Authorization")})
        if parsed.path.endswith("/models"):
            return self._json(200,{"data":[{"id":"Qwen/Qwen3-8B"},{"id":"mock-other"}]})
        self._json(404,{"error":"not found"})
    def do_POST(self):
        n=int(self.headers.get("Content-Length","0")); raw=self.rfile.read(n); payload=json.loads(raw or b"{}")
        parsed=urlparse(self.path)
        self.__class__.requests.append({"method":"POST","path":parsed.path,"payload":payload,"auth":self.headers.get("Authorization")})
        if not parsed.path.endswith("/chat/completions"):
            return self._json(404,{"error":"not found"})
        if not payload.get("stream"):
            return self._json(200,{"choices":[{"message":{"content":"连接正常","reasoning_content":"隐藏推理"}}]})
        self.send_response(200);self.send_header("Content-Type","text/event-stream");self.end_headers()
        if payload.get("response_format"):
            pieces=['{"ok":', 'true,"text":"测试"}']
        else:
            prose_calls=sum(1 for item in self.__class__.requests if item.get("method")=="POST" and item.get("payload",{}).get("stream") and not item.get("payload",{}).get("response_format"))
            if prose_calls <= 1:
                pieces=["夜色压在白塔檐角。","风从石缝里穿过。"]
            else:
                long_text="".join(
                    f"林夏走到第{i}段石阶时扶住潮湿石壁，先听齿轮声，再检查绳索、门缝和积水留下的痕迹。"
                    for i in range(1,13)
                )
                pieces=[long_text[i:i+45] for i in range(0,len(long_text),45)]
        for piece in pieces:
            event={"choices":[{"delta":{"content":piece,"reasoning_content":"不可见"}}]}
            self.wfile.write(("data: "+json.dumps(event,ensure_ascii=False)+"\n\n").encode())
        self.wfile.write(b"data: [DONE]\n\n")


class Server:
    def __enter__(self):
        MockHandler.requests=[]
        self.httpd=ThreadingHTTPServer(("127.0.0.1",0),MockHandler)
        self.thread=threading.Thread(target=self.httpd.serve_forever,daemon=True);self.thread.start()
        self.base=f"http://127.0.0.1:{self.httpd.server_port}/v1"
        return self
    def __exit__(self,*args):
        self.httpd.shutdown();self.httpd.server_close();self.thread.join(timeout=2)


def settings(base):
    return {"provider":"siliconflow","base_url":base,"api_key":"test-secret","model":"Qwen/Qwen3-8B","temperature":0.7,"top_p":0.9,"top_k":40,"min_p":0.05,"enable_thinking":False,"max_tokens":3500}


def test_siliconflow_compatible_http_roundtrip_and_payload_contract():
    with Server() as server:
        cfg=settings(server.base)
        models=asyncio.run(list_models(cfg));assert "Qwen/Qwen3-8B" in models
        normal=asyncio.run(chat_once(cfg,[{"role":"user","content":"ping"}],max_tokens=20));assert normal=="连接正常"
        structured=asyncio.run(chat_once(cfg,[{"role":"user","content":"json"}],max_tokens=80,json_mode=True));assert json.loads(structured)["ok"] is True
        async def collect():
            out=[]
            async for part in chat_stream(cfg,[{"role":"user","content":"night"}]):out.append(part)
            return "".join(out)
        streamed=asyncio.run(collect());assert "隐藏推理" not in streamed and "不可见" not in streamed;assert "白塔" in streamed
        get_req=next(x for x in MockHandler.requests if x["method"]=="GET")
        assert get_req["query"].get("sub_type")==["chat"]
        assert get_req["auth"]=="Bearer test-secret"
        posts=[x for x in MockHandler.requests if x["method"]=="POST"]
        assert posts[0]["payload"]["enable_thinking"] is False
        assert posts[0]["payload"]["top_k"]==40 and posts[0]["payload"]["min_p"]==0.05
        assert "chat_template_kwargs" not in posts[0]["payload"]
        structured_payload=posts[1]["payload"]
        assert structured_payload["response_format"]=={"type":"json_object"}
        assert structured_payload["enable_thinking"] is False


def test_models_and_provider_summary_api_use_same_provider_contract():
    with Server() as server:
        cfg=settings(server.base);client=TestClient(app)
        r=client.post("/api/models",json=cfg);assert r.status_code==200;assert "Qwen/Qwen3-8B" in r.json()["models"]
        s=client.post("/api/provider/summary",json=cfg);assert s.status_code==200;data=s.json();assert data["provider"]=="siliconflow";assert data["has_api_key"] is True;assert data["non_thinking"] is True


def test_generate_api_real_http_roundtrip_repairs_short_first_pass():
    with Server() as server:
        project=default_project("http-gen","HTTP生成","now")
        project["settings"].update(settings(server.base))
        project["settings"]["target_words"]=500
        chapter=project["chapters"][0]
        response=TestClient(app).post("/api/generate",json={
            "project":project,"chapter_id":chapter["id"],"mode":"instruction",
            "instruction":"写林夏检查白塔钟室，只写现实可验证的细节。","selection":"","target_words":500,
        })
        assert response.status_code==200
        events=[]
        for block in response.text.split("\n\n"):
            line=next((x for x in block.splitlines() if x.startswith("data:")),"")
            if line: events.append(json.loads(line[5:].strip()))
        assert any(e.get("type")=="repair" for e in events)
        done=next(e for e in events if e.get("type")=="done")
        assert done["length_repaired"] is True
        assert done["actual_chars"] >= 360
        posts=[x for x in MockHandler.requests if x.get("method")=="POST"]
        prose_posts=[x for x in posts if x["payload"].get("stream") and not x["payload"].get("response_format")]
        assert len(prose_posts)==2
        assert all(x["payload"].get("enable_thinking") is False for x in prose_posts)
        assert prose_posts[0]["payload"]["max_tokens"] >= 1095

