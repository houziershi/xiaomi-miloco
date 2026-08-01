# 门锁门铃回复音频部署指南

本文档说明如何在一台新的本地设备上跑通这条链路：

```text
门锁门铃事件 → Miloco 后端 → 指定 OpenClaw 会话 → OpenClaw 回复文本
→ OpenClaw 插件先唤醒门锁 → OpenClaw 侧生成/转换音频 → audio_sender 发送到门锁播放
→ 播放成功回调 Miloco → Miloco 监听门外访客语音 → 转写继续进入同一 OpenClaw 会话
```

职责边界：Miloco 识别 MIoT 门铃事件、维护本次门铃会话状态、接收播放结果回调并转发访客语音；OpenClaw 负责拿到回复文本后执行唤醒、TTS、PCM 转码和门锁音频发送。

## 当前交互流程

当前验证通过的交互链路如下：

1. 访客按门锁门铃。
2. Miloco 通过 MIoT 设备级通配 topic 收到事件：`device/<doorbell_did>/up/event_occured/#`。
3. Miloco 在本地按 `doorbell_siid` / `doorbell_eiid` 过滤出门铃事件，创建一个门铃会话 `conversation_id`。
4. Miloco 把“门铃被按下：...”发送到配置的 `doorbell_session_key`。
5. OpenClaw 在该会话里回复文本，例如“你是谁？你有什么事”。
6. OpenClaw 插件先执行 `doorbell_wake_action_iid` 唤醒门锁，再执行 `doorbell_reply_audio_command` 播放回复音频。
7. OpenClaw 插件把播放结果回调给 Miloco；只有回调 `success=true` 后，Miloco 才进入访客拾音窗口。
8. 访客在门锁旁说话，例如“我是快递员，取件码是多少”。
9. 感知引擎识别出完整语音后，Miloco 只在来源 DID 命中以下任一项时接管为门铃会话语音：
   - `doorbell_did` 本身。
   - 自动发现的同名/数字后缀门锁摄像头 DID。
   - 显式配置的 `doorbell_speech_source_dids`。
10. Miloco 把访客语音转成 `门外访客说：我是快递员，取件码是多少`，继续发送到同一个 OpenClaw 会话。
11. OpenClaw 再次回复；OpenClaw 插件再次先唤醒门锁，再播放音频。
12. 每轮播放成功后继续进入下一轮访客拾音，直到达到 `doorbell_max_turns`、播放失败，或静默超时。
13. 如果访客一直不说话，Miloco 不再请求 OpenClaw 生成回复，而是直接播放 `doorbell_silence_fallback_text` 并结束本次会话。

关键日志链路：

- `doorbell event received ...`：收到并匹配到配置的门铃事件。
- `doorbell speech source dids resolved ...`：解析本次会话接受哪些语音来源 DID。
- `doorbell conversation handoff ...`：开始把门铃事件交给门铃会话状态机。
- `doorbell conversation started ... conversation_id ...`：门铃会话已创建。
- `doorbell agent turn starting/submitted ... trace_id ...`：已向 OpenClaw 发起一轮对话。
- `doorbell audio callback received ... success=true`：OpenClaw 插件已回调播放结果。
- `doorbell conversation listening ...`：Miloco 已开始等待访客语音。
- `doorbell early/final speech candidates ...`：感知引擎产出了可用于门铃会话判断的完整语音。
- `doorbell speech accepted ...`：访客语音已进入同一个门铃会话。
- `doorbell speech ignored ...`：访客语音未进入门铃会话，日志里会给出原因，例如未监听、超时、重复或来源 DID 不匹配。
- `doorbell conversation silence timeout ...`：访客静默超时，准备走固定兜底音频。

## 1. 前置条件

- OpenClaw gateway 可用，访问地址类似 `http://127.0.0.1:18789/`。
- Miloco 已安装并完成小米账号绑定，`miloco-cli device list` 能看到门锁设备。
- 门锁与运行 OpenClaw/Miloco 的本机处于可互通网络，`audio_sender` 能连到门锁音频服务端口。
- 本机已有可用 TTS 能力。当前验证路径使用 OpenClaw workspace 里的 `edge-tts` skill，再用 `ffmpeg` 转 16 kHz PCM。
- 本机已安装 `ffmpeg`、能运行 `miloco-cli`、能运行 `audio_sender`。
- 门锁设备已允许拾音：门锁/摄像头 did 需要在 `CAMERA_VOICE_ALLOW_LIST_KEY` 或 Web 端“语音”开关里启用。

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

注意：MIoT 服务端规则要求只能订阅设备级事件通配 topic：

```text
device/<did>/up/event_occured/#
```

不能直接订阅某个具体事件 leaf，例如 `device/<did>/up/event_occured/7/1006`。Miloco 会先订阅该设备全部事件，再在收到事件后按 `siid/eiid` 过滤门铃事件。

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
    "doorbell_speech_source_dids": ["<camera-did-that-records-visitor-speech>"],
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

双向对话相关配置：

- `doorbell_conversation_enabled`：是否启用门铃双向语音对话，默认 `true`。关闭后只做单次门铃消息和回复播放，不监听访客语音。
- `doorbell_speech_source_dids`：门铃会话额外接受的访客语音来源 DID 列表，默认 `[]`。当门铃事件 DID 和实际拾音摄像头 DID 不一致时填写，例如门铃事件来自锁 DID、语音识别来自同一门锁的第二路摄像头 DID。Miloco 仍会自动加入 `doorbell_did`，并尝试按同名/数字后缀自动发现同门锁摄像头；这个配置是显式兜底。
- `doorbell_visitor_listen_seconds`：每次门锁回复音频播放成功后，等待访客说话的窗口，默认 `15.0` 秒。
- `doorbell_max_turns`：单次门铃会话最多接收并转发的访客语音轮数，默认 `3`。
- `doorbell_visitor_message_prefix`：访客转写发给 OpenClaw 时的前缀，默认 `门外访客说：`。
- `doorbell_silence_fallback_enabled`：访客监听窗口内无人说话时，是否直接播放固定兜底语音并结束会话，默认 `true`。
- `doorbell_silence_fallback_text`：静默超时后播放到门锁的固定文案，默认 `我没有听到您的声音，请稍后再按门铃。`。

示例完整配置：

```json
{
  "miot": {
    "doorbell_did": "<did>",
    "doorbell_siid": 7,
    "doorbell_eiid": 1006,
    "doorbell_session_key": "agent:main:dashboard:xxxx",
    "doorbell_wake_action_iid": "action.17.3",
    "doorbell_speech_source_dids": ["1179480261"],
    "doorbell_reply_audio_command": [
      "/Users/<you>/.openclaw/miloco/scripts/doorbell_reply_audio.sh",
      "{text}"
    ],
    "doorbell_conversation_enabled": true,
    "doorbell_visitor_listen_seconds": 15.0,
    "doorbell_max_turns": 3,
    "doorbell_visitor_message_prefix": "门外访客说：",
    "doorbell_silence_fallback_enabled": true,
    "doorbell_silence_fallback_text": "我没有听到您的声音，请稍后再按门铃。"
  }
}
```

会话规则：

- 门铃事件会附加 OpenClaw 系统提示，让回复尽量短、适合直接播放。
- OpenClaw 回复文本播放成功后，Miloco 才进入访客拾音窗口。
- 如果唤醒门锁或播放音频失败，OpenClaw 会回调失败，Miloco 结束本次门铃会话，不再监听访客语音。
- 访客语音必须满足 `is_complete=true` 且来源 DID 匹配门锁 DID、自动发现的同名摄像头 DID 或 `doorbell_speech_source_dids`，才会转发为 `门外访客说：...`。
- 每次访客语音进入同一 OpenClaw 会话后，OpenClaw 的新回复会再次唤醒门锁并播放。
- 如果监听窗口内没有访客语音，Miloco 不再调用 OpenClaw 生成回复，而是直接请求 OpenClaw 插件播放 `doorbell_silence_fallback_text`，播放后结束会话。
- 有识别文本但没进门铃会话时，查看 `doorbell conversation started ... speech_source_dids=...`、`doorbell speech accepted`、`doorbell speech ignored` 日志；如果日志提示 `source did mismatch`，把实际语音来源 DID 加到 `doorbell_speech_source_dids`。

## 7. 安装/重启服务

开发分支本地验证时，从仓库根目录执行。

1. 构建并安装 OpenClaw 插件：

```bash
pnpm --dir plugins/openclaw build
rsync -a --delete \
  plugins/openclaw/dist \
  plugins/openclaw/skills \
  plugins/openclaw/openclaw.plugin.json \
  plugins/openclaw/package.json \
  ~/.openclaw/extensions/miloco-openclaw-plugin/
```

2. 安装 Miloco 后端代码到当前 `miloco-cli` 使用的 Python 环境：

```bash
uv pip install --python ~/.local/share/uv/tools/miloco/bin/python \
  -e backend/miot -e backend/miloco
```

3. 重启 Miloco 后端和 OpenClaw gateway：

```bash
miloco-cli service restart
openclaw gateway restart
```

如果 `openclaw gateway restart` 提示配置校验失败，先运行：

```bash
openclaw config validate
openclaw doctor --fix
```

修复配置后再重启 gateway。

4. 确认门铃事件订阅成功：

```bash
tail -n 200 ~/.openclaw/miloco/log/miloco-backend.log \
  | rg 'mips_cloud subscribed device events|doorbell event subscription synced|subscribe doorbell event failed'
```

成功时应该看到：

```text
mips_cloud subscribed device events topic=device/<doorbell_did>/up/event_occured/#
doorbell event subscription synced: did=<doorbell_did> siid=<doorbell_siid> eiid=<doorbell_eiid>
```

如果看到 `subscribe doorbell event failed ... Not authorized`，通常是 MIoT MQTT 连接刚建立后的临时授权状态；先再执行一次 `miloco-cli service restart`，然后重新查看上述订阅日志。不要在没有订阅成功日志时做端到端测试，否则门铃事件不会进入会话。

5. 确认运行配置已生效：

```bash
miloco-cli config show | rg 'doorbell_(did|siid|eiid|session_key|wake_action_iid|speech_source_dids|visitor_listen_seconds|silence_fallback)'
```

重点确认：

- `doorbell_did` 是产生门铃事件的门锁 DID。
- `doorbell_siid` / `doorbell_eiid` 与日志中的门铃事件三元组一致。
- `doorbell_session_key` 是目标 OpenClaw 会话。
- `doorbell_speech_source_dids` 包含实际识别访客语音的摄像头 DID；如果不确定，先测试一次，再从 `realtime_perceive` 里的 `source_device_ids` 反查。
- `doorbell_reply_audio_command` 指向可执行脚本，且脚本单独运行能播放到门锁。

## 8. 端到端验证

1. 打开目标 OpenClaw 会话，确认它能正常回复消息。
2. 按门锁门铃。
3. 预期结果：
   - Miloco 日志出现匹配的 `did/siid/eiid`。
   - 目标 OpenClaw 会话收到门铃消息并回复文本。
   - OpenClaw 插件日志不出现 `[doorbell-reply-audio] ... failed`。
   - 门锁被唤醒并播放 OpenClaw 回复音频。
   - 回复播放成功后，在门锁旁边说一句话，例如“我是快递员，我来取快递。”。
   - 目标 OpenClaw 会话继续收到 `门外访客说：我是快递员，我来取快递。`，并再次回复、再次播放到门锁。

建议按三轮分开验证：

1. **门铃事件链路**：只按门铃，不说话。确认目标会话收到门铃消息，门锁播放 OpenClaw 首轮回复；静默超时后播放 `doorbell_silence_fallback_text`。
2. **访客语音链路**：按门铃，听完首轮回复后再说一句完整语音。确认目标会话出现 `门外访客说：...`，且门锁播放 OpenClaw 二轮回复。
3. **来源 DID 链路**：如果语音没有进入门铃会话，先不要改 OpenClaw，查看 Miloco 日志中的 `doorbell speech ignored`。如果原因是 `source did mismatch`，把日志里的实际 `source_dids` 写入 `doorbell_speech_source_dids`，重启后再测。

排查命令：

```bash
# Miloco 后端是否收到门铃事件、创建会话、进入监听、接收访客语音
tail -n 500 ~/.openclaw/miloco/log/miloco-backend.log \
  | rg 'doorbell event received|doorbell speech source dids resolved|doorbell conversation handoff|doorbell conversation started|doorbell audio callback received|doorbell conversation listening|doorbell .*speech .*candidates|doorbell speech accepted|doorbell speech ignored|doorbell conversation silence timeout|<did>'

# OpenClaw 是否执行音频链路
tail -n 500 /tmp/openclaw/openclaw-$(date +%F).log | rg 'doorbell-reply-audio|audio_sender|edge-tts|ffmpeg|miloco-cli|<did>'

# 当前生效配置
miloco-cli config show | rg 'doorbell'

# 门锁拾音是否开启
miloco-cli scope camera list | rg '<did>|voice_in_use'
```

常见错误：

- `has_reply=False` 或没有 `responseText`：OpenClaw gateway 可能未重启加载新版 Miloco 插件。
- `ModuleNotFoundError: No module named 'edge_tts'`：音频脚本用错 Python，改成装有 `edge_tts` 的绝对路径。
- `audio command failed`：单独运行 `doorbell_reply_audio.sh '测试'`，先把脚本跑通。
- 没收到门铃事件：先确认有 `mips_cloud subscribed device events topic=device/<did>/up/event_occured/#`；再按日志确认 `doorbell_did`、`doorbell_siid`、`doorbell_eiid`。
- 有回复但无声音：确认 OpenClaw 日志里唤醒 action 和音频命令没有失败，确认门锁 IP/端口可达。
- 有第一次播放但访客说话没进会话：确认播放成功回调日志、门锁拾音开关、`doorbell_visitor_listen_seconds` 窗口、语音识别结果是否 `is_complete=true`，以及 `doorbell_speech_source_dids` 是否覆盖实际 `source_device_ids`。
- 访客不说话没有兜底音：确认 `doorbell_silence_fallback_enabled=true`，以及 OpenClaw 日志中 `doorbell_reply_audio` action 没有播放失败。

## 9. 代码入口

- MIoT 门铃事件订阅与过滤：`backend/miloco/src/miloco/miot/client.py`
- 门铃双向会话状态机：`backend/miloco/src/miloco/doorbell/conversation.py`
- 播放成功/失败回调接口：`backend/miloco/src/miloco/miot/router.py`
- 访客语音转发入口：`backend/miloco/src/miloco/perception/client.py`
- Miloco → OpenClaw webhook payload：`backend/miloco/src/miloco/utils/agent_client.py`
- OpenClaw 回复文本提取：`plugins/openclaw/src/hooks/trace.ts`
- OpenClaw 侧唤醒与播放：`plugins/openclaw/src/webhooks/agent.ts`
- 门锁默认配置模板：`backend/miloco/src/miloco/config/doorlock.yaml`
