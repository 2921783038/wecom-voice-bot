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
