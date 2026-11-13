# wecom-voice-bot

企业微信自建应用的**语音与表情包**支持。官方文档只给了回调 XML 的字段说明，
但真正跑起来要处理 AMR 转码、`MediaId` 下载、`Recognition` 复用、5 秒超时、表情包判定——
这个仓库把这些都写好了。

## 能力

| 消息类型 | 处理方式 |
| --- | --- |
| `text` | 直接转发给后端 |
| `image` | 下载图片，交给支持视觉的后端 |
| `voice` | 先读 `Recognition`；没有就下载 AMR → 16k WAV → 转写 |
| `emotion` | 按表情包处理，不走进图片理解 |

回复支持文本与语音（`[声]` 前缀触发 TTS）。

## 快速开始

```bash
pip install -r requirements.txt   # 只用标准库，无需额外依赖
cp .env.example .env              # 填 CorpID / AgentId / Secret
python3 wecom_server.py           # 默认 127.0.0.1:18114
```

企业微信后台：

1. 应用管理 → 自建应用 → 记下 `CorpID`、`AgentId`、`Secret`
2. 接收消息 → 设置 API 接收 → 填回调 URL、Token、EncodingAESKey
3. 可信 IP 里加上你的出口 IP

**第一次接入建议直接看 [docs/WECOM-SETUP.md](docs/WECOM-SETUP.md)**：
从注册企业微信到收到第一条消息的完整步骤，含回调验证、可信 IP、常见错误码。

## 主动联系

不是等你说话才回——它会自己找人。

**随机问候（`checkin_loop`）**

- 每 50–140 分钟随机醒一次，醒来交给模型自己判断
- 每天最多 5 次，只在 8:00–23:00 之间
- 距你上次说话不足 25 分钟就跳过，避免打断
- 会带上今天的对话时间线和当前状态，让模型决定：
  输出 `[沉默]` 就什么都不发；想说就一句 20 字以内，可以带 `[语音]` 或 `[表情包]`
- 发出后写进记忆，下次对话能接上

**定时提醒（`reminder_loop`）**

每 30 秒扫一次提醒队列，到点主动推送 `⏰ ...`；超过 2 小时的过期提醒不再补发。

想调频率就改这几个常量：

| 参数 | 位置 | 默认 |
| --- | --- | --- |
| 每日上限 | `checkin_once()` 的 `_checkin["n"] >= 5` | 5 次 |
| 唤醒间隔 | `checkin_loop()` 的 `randint(50, 140)` | 50–140 分钟 |
| 静默阈值 | `gap < 25` | 25 分钟 |
| 活跃时段 | `8 <= lt.tm_hour < 23` | 8–23 点 |

## 自主发图

和表情包同一套思路：**画不画由模型自己判断**，不是关键词触发。

- 你说「看看你的猫」「画一个下雨的窗」，或者它自己想分享一个画面 → 回复里带 `[图] 具体描述`
- 服务端异步生成，先发文字、图随后到，不阻塞对话
- 每天最多 3 张；失败只写日志，不会在聊天里报错
- 渠道按 `config.json` 的 `images` 列表依次尝试，第一个失败自动换下一个

`config.json` 加一段（示例是 agnes）：

```json
"images": [
  {
    "provider": "agnes",
    "url": "https://api.agnes-ai.cn/v1/images/generations",
    "model": "agnes-image-2.5-flash",
    "key": "sk-...",
    "size": "1024x1024"
  }
]
```

两处容易踩：

- 域名是 `api.agnes-ai.cn`，不是 `apihub.agnes-ai.cn`（后者会返回 `401 Invalid token`）
- 接口返回 `{"data":[{"url":"https://cos-...png"}]}`，服务端会再下载一次；也支持 `b64_json` 直出

不配置 `images` 时功能自动关闭，`[图]` 会被当作普通文本清理掉。

## 语音链路

```
回调 XML(voice) → 有 Recognition? ── 有 ──→ 直接用
                        │ 没有
                        ↓
          下载 MediaId → ffmpeg -ar 16000 -ac 1 → WAV
                        ↓
      POST /audio/transcriptions（可用本地 SenseVoice / 任意云端 ASR）
                        ↓
                      文本 → 后端
```

默认转写端点是本地服务，可以改成任何兼容 `POST /audio/transcriptions` 的实现
（见 `wecom_server.py` 里的 `transcribe_voice()`）。

## 必读的坑

- **5 秒超时**：回调里只解析并入队，立即返回空串，耗时处理放后台线程
- **表情包不是图片**：用 `MsgType=emotion` 判断，不看后缀
- **AMR 格式**：企微语音是 AMR-NB，必须转码才能给 ASR
- **`Recognition` 优先**：企微自己已经转好文字时不要再花一次转写

完整的踩坑记录见 [luci-channels/docs/PITFALLS.md](https://github.com/2921783038/luci-channels/blob/main/docs/PITFALLS.md)。

## 注意

- 仅用于自己的企业微信机器人，遵守企业微信的接口使用规范
- 不要把这个服务直接暴露到公网，只通过反向代理 + token 访问
- 回调 URL 必须是 HTTPS（企业微信强制要求）

## License

MIT

---

如果这个项目帮到你，点个 ⭐ Star 支持一下。