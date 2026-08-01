# 门锁门铃回复音频部署指南

本文档说明如何在一台新的本地设备上跑通这条链路：

```text
门锁门铃事件 → Miloco 后端 → 指定 OpenClaw 会话 → OpenClaw 回复文本
→ OpenClaw 插件先唤醒门锁 → OpenClaw 侧生成/转换音频 → audio_sender 发送到门锁播放
```

职责边界：Miloco 只识别 MIoT 门铃事件并把 `doorbellReplyAudio` 请求交给 OpenClaw；OpenClaw 负责拿到回复文本后执行唤醒、TTS、PCM 转码和门锁音频发送。

## 1. 前置条件

- OpenClaw gateway 可用，访问地址类似 `http://127.0.0.1:18789/`。
- Miloco 已安装并完成小米账号绑定，`miloco-cli device list` 能看到门锁设备。
- 门锁与运行 OpenClaw/Miloco 的本机处于可互通网络，`audio_sender` 能连到门锁音频服务端口。
- 本机已有可用 TTS 能力。当前验证路径使用 OpenClaw workspace 里的 `edge-tts` skill，再用 `ffmpeg` 转 16 kHz PCM。
- 本机已安装 `ffmpeg`、能运行 `miloco-cli`、能运行 `audio_sender`。

## 2. 找到门铃事件三元组

先观察 Miloco 后端日志：

```bash
tail -f ~/.openclaw/miloco/log/miloco-backend.log
```

按一次门铃，找到类似日志：

```text
[DEBUG-EVENT] topic=device/<did>/up/event_occured/<siid>/<eiid> payload=...
```

记录：

- `did`：门锁设备 ID。
- `siid`：事件服务 ID。
- `eiid`：事件 ID。

当前已验证的小米智能门锁 5 Max 内外双摄门铃事件是 `siid=7`、`eiid=1006`。不同设备或固件可能不同，以日志为准。

## 3. 找到 OpenClaw 目标会话

打开希望接收门铃事件的 OpenClaw 会话，浏览器 URL 类似：

```text
http://127.0.0.1:18789/chat?session=agent%3Amain%3Adashboard%3Axxxx
```

把 `session` 参数 URL decode 后得到 `sessionKey`：

```text
agent:main:dashboard:xxxx
```

后续配置写这个 `sessionKey`。

## 4. 验证门锁唤醒 action

门锁播放音频前必须先唤醒。当前验证过的唤醒 action 是：

```bash
miloco-cli device action <did> action.17.3
```

如果换设备后不生效，先用设备 spec / `miloco-cli device catalog` 找到实际唤醒 action，再把配置里的 `doorbell_wake_action_iid` 改成对应值。

## 5. 准备音频发送脚本

推荐把 OpenClaw 侧的 TTS、转码、发送门锁音频封装成一个脚本，例如：

```bash
mkdir -p ~/.openclaw/miloco/scripts
cat > ~/.openclaw/miloco/scripts/doorbell_reply_audio.sh <<'EOF_SCRIPT'
#!/usr/bin/env bash
set -euo pipefail

TEXT="${1:-}"
if [[ -z "$TEXT" ]]; then
  echo "missing text" >&2
  exit 2
fi

BASE="/tmp/doorbell_reply_audio_$$"
MP3="${BASE}.mp3"
PCM="${BASE}.pcm"
cleanup() { rm -f "$MP3" "$PCM"; }
trap cleanup EXIT

cd "$HOME/.openclaw/workspace/skills/edge-tts"
/opt/homebrew/bin/python3 scripts/tts.py "$TEXT" -v zh-CN-YunjianNeural -o "$MP3"
ffmpeg -hide_banner -loglevel error -y -i "$MP3" \
  -f s16le -acodec pcm_s16le -ar 16000 -ac 1 "$PCM"
/Users/<you>/Projects/audio_sender/audio_sender <doorlock-ip> 9527 file "$PCM"
EOF_SCRIPT
chmod +x ~/.openclaw/miloco/scripts/doorbell_reply_audio.sh
```

需要按本机实际情况替换：

- `/opt/homebrew/bin/python3`：必须是装有 `edge_tts` 模块的 Python。用下面命令确认：

  ```bash
  /opt/homebrew/bin/python3 -c 'import edge_tts; print(edge_tts.__file__)'
  ```

- `/Users/<you>/Projects/audio_sender/audio_sender`：`audio_sender` 可执行文件路径。
- `<doorlock-ip>`：门锁音频服务 IP。
- `9527`：门锁音频服务端口，以你验证过的值为准。
- `zh-CN-YunjianNeural`：TTS 音色，可按 OpenClaw 侧 TTS 能力调整。

先单独验证脚本：

```bash
~/.openclaw/miloco/scripts/doorbell_reply_audio.sh '测试门锁语音播放。'
```

看到 `Device should be playing audio now.` 且门锁有声音，说明音频发送脚本 OK。

> 常见坑：OpenClaw gateway 是 LaunchAgent/daemon 环境时，`python3` 可能解析到系统 Python，缺少 `edge_tts`。脚本里请写绝对 Python 路径，不要依赖交互式 shell 的 `PATH`。

## 6. 写入 Miloco 运行配置

建议把真实设备 ID、本机会话和脚本路径写到用户配置，不提交到仓库：

```bash
miloco-cli config set miot.doorbell_did '<did>'
miloco-cli config set miot.doorbell_siid 7
miloco-cli config set miot.doorbell_eiid 1006
miloco-cli config set miot.doorbell_session_key 'agent:main:dashboard:xxxx'
miloco-cli config set miot.doorbell_wake_action_iid 'action.17.3'
```

`doorbell_reply_audio_command` 是数组；当前 CLI 对复杂数组配置不做 JSON 解析，建议直接编辑 `~/.openclaw/miloco/config.json`：

```json
{
  "miot": {
    "doorbell_did": "<did>",
    "doorbell_siid": 7,
    "doorbell_eiid": 1006,
    "doorbell_session_key": "agent:main:dashboard:xxxx",
    "doorbell_wake_action_iid": "action.17.3",
    "doorbell_reply_audio_command": [
      "/Users/<you>/.openclaw/miloco/scripts/doorbell_reply_audio.sh",
      "{text}"
    ]
  }
}
```

`doorbell_reply_audio_command` 支持占位符：

- `{text}`：OpenClaw 回复文本。
- `{did}`：门锁 did。
- `{siid}`：门铃事件 siid。
- `{eiid}`：门铃事件 eiid。
- `{wakeActionIid}`：门锁唤醒 action iid。

Miloco 会把这些值传给 OpenClaw 插件；OpenClaw 插件会先执行唤醒 action，再执行音频命令。

## 7. 安装/重启服务

开发分支本地验证时，从仓库根目录执行：

```bash
pnpm --dir plugins/openclaw build
rsync -a --delete \
  plugins/openclaw/dist \
  plugins/openclaw/skills \
  plugins/openclaw/openclaw.plugin.json \
  plugins/openclaw/package.json \
  ~/.openclaw/extensions/miloco-openclaw-plugin/

uv pip install --python ~/.local/share/uv/tools/miloco/bin/python \
  -e backend/miot -e backend/miloco

~/.local/bin/supervisorctl -c ~/.openclaw/miloco/supervisord.conf restart miloco-backend
openclaw gateway restart
```

如果 `openclaw gateway restart` 提示配置校验失败，先运行：

```bash
openclaw config validate
openclaw doctor --fix
```

修复配置后再重启 gateway。

## 8. 端到端验证

1. 打开目标 OpenClaw 会话，确认它能正常回复消息。
2. 按门锁门铃。
3. 预期结果：
   - Miloco 日志出现匹配的 `did/siid/eiid`。
   - 目标 OpenClaw 会话收到门铃消息并回复文本。
   - OpenClaw 插件日志不出现 `[doorbell-reply-audio] ... failed`。
   - 门锁被唤醒并播放 OpenClaw 回复音频。

排查命令：

```bash
# Miloco 后端是否收到门铃事件
tail -n 300 ~/.openclaw/miloco/log/miloco-backend.log | rg 'doorbell|DOORBELL|Device event|<did>'

# OpenClaw 是否执行音频链路
tail -n 500 /tmp/openclaw/openclaw-$(date +%F).log | rg 'doorbell-reply-audio|audio_sender|edge-tts|ffmpeg|miloco-cli|<did>'

# 当前生效配置
miloco-cli config show | rg 'doorbell'
```

常见错误：

- `has_reply=False` 或没有 `responseText`：OpenClaw gateway 可能未重启加载新版 Miloco 插件。
- `ModuleNotFoundError: No module named 'edge_tts'`：音频脚本用错 Python，改成装有 `edge_tts` 的绝对路径。
- `audio command failed`：单独运行 `doorbell_reply_audio.sh '测试'`，先把脚本跑通。
- 没收到门铃事件：重新按日志确认 `doorbell_did`、`doorbell_siid`、`doorbell_eiid`。
- 有回复但无声音：确认 OpenClaw 日志里唤醒 action 和音频命令没有失败，确认门锁 IP/端口可达。

## 9. 代码入口

- MIoT 门铃事件订阅与过滤：`backend/miloco/src/miloco/miot/client.py`
- Miloco → OpenClaw webhook payload：`backend/miloco/src/miloco/utils/agent_client.py`
- OpenClaw 回复文本提取：`plugins/openclaw/src/hooks/trace.ts`
- OpenClaw 侧唤醒与播放：`plugins/openclaw/src/webhooks/agent.ts`
- 门锁默认配置模板：`backend/miloco/src/miloco/config/doorlock.yaml`
