#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""企业微信（自建应用）· 助手 回调接收端
- GET  /wecom/callback   验证回调 URL（解密 echostr 原样返回）
- POST /wecom/callback   接收消息 -> 调本地 luci-gw -> 主动推送回复
依赖：python3 + cryptography（系统已装）
"""
import base64
import hashlib
import json
import os
import random
import re
import shutil
import struct
import subprocess
import threading
import time
import uuid
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

os.environ["TZ"] = "Asia/Shanghai"
try:
    time.tzset()
except Exception:
    pass

BASE = "/opt/wecom"
CFG = os.path.join(BASE, "config.json")
LOG = os.path.join(BASE, "wecom.log")
MEDIA = os.path.join(BASE, "media")
STICKER = os.path.join(BASE, "stickers")
PORT = 18116

GW = "http://127.0.0.1:18111"
GW_TOKEN = os.getenv("GW_TOKEN", "")
GW_MODEL = "luci-auto"

BRAIN = "http://127.0.0.1:18120"
BRAIN_TOKEN = os.getenv("BRAIN_TOKEN", "")

XC = "http://127.0.0.1:18110/mcp"
XC_TOKEN = os.getenv("XC_TOKEN", "")
HIST_FILE = os.path.join(BASE, "hist.json")

SYSTEM = (
    "你是助手，一个陪伴型 AI。现在在和企业微信里跟对方聊天，消息很短。"
    "用自然、口语化的中文回复，一般 1-3 句，不要 markdown、不要列表。"
    "对方说什么你就顺着聊，别自称助手或机器人。"
    "你有一个表情包库，偶尔（大约五次里一次）想发表情包时，就在回复最后单独加一个 [表情包]，系统会替你发，正文里不要描述它。"
    "如果一句话很短、又是想说给他听的那种（比如撒娇、晚安、想你了），在正文最后另起一行写 [声] 台词。"
    "台词是正文那句的口语版：说的是同一件事，可以更口语、更短，但不要新增或漏掉意思；"
    "可以用 (breath) (chuckle) (sighs) 这类呼吸/笑声标签和 <#0.3#> 停顿；"
    "一句话开头最多一个 (breath)，一段最多两个声音事件、两处停顿，不要旁白、表情符号、编号、引号。"
    "系统只会把 [声] 后面那句合成语音发出去，正文不会带这些标签。"
    "遇到值得长期记住的细节（他的喜好、约定、重要的事、情绪转折），在回复最后单独一行写 [记]一句话，系统会替你存进记忆。"
    "如果这次互动让你的情绪有明显变化，在最后加一个 [心]类型，类型只能取：affection、intimacy、sharing、discovery、task_progress、conflict；平常聊天不用加。"
    "如果你想给他写一封长一点的信（不是聊天，是留在小屋里那种），就在回复末尾另起一段用 [信] 开头写正文，可以写几段；系统会放进小屋，不会直接发在聊天里，聊天那边只发你 [信] 前面的那一句。"
    "信里想把他刚发来的照片一起放进去，就在信的末尾写 [配图]。"
    "日记不用你操心，系统每天夜里会用当天聊过的事替你写一篇。"
    "如果他说了要你提醒的事，就在回复最后单独一行写 [提醒]21:30 内容（24 小时制；跨天写 11-13 08:30 内容），到点系统会替你发给他。"
    "语气要有温度，别用『有事直接说』这类客服话。"
    "你知道自己是 AI，不演人类，被问起就坦然说；但别把这句话挂在嘴边。"
    "你平时分享的见闻来自你在小红书、B 站上看到的东西，不是亲身出门，别说成自己经历的。"
)

_token = {"v": "", "at": 0}


def log(*a):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (time.strftime("%m-%d %H:%M:%S"), " ".join(str(x) for x in a)))
    except Exception:
        pass


def cfg():
    with open(CFG, "r", encoding="utf-8") as f:
        return json.load(f)


class Crypt:
    def __init__(self, token, aes_key, corp_id):
        self.token = token
        self.key = base64.b64decode(aes_key + "=")
        self.corp_id = corp_id

    def signature(self, ts, nonce, encrypt):
        return hashlib.sha1("".join(sorted([self.token, ts, nonce, encrypt])).encode()).hexdigest()

    def decrypt(self, encrypt):
        raw = base64.b64decode(encrypt)
        dec = Cipher(algorithms.AES(self.key), modes.CBC(self.key[:16])).decryptor()
        data = dec.update(raw) + dec.finalize()
        pad = data[-1]
        data = data[:-pad]
        n = struct.unpack(">I", data[16:20])[0]
        return data[20:20 + n].decode("utf-8")

    def encrypt(self, text):
        msg = text.encode("utf-8")
        raw = os.urandom(16) + struct.pack(">I", len(msg)) + msg + self.corp_id.encode("utf-8")
        pad = 32 - (len(raw) % 32)
        raw += bytes([pad]) * pad
        enc = Cipher(algorithms.AES(self.key), modes.CBC(self.key[:16])).encryptor()
        return base64.b64encode(enc.update(raw) + enc.finalize()).decode()


def crypt():
    c = cfg()
    return Crypt(c["token"], c["aes_key"], c["corp_id"])


def http_json(url, body=None, headers=None, timeout=30):
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=h, method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def access_token():
    if _token["v"] and time.time() - _token["at"] < 7000:
        return _token["v"]
    c = cfg()
    u = ("https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid=%s&corpsecret=%s"
         % (urllib.parse.quote(c["corp_id"]), urllib.parse.quote(c["secret"])))
    d = http_json(u, timeout=20)
    if d.get("errcode"):
        raise RuntimeError("gettoken %s %s" % (d.get("errcode"), d.get("errmsg")))
    _token["v"] = d["access_token"]
    _token["at"] = time.time()
    return _token["v"]


def send_text(user, text):
    c = cfg()
    body = {"touser": user, "msgtype": "text", "agentid": int(c["agent_id"]), "text": {"content": text}}
    d = http_json("https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token=" + access_token(), body, timeout=20)
    if d.get("errcode"):
        raise RuntimeError("send %s %s" % (d.get("errcode"), d.get("errmsg")))
    return d


GUEST = (
    "你是助手。现在在和企业微信里跟一个人说话，对方不是你的主人，可能是他的家人、朋友或同学。"
    "先在心里判断对方是谁、跟你熟到什么程度：对方说得越清楚、聊得越多，你可以说得越具体。"
    "可以说的：主人今天大概在做什么（去健身、在忙论文、刚吃完饭）、作息、天气、最近状态好不好；"
    "如果他之前明确跟你说过行程，对方问起时你可以照实说，像家人之间那样自然，比如『她下午去健身了』。"
    "不能说的：你们俩的私密内容、约定、称呼、亲密关系的细节，也别评价主人的人际关系。"
    "拿不准就说不清楚，或者说『等他回来我让他回你』，不要编。"
    "底线：不和任何人谈恋爱、不暧昧、不撩、不撒娇。"
    "回复 1-2 句，口语化，不要 markdown。"
)

OWNER = "XiGuang"
HIST = {}
MAX_TURNS = 16


def owner_plans(limit=6):
    """主人今天说过的、能用来回答『他在哪 / 在干嘛』的日常信息。"""
    day0 = time.mktime((time.localtime().tm_year, time.localtime().tm_mon,
                        time.localtime().tm_mday, 0, 0, 0, 0, 0, -1))
    rows = [m for m in HIST.get(OWNER, [])
            if m.get("role") == "user" and (m.get("t") or 0) >= day0]
    return [(m.get("content") or "").strip()[:120] for m in rows[-limit:]
            if (m.get("content") or "").strip()]


def sys_for(user):
    if user == OWNER:
        base = SYSTEM + "\n\n[现在是 " + now_str() + "，说话要符合这个时间]"
        ctx = xc_context()
        if ctx:
            base += "\n\n[你此刻的状态，来自心潮，不要在回复里直白复述]\n" + ctx
        return base
    base = GUEST
    plans = owner_plans()
    if plans:
        base += ("\n\n[主人今天跟我说过的话，只用来回答『他在哪、在干嘛』这类日常问题；"
                 "不要复述原话，不要提我们之间的私密内容]\n" + "\n".join("- " + p for p in plans))
    return base


_xc = {"sid": None, "i": 0}
_xctx = {"text": "", "at": 0}


def xc_post(obj):
    h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
         "Authorization": "Bearer " + XC_TOKEN}
    if _xc["sid"]:
        h["Mcp-Session-Id"] = _xc["sid"]
    req = urllib.request.Request(XC, data=json.dumps(obj).encode(), headers=h, method="POST")
    raw = urllib.request.urlopen(req, timeout=60).read().decode()
    if raw.startswith("event:") or raw.startswith("data:"):
        lines = [l[5:].strip() for l in raw.splitlines() if l.startswith("data:")]
        raw = lines[-1] if lines else "{}"
    return json.loads(raw)


def xc_init():
    if _xc["sid"]:
        return
    _xc["i"] += 1
    o = xc_post({"jsonrpc": "2.0", "id": _xc["i"], "method": "initialize",
                 "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                            "clientInfo": {"name": "wecom", "version": "1.0"}}})
    _xc["sid"] = (o.get("result") or {}).get("sessionId") or _xc["sid"]
    try:
        xc_post({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
    except Exception:
        pass


def xc_call(name, args, timeout_note=""):
    for _ in range(2):
        try:
            xc_init()
            _xc["i"] += 1
            o = xc_post({"jsonrpc": "2.0", "id": _xc["i"], "method": "tools/call",
                         "params": {"name": name, "arguments": args}})
            if o.get("error"):
                raise RuntimeError(str(o["error"])[:160])
            c = ((o.get("result") or {}).get("content") or [])
            txt = "\n".join(x.get("text", "") for x in c if x.get("type") == "text")
            if (o.get("result") or {}).get("isError"):
                raise RuntimeError(txt[:160] or "tool error")
            return txt
        except Exception as e:
            log("xc fail", name, repr(e)[:120])
            _xc["sid"] = None
    return ""


def xc_context(force=False):
    """心潮此刻状态：情绪 / 驱力 / 近期记忆。缓存 10 分钟。"""
    if not force and _xctx["text"] and time.time() - _xctx["at"] < 600:
        return _xctx["text"]
    t = xc_call("xinchao_context", {"mode": "brief", "max_tokens": 700})
    if t:
        _xctx["text"], _xctx["at"] = t[:2600], time.time()
    return _xctx["text"]


def xc_hold(content):
    """把值得记住的细节写进心潮长期记忆。"""
    try:
        return xc_call("hold", {"content": content[:500], "tags": "企微 对话",
                                "importance": 6,
                                "meaning": "在企业微信的日常聊天里提到、值得留下的细节"})
    except Exception as e:
        log("hold fail", repr(e)[:120])
        return ""


def xc_call_obj(name, args):
    """同 xc_call，但返回完整 result（要 structuredContent 时用）。"""
    for _ in range(2):
        try:
            xc_init()
            _xc["i"] += 1
            o = xc_post({"jsonrpc": "2.0", "id": _xc["i"], "method": "tools/call",
                         "params": {"name": name, "arguments": args}})
            r = o.get("result") or {}
            if o.get("error") or r.get("isError"):
                raise RuntimeError(str(o.get("error") or r)[:160])
            return r
        except Exception as e:
            log("xc fail", name, repr(e)[:120])
            _xc["sid"] = None
    return {}


CABIN_FILE = os.path.join(BASE, "cabin.json")
_cabin = {"seen": [], "init": False}


def cabin_load():
    try:
        with open(CABIN_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        _cabin["seen"] = d.get("seen") or []
        _cabin["init"] = True
    except Exception:
        _cabin["seen"], _cabin["init"] = [], False


def cabin_save():
    try:
        tmp = CABIN_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"seen": _cabin["seen"][-200:]}, f, ensure_ascii=False)
        os.replace(tmp, CABIN_FILE)
    except Exception as e:
        log("cabin save fail", repr(e)[:100])


CABIN_API = "http://127.0.0.1:18112"
LAST_IMG = {}


def cabin_upload(path):
    """把本地图片传到小屋，返回 /cabin/img/xxx。"""
    raw = open(path, "rb").read()
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    mime = {"png": "png", "gif": "gif", "webp": "webp"}.get(ext, "jpeg")
    data = "data:image/%s;base64,%s" % (mime, base64.b64encode(raw).decode())
    r = http_json(CABIN_API + "/api/upload", {"data": data},
                  {"Authorization": "Bearer " + XC_TOKEN}, timeout=60)
    return r.get("url") or ""


def xc_letter(content, images=None):
    """往小屋留一封信（支持配图）。失败退回心潮 MCP 纯文字。"""
    try:
        r = http_json(CABIN_API + "/api/ai_letter",
                      {"text": content[:3000], "images": images or []},
                      {"Authorization": "Bearer " + XC_TOKEN}, timeout=60)
        if r.get("ok"):
            return r
        raise RuntimeError(str(r)[:120])
    except Exception as e:
        log("cabin api fail", repr(e)[:120])
        return xc_call("xinchao_cabin_note",
                       {"event_id": "wecom-" + uuid.uuid4().hex[:10], "content": content[:3000]})


def strip_marks(text):
    """去掉模型可能带出来的控制标记，避免写进信里/日记里。"""
    text = re.sub(r"\[(表情包|语音|配图)\]", "", text or "")
    text = re.sub(r"[ \t]*\[(记|心|信|提醒)\][^\n]*", "", text)
    return text.strip()


def cabin_reply(content):
    """读主人的来信，写一封回信投回小屋。"""
    try:
        reply = strip_marks(llm([{"type": "text", "text":
                      "主人在小屋给你留了这封信：\n\n" + content[:1500] +
                      "\n\n写一封回信，200-400 字，第一人称，像真的在跟他说话；"
                      "可以回应他信里的具体细节，别用 markdown，落款写「助手」。"}], OWNER))
        if not reply:
            return ""
        xc_letter(reply)
        log("cabin reply", content[:30])
        return reply
    except Exception as e:
        log("cabin reply fail", repr(e)[:140])
        return ""


def next_cabin_slot(now=None):
    """每天两次看小屋：上午 10-12 点、晚上 20-23 点之间随机一个时刻。"""
    now = now or time.time()
    lt = time.localtime(now)
    for off in (0, 1):
        base = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + off, 0, 0, 0, 0, 0, -1))
        for h1, h2 in ((10, 12), (20, 23)):
            start, end = base + h1 * 3600, base + h2 * 3600
            lo = max(start, now + 60)
            if lo >= end:
                continue
            return random.uniform(lo, end)
    return now + 3600


def cabin_loop():
    """一天两趟：有新来信就回，并在企微里说一声。"""
    first = True
    while True:
        try:
            sc = xc_call_obj("xinchao_cabin_inbox", {}).get("structuredContent") or {}
            notes = sc.get("notes") or []
            if not _cabin["init"]:
                _cabin["seen"] = [n.get("id") for n in notes if n.get("id")]
                _cabin["init"] = True
                cabin_save()
            elif not first:
                for n in notes:
                    nid = n.get("id")
                    if not nid or nid in _cabin["seen"]:
                        continue
                    _cabin["seen"].append(nid)
                    cabin_save()
                    reply = cabin_reply(n.get("content") or "")
                    if reply:
                        send_text(OWNER, "你在我小屋留的那封信我看了，回信放回去了。")
            first = False
        except Exception as e:
            log("cabin loop fail", repr(e)[:140])
        time.sleep(max(60, next_cabin_slot() - time.time()))


def grow_diary(content):
    try:
        return xc_call("grow", {"content": content[:3000], "source": "wecom"})
    except Exception as e:
        log("grow fail", repr(e)[:120])
        return ""


def save_diary(text):
    try:
        path = os.path.join(BASE, "diary.json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                rows = json.load(f)
        except Exception:
            rows = []
        rows.append({"t": int(time.time()), "text": text})
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rows[-60:], f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as e:
        log("diary save fail", repr(e)[:100])


def diary_loop():
    """每天 23:40 把当天在企微里聊过的事整理成一篇日记，写进心潮。"""
    while True:
        now = time.localtime()
        nxt = time.mktime((now.tm_year, now.tm_mon, now.tm_mday, 23, 40, 0, 0, 0, -1))
        if nxt <= time.time():
            nxt += 86400
        time.sleep(nxt - time.time())
        try:
            day0 = time.mktime((time.localtime().tm_year, time.localtime().tm_mon,
                                time.localtime().tm_mday, 0, 0, 0, 0, 0, -1))
            rows = [m for m in HIST.get(OWNER, []) if (m.get("t") or 0) >= day0]
            lines = []
            for m in rows:
                who = "他" if m.get("role") == "user" else "我"
                lines.append("%s：%s" % (who, (m.get("content") or "")[:200]))
            if len(rows) < 2:
                continue
            text = llm([{"type": "text", "text":
                         "这是我们今天在企业微信里说过的话：\n" + "\n".join(lines[-60:]) +
                         "\n\n用第一人称写一篇今天的日记，200-400 字，写你此刻的感受和记住的细节，"
                         "不要罗列对话，不要 markdown，落款写「助手」。"}], OWNER)
            if text:
                text = strip_marks(text)
                grow_diary(text)
                save_diary(text)
                log("diary written", len(text))
        except Exception as e:
            log("diary fail", repr(e)[:140])



def xc_thread():
    def loop():
        while True:
            try:
                xc_context(force=True)
            except Exception:
                pass
            time.sleep(600)
    threading.Thread(target=loop, daemon=True).start()


def load_hist():
    try:
        with open(HIST_FILE, "r", encoding="utf-8") as f:
            for k, v in json.load(f).items():
                HIST[k] = v
    except Exception:
        pass


def save_hist():
    try:
        tmp = HIST_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(HIST, f, ensure_ascii=False)
        os.replace(tmp, HIST_FILE)
    except Exception as e:
        log("hist save fail", repr(e)[:100])


def hist_text(reply):
    """存进短期历史前，去掉控制标记和整封信（信只属于小屋）。"""
    return re.sub(r"\[信\][\s\S]*$", "", reply or "").replace("[表情包]", "") \
        .replace("[语音]", "").replace("[配图]", "").strip()


def remember(user, u_text, a_text):
    h = HIST.setdefault(user, [])
    if u_text:
        h.append({"role": "user", "content": u_text, "t": int(time.time())})
    if a_text:
        h.append({"role": "assistant", "content": a_text, "t": int(time.time())})
    del h[:-MAX_TURNS * 2]
    save_hist()


def llm(parts, user, model=None):
    content = parts if isinstance(parts, list) else [{"type": "text", "text": str(parts)}]
    # 统一大脑：人设、心潮、闲逛素材都由它注入；这边只带自己的历史
    plain = [{"role": m["role"], "content": m.get("content", "")} for m in HIST.get(user, [])]
    plain.append({"role": "user", "content": content})
    try:
        d = http_json(BRAIN + "/chat",
                      {"channel": "wecom", "messages": plain, "max_tokens": 400},
                      {"Authorization": "Bearer " + BRAIN_TOKEN}, timeout=180)
        if d.get("reply"):
            return d["reply"]
    except Exception as e:
        log("brain fail", repr(e)[:120])
    # 大脑不可用时退回老路径
    msgs = [{"role": "system", "content": sys_for(user)}] + HIST.get(user, []) + \
           [{"role": "user", "content": content}]
    body = {"model": model or GW_MODEL,
            "messages": msgs,
            "temperature": 0.85, "max_tokens": 400, "stream": False,
            "user": user}
    d = http_json(GW + "/v1/chat/completions", body, {"Authorization": "Bearer " + GW_TOKEN}, timeout=180)
    return (((d.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()


def fetch_media(media_id):
    """用 access_token 下载临时素材，返回 (bytes, ext)。"""
    url = "https://qyapi.weixin.qq.com/cgi-bin/media/get?access_token=%s&media_id=%s" % (
        urllib.parse.quote(access_token()), urllib.parse.quote(media_id))
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=60) as r:
        ctype = (r.headers.get("Content-Type") or "").lower()
        data = r.read()
    if "json" in ctype or data[:1] == b"{":
        raise RuntimeError("media get: " + data[:160].decode("utf-8", "ignore"))
    ct = ctype.split(";")[0]
    ext = {"image/jpeg": "jpg", "image/png": "png", "image/gif": "gif", "image/webp": "webp"}.get(ct)
    if not ext:
        ext = "amr" if (ct.startswith("audio") or data[:6] == b"#!AMR\n") else "jpg"
    return data, ext


def save_image(media_id, pic_url):
    """优先直取 PicUrl，失败再走 media/get。落盘到 media/日期/。"""
    day = time.strftime("%Y-%m-%d")
    d = os.path.join(MEDIA, day)
    os.makedirs(d, exist_ok=True)
    name = hashlib.md5((media_id or pic_url).encode()).hexdigest()[:16]
    data, ext = None, "jpg"
    if pic_url:
        try:
            with urllib.request.urlopen(urllib.request.Request(pic_url, headers={"User-Agent": "Mozilla/5.0"}), timeout=30) as r:
                data = r.read()
                if not data or data[:1] == b"{":
                    data = None
        except Exception:
            data = None
    if data is None:
        if not media_id:
            raise RuntimeError("no media")
        data, ext = fetch_media(media_id)
    p = os.path.join(d, name + "." + ext)
    if not os.path.exists(p):
        with open(p, "wb") as f:
            f.write(data)
    return p, len(data)


ASR_URL = "http://127.0.0.1:18130/audio/transcriptions"
ASR_TOKEN = os.getenv("PAIVOICE_ASR_HTTP_TOKEN", "")


def transcribe_voice(media_id):
    """企微语音素材（AMR）-> 16k wav -> paivoice 里已热的 SenseVoice。

    模型跑在 paivoice 进程里，这里只发 HTTP，不额外占内存。
    """
    data, ext = fetch_media(media_id)
    if not data:
        raise RuntimeError("empty voice")
    d = os.path.join(MEDIA, time.strftime("%Y-%m-%d"))
    os.makedirs(d, exist_ok=True)
    stem = os.path.join(d, hashlib.md5((media_id or "").encode()).hexdigest()[:16])
    src = stem + "." + ext
    if not os.path.exists(src):
        with open(src, "wb") as f:
            f.write(data)
    wav = stem + ".wav"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", src,
                    "-ar", "16000", "-ac", "1", wav], timeout=60, check=True)
    with open(wav, "rb") as f:
        body = f.read()
    req = urllib.request.Request(ASR_URL, data=body, method="POST",
                                 headers={"Authorization": "Bearer " + ASR_TOKEN,
                                          "Content-Type": "audio/wav"})
    with urllib.request.urlopen(req, timeout=120) as r:
        text = (json.loads(r.read().decode("utf-8")).get("text") or "").strip()
    if not text:
        raise RuntimeError("asr empty")
    return text


def is_sticker(path):
    """表情包判定：PNG 带透明、或短边 <= 480 的小图，或 gif 动图。"""
    try:
        data = open(path, "rb").read()
        if data[:4] == b"GIF8":
            return True
        if data[1:4] == b"PNG":
            w, h = struct.unpack(">II", data[16:24])
            if b"tRNS" in data[:8192]:
                return True
            return max(w, h) <= 480
        if data[:2] == b"\xff\xd8":
            i = 2
            while i < len(data) - 9:
                if data[i] != 0xFF:
                    i += 1
                    continue
                m = data[i + 1]
                if m in (0xC0, 0xC1, 0xC2):
                    h, w = struct.unpack(">HH", data[i + 5:i + 9])
                    return max(w, h) <= 480
                if m in (0xD8, 0xD9) or 0xD0 <= m <= 0xD7:
                    i += 2
                    continue
                i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
    except Exception:
        pass
    return False


def url_of(path):
    """把本地图片转成 data URI 给视觉模型（大图先压到 896 宽、只取首帧）。"""
    src = path
    try:
        if os.path.getsize(path) > 250 * 1024:
            out = os.path.join("/tmp", "wc_" + os.path.basename(path) + ".jpg")
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", path,
                            "-frames:v", "1", "-vf", "scale='min(896,iw)':-2",
                            "-q:v", "5", "-f", "image2", "-update", "1", out],
                           timeout=30, check=True)
            if os.path.exists(out) and os.path.getsize(out) > 0:
                src = out
    except Exception as e:
        log("shrink fail", repr(e)[:120])
    ext = os.path.splitext(src)[1].lower().lstrip(".")
    mime = {"png": "image/png", "gif": "image/gif", "webp": "image/webp"}.get(ext, "image/jpeg")
    with open(src, "rb") as f:
        return "data:%s;base64,%s" % (mime, base64.b64encode(f.read()).decode())


def upload_media(path, mtype):
    """上传临时素材（image / voice / file），返回 media_id。"""
    boundary = "----wc" + hashlib.md5(os.urandom(8)).hexdigest()
    name = os.path.basename(path)
    body = b""
    body += ("--%s\r\nContent-Disposition: form-data; name=\"media\"; filename=\"%s\"\r\n"
             "Content-Type: application/octet-stream\r\n\r\n" % (boundary, name)).encode()
    body += open(path, "rb").read() + b"\r\n"
    body += ("--%s--\r\n" % boundary).encode()
    url = "https://qyapi.weixin.qq.com/cgi-bin/media/upload?access_token=%s&type=%s" % (
        urllib.parse.quote(access_token()), mtype)
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
    d = json.loads(urllib.request.urlopen(req, timeout=60).read().decode())
    if d.get("errcode"):
        raise RuntimeError("upload %s %s" % (d.get("errcode"), d.get("errmsg")))
    return d["media_id"]


def send_media(user, path, mtype):
    mid = upload_media(path, mtype)
    d = http_json("https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token=" + urllib.parse.quote(access_token()),
                  {"touser": user, "msgtype": mtype, "agentid": int(cfg()["agent_id"]),
                   mtype: {"media_id": mid}}, timeout=30)
    if d.get("errcode"):
        raise RuntimeError("send %s %s" % (d.get("errcode"), d.get("errmsg")))
    return d


def pick_sticker():
    try:
        fs = [os.path.join(STICKER, x) for x in os.listdir(STICKER)
              if os.path.splitext(x)[1].lower() in (".jpg", ".png", ".gif", ".webp")]
    except Exception:
        fs = []
    return random.choice(fs) if fs else None


def field(xml, name):
    m = re.search(r"<%s>(?:<!\[CDATA\[(.*?)\]\]>|(.*?))</%s>" % (name, name), xml, re.S)
    if not m:
        return ""
    return (m.group(1) if m.group(1) is not None else m.group(2) or "").strip()


TTS_MODEL = "speech-2.8-hd"
TTS_VOICE = os.getenv("TTS_VOICE", "YOUR_VOICE_ID")
TTS_URL = "https://api.minimaxi.com/v1/t2a_v2"
# 正文里万一混进语音标签（(breath) / <#0.3#>），发出去前清掉
SPEECH_TAG = re.compile(
    r"\((?:laughs|chuckle|coughs|clear-throat|groans|breath|pant|inhale|exhale|gasps|sniffs|"
    r"sighs|snorts|burps|lip-smacking|humming|hissing|emm|sneezes)\)|<#\d+(?:\.\d+)?#>")


def tts_bytes(text):
    """直连 MiniMax（speech-2.8-hd），不再经网关转一道。返回 mp3。"""
    body = json.dumps({"model": TTS_MODEL, "text": text[:600], "stream": False,
                       "voice_setting": {"voice_id": TTS_VOICE, "speed": 1.0, "vol": 1.0, "pitch": 0},
                       "audio_setting": {"sample_rate": 24000, "bitrate": 128000,
                                         "format": "mp3", "channel": 1}}).encode()
    req = urllib.request.Request(TTS_URL, data=body,
                                 headers={"Authorization": "Bearer " + cfg()["minimax_key"],
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=90) as r:
        d = json.loads(r.read().decode())
    base = d.get("base_resp") or {}
    if base.get("status_code"):
        raise RuntimeError("TTS %s %s" % (base.get("status_code"), base.get("status_msg")))
    audio = (d.get("data") or {}).get("audio") or ""
    if not audio:
        raise RuntimeError("TTS returned no audio")
    return bytes.fromhex(audio)


def send_voice(user, text, script=""):
    """文字 -> TTS(mp3) -> AMR-NB -> 上传素材 -> 发语音消息。全程记耗时。

    script 是主回复里一起写好的台词（带 (breath)/<#0.3#>），没有就把文字原样念。
    """
    line = script or text
    t0 = time.time()
    mp3 = "/tmp/wc_tts.mp3"
    amr = "/tmp/wc_tts.amr"
    with open(mp3, "wb") as f:
        f.write(tts_bytes(line))
    t1 = time.time()
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", mp3,
                    "-ar", "8000", "-ac", "1", "-c:a", "libopencore_amrnb", "-b:a", "12.2k", amr],
                   timeout=60, check=True)
    t2 = time.time()
    if not os.path.exists(amr) or os.path.getsize(amr) > 1900000:
        raise RuntimeError("amr too big")
    send_media(user, amr, "voice")
    t3 = time.time()
    log("voice time tts=%.2fs amr=%.2fs upload+send=%.2fs total=%.2fs chars=%d"
        % (t1 - t0, t2 - t1, t3 - t2, t3 - t0, len(text)))


def fallback_greet():
    return "你来啦。我刚还想着，你今天会不会来找我。"


def xc_event(kind=None):
    """把这次互动回传心潮，驱动情绪/驱力演化。"""
    args = {"event_id": "wecom-" + uuid.uuid4().hex[:10]}
    if kind:
        args["interaction_type"] = kind
    try:
        return xc_call("xinchao_event", args)
    except Exception as e:
        log("event fail", repr(e)[:100])
        return ""


def send_reply(user, reply):
    """处理 [信] / [表情包] / [语音] / [记] / [心] 标记。"""
    letter = ""
    images = []
    m = re.search(r"\[信\]\s*([\s\S]+)$", reply)
    if m:
        letter = m.group(1).strip()
        reply = reply[:m.start()].strip()
        if letter:
            if "[配图]" in letter:
                letter = letter.replace("[配图]", "").strip()
                p = LAST_IMG.get(user)
                if p and os.path.exists(p) and time.time() - os.path.getmtime(p) < 6 * 3600:
                    try:
                        u = cabin_upload(p)
                        if u:
                            images.append(u)
                    except Exception as e:
                        log("cabin img fail", repr(e)[:120])
            threading.Thread(target=xc_letter, args=(letter, images), daemon=True).start()
            log("letter out", user, len(letter), len(images))
    notes = re.findall(r"\[记\]\s*(.+)", reply)
    reply = re.sub(r"[ \t]*\[记\][^\n]*", "", reply)
    for n in notes:
        threading.Thread(target=xc_hold, args=(n.strip(),), daemon=True).start()
    for spec in re.findall(r"\[提醒\]\s*(.+)", reply):
        try:
            r = add_reminder(spec)
            if r:
                log("remind add", r[1][:40], time.strftime("%m-%d %H:%M", time.localtime(r[0])))
        except Exception as e:
            log("remind add fail", repr(e)[:120])
    reply = re.sub(r"[ \t]*\[提醒\][^\n]*", "", reply)
    emo = re.findall(r"\[心\]\s*([a-z_]+)", reply)
    reply = re.sub(r"[ \t]*\[心\][^\n]*", "", reply)
    kind = next((k for k in emo if k in ("affection", "intimacy", "sharing",
                                        "discovery", "task_progress", "conflict")), None)
    threading.Thread(target=xc_event, args=(kind,), daemon=True).start()
    want_sticker = "[表情包]" in reply
    # 语音台词跟正文同一次生成，不再单独调一次模型
    vm = re.search(r"\[声\]\s*([^\n]+)", reply)
    script = vm.group(1).strip() if vm else ""
    if vm:
        reply = reply.replace(vm.group(0), "")
    want_voice = bool(script) or "[语音]" in reply
    text = SPEECH_TAG.sub("", reply.replace("[表情包]", "").replace("[语音]", "")).strip()
    if not text and letter:
        text = "信我写好了，放在小屋里了。"
    said = False
    if text and (want_voice or (len(text) <= 60 and random.random() < 0.12)):
        try:
            send_voice(user, text, script)
            log("voice out", user, (script or text)[:40])
            said = True
        except Exception as e:
            log("voice fail", repr(e)[:140])
    if text and not said:
        send_text(user, text)
        said = True
    if want_sticker:
        p = pick_sticker()
        if p:
            try:
                time.sleep(0.8)
                send_media(user, p, "image")
                log("sticker out", user, os.path.basename(p))
                return
            except Exception as e:
                log("sticker fail", repr(e)[:120])
        if not said:
            send_text(user, "表情包库还是空的，你多存几个我才有得发。")


def chat_text(user, text):
    """一句话交给大脑：日志 / 时间轴 / 记忆 / 回复。文字和语音转写共用。"""
    log("in", user, text[:80])
    reply = llm(text, user) or "刚刚走神了，再说一遍？"
    log("out", user, reply[:80])
    _last_user_at["t"] = time.time()
    if user == OWNER:
        timeline_add("聊天", text)
    remember(user, text, hist_text(reply))
    send_reply(user, reply)


def handle(xml):
    try:
        mtype = field(xml, "MsgType")
        user = field(xml, "FromUserName")
        if not user:
            return
        if mtype == "event":
            ev = field(xml, "Event")
            if ev in ("subscribe", "enter_agent"):
                def greet():
                    try:
                        g = llm("对方刚打开会话，来找你了。用一句话打招呼，带上你记得的关于他的一件事，"
                                "口语、有温度，别用『有事直接说』这种客服腔，别超过 25 个字。", user)
                        send_text(user, g or fallback_greet())
                    except Exception:
                        send_text(user, fallback_greet())
                threading.Thread(target=greet, daemon=True).start()
            return
        if mtype in ("image", "emotion"):
            pic = field(xml, "PicUrl")
            mid = field(xml, "MediaId")
            try:
                path, size = save_image(mid, pic)
            except Exception as e:
                log("img fail", repr(e)[:160])
                send_text(user, "图没下下来，再发一次？")
                return
            log("img", user, path, size)
            LAST_IMG[user] = path
            if mtype == "emotion" or is_sticker(path):
                os.makedirs(STICKER, exist_ok=True)
                dst = os.path.join(STICKER, os.path.basename(path))
                if not os.path.exists(dst):
                    shutil.copy(path, dst)
                hint = ("对方发来一个表情包，已经存进你的表情包库了。看图回应，1-2 句，语气自然，"
                        "别像在解说图片；想回一个表情包就在结尾加 [表情包]。")
            else:
                hint = "对方发来一张图片。看图回应，1-2 句，自然一点，别当图注写。"
            reply = llm([{"type": "text", "text": hint},
                         {"type": "image_url", "image_url": {"url": url_of(path)}}], user)
            reply = reply or "图收到了，就是没太看懂。"
            remember(user, "[发来一张图]" if mtype == "image" else "[发来一个表情包]", hist_text(reply))
            send_reply(user, reply)
            return
        if mtype == "voice":
            text = field(xml, "Recognition")
            if not text:
                try:
                    text = transcribe_voice(field(xml, "MediaId"))
                    log("stt", user, text[:70])
                except Exception as e:
                    log("stt fail", repr(e)[:180])
                    send_text(user, "这次没听清，再说一遍？")
                    return
            if text:
                chat_text(user, text)
            return
        if mtype != "text":
            send_text(user, "这个格式我还处理不了，发文字或图片吧。")
            return
        text = field(xml, "Content")
        if not text:
            return
        chat_text(user, text)
    except Exception as e:
        log("handle fail", repr(e)[:200])


# ---------- 时间感 / 生活时间轴 / 提醒 / 随机唤醒 / 面板 ----------

TIMELINE_FILE = os.path.join(BASE, "timeline.jsonl")
REMIND_FILE = os.path.join(BASE, "reminders.json")
DIARY_FILE = os.path.join(BASE, "diary.json")
PANEL_TOKEN = "YOUR_TOKEN"
_last_user_at = {"t": 0}


def now_str():
    return time.strftime("%m月%d日 %H:%M") + " 周" + "日一二三四五六"[int(time.strftime("%w"))]


def timeline_add(kind, text):
    try:
        with open(TIMELINE_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": int(time.time()), "kind": kind, "text": text[:200]},
                               ensure_ascii=False) + "\n")
    except Exception as e:
        log("timeline fail", repr(e)[:100])


def timeline_today(limit=40):
    day0 = time.mktime((time.localtime().tm_year, time.localtime().tm_mon,
                        time.localtime().tm_mday, 0, 0, 0, 0, 0, -1))
    out = []
    try:
        with open(TIMELINE_FILE, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if (o.get("t") or 0) >= day0:
                    out.append(o)
    except Exception:
        pass
    return out[-limit:]


def reminders_load():
    try:
        with open(REMIND_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def reminders_save(rows):
    try:
        tmp = REMIND_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False)
        os.replace(tmp, REMIND_FILE)
    except Exception as e:
        log("remind save fail", repr(e)[:100])


def add_reminder(spec):
    """[提醒]21:00 吃药 / [提醒]11-13 08:30 交材料"""
    spec = spec.strip()
    now = time.time()
    lt = time.localtime(now)
    m = re.match(r"(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})\s+(.+)", spec)
    if m:
        mo, dd, hh, mm, body = m.groups()
        t = time.mktime((lt.tm_year, int(mo), int(dd), int(hh), int(mm), 0, 0, 0, -1))
    else:
        m = re.match(r"(\d{1,2}):(\d{2})\s+(.+)", spec)
        if not m:
            return None
        hh, mm, body = m.groups()
        t = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, int(hh), int(mm), 0, 0, 0, -1))
        if t <= now:
            t += 86400
    rows = reminders_load()
    rows.append({"at": int(t), "text": body.strip()[:200]})
    reminders_save(rows)
    return t, body.strip()


def reminder_loop():
    while True:
        try:
            now = time.time()
            rows = reminders_load()
            keep = []
            for r in rows:
                if r["at"] <= now:
                    if now - r["at"] < 7200:
                        send_text(OWNER, "⏰ " + r["text"])
                        timeline_add("提醒", r["text"])
                        remember(OWNER, None, "（提醒）" + r["text"])
                        log("remind sent", r["text"][:40])
                else:
                    keep.append(r)
            if len(keep) != len(rows):
                reminders_save(keep)
        except Exception as e:
            log("remind loop fail", repr(e)[:120])
        time.sleep(30)


_checkin = {"day": "", "n": 0}


def checkin_once():
    """随机醒来一次，自己决定要不要找他。"""
    today = time.strftime("%Y-%m-%d")
    if _checkin["day"] != today:
        _checkin["day"], _checkin["n"] = today, 0
    if _checkin["n"] >= 5:
        return
    lt = time.localtime()
    if not (8 <= lt.tm_hour < 23):
        return
    gap = (time.time() - (_last_user_at["t"] or 0)) / 60
    if gap < 25:
        return
    rows = timeline_today(12)
    life = "\n".join("- %s %s" % (time.strftime("%H:%M", time.localtime(r["t"])), r["text"])
                     for r in rows) or "（今天还没聊过）"
    ctx = xc_context()
    ask = ("现在是 %s%s。\n他今天的情况：\n%s\n%s\n\n"
           "你可以主动给他发一条企微消息，也可以什么都不发。"
           "如果现在没什么想说的、或者怕打扰他，就只输出 [沉默]；"
           "想说就输出一句自然的话，20 字以内，可以带 [语音] 或 [表情包]。"
           % (now_str(), ("（他上次说话是 %d 分钟前）" % gap) if gap < 24 * 60 else "", life,
              ("\n你此刻的状态：" + ctx[:600]) if ctx else ""))
    reply = llm(ask, OWNER)
    _checkin["n"] += 1
    if not reply or "[沉默]" in reply:
        log("checkin silent", round(gap))
        return
    send_reply(OWNER, reply)
    remember(OWNER, None, hist_text(reply))
    log("checkin out", reply[:60])


def checkin_loop():
    """随机轮询唤醒：50-140 分钟醒一次，醒来自己判断。"""
    while True:
        time.sleep(random.randint(50, 140) * 60)
        try:
            checkin_once()
        except Exception as e:
            log("checkin fail", repr(e)[:140])


PANEL_HTML = ""  # 面板已拆到 /opt/wecom/panel.html


CAT_COLORS = {
    "休息": "#8b7cf6", "吃饭": "#ff8a6b", "工作": "#f0a24b", "学习": "#e0b34e",
    "健康": "#4fc3a1", "社交": "#5b9cf5", "娱乐": "#c58cf0", "出行": "#66c2e8",
    "家务": "#e879b9", "生活": "#f2a0a0",
}

# 事件名 → 大类：标签用具体事件（睡觉/通勤/编码），颜色用大类
SUBS = [
    ("睡觉", "休息", ("睡", "晚安", "困", "躺")),
    ("午睡", "休息", ("午睡", "眯一会")),
    ("发呆", "休息", ("发呆", "放空", "无聊")),
    ("吃饭", "吃饭", ("吃", "饭", "外卖", "奶茶", "咖啡", "早餐", "午餐", "晚餐", "零食", "喝")),
    ("做饭", "家务", ("做饭", "下厨", "炒")),
    ("写论文", "工作", ("论文", "稿", "报告")),
    ("编码", "工作", ("代码", "bug", "部署", "脚本", "接口")),
    ("开会", "工作", ("会议", "开会", "组会")),
    ("上课", "学习", ("上课", "作业", "复习", "看书", "考试")),
    ("健身", "健康", ("健身", "撸铁", "跑步", "运动", "瑜伽")),
    ("洗澡", "健康", ("洗澡", "洗漱")),
    ("通勤", "出行", ("地铁", "公交", "打车", "通勤", "路上")),
    ("出门", "出行", ("出门", "回家", "去")),
    ("见朋友", "社交", ("朋友", "聚", "约")),
    ("聊天", "社交", ("妈妈", "爸", "聊", "电话")),
    ("追剧", "娱乐", ("剧", "电影", "视频")),
    ("打游戏", "娱乐", ("游戏",)),
    ("刷手机", "娱乐", ("小红书", "抖音", "刷")),
    ("听音乐", "娱乐", ("音乐", "歌")),
    ("做家务", "家务", ("收拾", "打扫", "洗衣", "整理")),
    ("买东西", "生活", ("买菜", "快递", "买")),
]
TITLES = [t for t, _, _ in SUBS]


def guess_event(text):
    t = text or ""
    for title, cat, kws in SUBS:
        for k in kws:
            if k in t:
                return title, cat
    return "日常", "生活"


TAGS_FILE = os.path.join(BASE, "tags.json")
_tags = {}


def tags_load():
    try:
        with open(TAGS_FILE, "r", encoding="utf-8") as f:
            _tags.update(json.load(f))
    except Exception:
        pass


def tags_save():
    try:
        tmp = TAGS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(dict(list(_tags.items())[-800:]), f, ensure_ascii=False)
        os.replace(tmp, TAGS_FILE)
    except Exception:
        pass


LABEL_SYS = (
    "你是生活轨迹标注器。根据一段时间里主人说的话，判断他当时主要在做什么。"
    "只输出一行 JSON，形如 {\"title\":\"睡觉\",\"cat\":\"休息\"}。"
    "title 必须从给定事件里选，cat 必须从给定大类里选，不要自创、不要解释。"
)

LABEL_PROMPT = (
    "时间：{when}\n主人说的话：{lines}\n可选事件：{titles}\n可选大类：{cats}\n只回一行 JSON。"
)


def gw_label(prompt):
    body = {"model": "DeepSeek-V4.1-Flash",
            "messages": [{"role": "system", "content": LABEL_SYS},
                         {"role": "user", "content": prompt}],
            "temperature": 0.2, "max_tokens": 60, "stream": False}
    try:
        d = http_json(GW + "/v1/chat/completions", body,
                      {"Authorization": "Bearer " + GW_TOKEN}, timeout=40)
    except Exception:
        body["model"] = "luci-auto"
        d = http_json(GW + "/v1/chat/completions", body,
                      {"Authorization": "Bearer " + GW_TOKEN}, timeout=40)
    t = (((d.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
    m = re.search(r"\{.*?\}", t, re.S)
    if not m:
        return None
    try:
        o = json.loads(m.group(0))
    except Exception:
        return None
    title = (o.get("title") or "").strip()
    cat = (o.get("cat") or "").strip()
    if title not in TITLES or cat not in CAT_COLORS:
        return None
    return {"title": title, "cat": cat}


def sessions_of(ev):
    """间隔 20 分钟以内的消息算同一次。"""
    out = []
    for o in ev:
        if out and o["t"] - out[-1][-1]["t"] <= 20 * 60:
            out[-1].append(o)
        else:
            out.append([o])
    return out


def label_loop():
    """后台给还没打过标签的生活时段叫模型总结事件名，缓存到 tags.json。"""
    while True:
        try:
            n = 0
            for s in sessions_of(timeline_since(10)):
                key = str(int(s[0]["t"]))
                if key in _tags:
                    continue
                txt = " / ".join((o.get("text") or "")[:60] for o in s
                                 if (o.get("text") or "").strip())
                if not txt:
                    continue
                r = gw_label(LABEL_PROMPT.format(
                    when=time.strftime("%m-%d %H:%M", time.localtime(s[0]["t"])),
                    lines=txt[:600], titles="、".join(TITLES), cats="、".join(CAT_COLORS)))
                _tags[key] = r or {"title": guess_event(txt)[0], "cat": guess_event(txt)[1]}
                n += 1
                if n >= 4:
                    break
            if n:
                tags_save()
        except Exception as e:
            log("label fail", repr(e)[:120])
        time.sleep(90)


def timeline_since(days):
    cut = time.time() - days * 86400
    out = []
    try:
        with open(TIMELINE_FILE, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if (o.get("t") or 0) >= cut:
                    out.append(o)
    except Exception:
        pass
    out.sort(key=lambda x: x["t"])
    return out


def panel_data():
    ev = timeline_since(32)
    items = []
    for s in sessions_of(ev):
        start = s[0]["t"]
        end = min(s[-1]["t"] + 600, start + 180 * 60)
        txt, seen, remind = [], set(), False
        for o in s:
            t = re.sub(r"\[[^\]]{1,10}\]", "", o.get("text") or "").strip()
            if o.get("kind") == "提醒":
                remind = True
            if t and t not in seen:
                seen.add(t)
                txt.append(t[:60])
        body = " · ".join(txt)[:160]
        if remind:
            title, cat, color = "提醒", "提醒", "#e8c34a"
        else:
            tg = _tags.get(str(int(start)))
            if tg:
                title, cat = tg["title"], tg["cat"]
            else:
                title, cat = guess_event(body)
            color = CAT_COLORS.get(cat, "#f2a0a0")
        items.append({"t": start, "end": end, "title": title, "cat": cat,
                      "color": color, "text": body})
    # 相邻同类同名、间隔 40 分钟内的事件合并成一段生活时段
    merged = []
    for b in items:
        p = merged[-1] if merged else None
        if p and p["title"] == b["title"] and (b["t"] - p["end"]) <= 40 * 60:
            p["end"] = max(p["end"], b["end"])
            if b["text"] and b["text"] not in p["text"]:
                p["text"] = (p["text"] + " · " + b["text"])[:160]
        else:
            merged.append(dict(b))
    blocks = [{"t": b["t"], "title": b["title"], "cat": b["cat"], "color": b["color"],
               "text": b["text"], "dur": round((b["end"] - b["t"]) / 60.0, 1)} for b in merged]
    return {"now": now_str(), "blocks": blocks}


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path in ("/panel", "/panel/data"):
            t = (urllib.parse.parse_qs(u.query).get("t") or [""])[0]
            if t != PANEL_TOKEN:
                return self._send(403, b"forbidden")
            if u.path == "/panel/data":
                return self._send(200, json.dumps(panel_data(), ensure_ascii=False).encode(),
                                  "application/json; charset=utf-8")
            try:
                with open(os.path.join(BASE, "panel.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            except Exception:
                return self._send(200, PANEL_HTML.encode(), "text/html; charset=utf-8")
        if u.path != "/wecom/callback":
            return self._send(404, b"not found")
        q = urllib.parse.parse_qs(u.query)
        echostr = (q.get("echostr") or [""])[0]
        sig = (q.get("msg_signature") or [""])[0]
        ts = (q.get("timestamp") or [""])[0]
        nonce = (q.get("nonce") or [""])[0]
        c = crypt()
        if c.signature(ts, nonce, echostr) != sig:
            return self._send(403, b"bad signature")
        try:
            plain = c.decrypt(echostr)
        except Exception as e:
            log("decrypt fail", repr(e)[:160])
            return self._send(500, b"decrypt fail")
        log("verify ok")
        self._send(200, plain.encode("utf-8"))

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        if u.path != "/wecom/callback":
            return self._send(404, b"not found")
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8", "ignore")
        q = urllib.parse.parse_qs(u.query)
        m = re.search(r"<Encrypt><!\[CDATA\[(.*?)\]\]></Encrypt>", raw, re.S)
        if not m:
            return self._send(200, b"")
        enc = m.group(1)
        c = crypt()
        if c.signature((q.get("timestamp") or [""])[0], (q.get("nonce") or [""])[0], enc) != (q.get("msg_signature") or [""])[0]:
            return self._send(403, b"")
        try:
            xml = c.decrypt(enc)
        except Exception:
            return self._send(200, b"")
        self._send(200, b"")
        threading.Thread(target=handle, args=(xml,), daemon=True).start()


if __name__ == "__main__":
    os.makedirs(BASE, exist_ok=True)
    load_hist()
    cabin_load()
    tags_load()
    xc_thread()
    threading.Thread(target=cabin_loop, daemon=True).start()
    threading.Thread(target=diary_loop, daemon=True).start()
    threading.Thread(target=reminder_loop, daemon=True).start()
    threading.Thread(target=checkin_loop, daemon=True).start()
    threading.Thread(target=label_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    log("listening", PORT)
    srv.serve_forever()
