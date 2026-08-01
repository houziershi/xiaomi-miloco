# 门铃会话专用 ASR 方案备忘

## 背景

当前门铃双向对话链路中，访客语音转文本依赖 Miloco 感知引擎的 omni 多模态模型输出 `speeches`。该链路可以同时处理视觉、环境音和语音，但在门锁门铃场景中，如果访客语音较短、门锁提示音干扰、窗口切分不理想，可能出现 VAD 判断有明显人声活动但 `speeches=[]` 的情况。

已验证案例：访客说“我是快递员，我来取快递。”时，感知窗口内 `_gate_speech_prob_*` 和 `_gate_audio_energy_*` 都较高，但 omni 没有输出完整语音转写，门铃会话最终进入静默兜底。

## 可用性结论

已用本机 MiMo API 配置测试 `mimo-v2.5-asr`：

- `mimo-v2.5-asr` 服务可达。
- 文本-only 请求会失败，错误为 ASR 模型要求 exactly one `input_audio`。
- 当前 omni 音频块使用 `audio/m4a`，`mimo-v2.5-asr` 不接受该 MIME。
- `mimo-v2.5-asr` 接受 `audio/wav`、`audio/mpeg`、`audio/mp3`。
- 使用 WAV 测试音频“我是快递员，我来取快递。”请求成功，返回文本为“我是快递员，我来取快递。”。

## 不建议直接替换全局 omni 模型

不要直接把 `model.omni.model` 从 `mimo-v2.5` 改成 `mimo-v2.5-asr`，原因：

- `mimo-v2.5-asr` 是 ASR-only 模型，不适合承担视觉 caption、环境音、规则匹配和 suggestions。
- 当前感知引擎期望 omni 返回结构化 JSON，包括 `caption`、`speeches`、`env_sounds`、`matched_rules`、`suggestions`。
- ASR 模型返回纯转写文本，不能直接兼容现有 omni 输出 schema。
- ASR 模型要求输入中必须有且仅有一个 `input_audio`，不适合作为普通多模态模型使用。

## 推荐架构

后续如果要接入，应做成“门铃会话专用 ASR 通道”，只服务门铃监听窗口：

1. 门铃事件创建 `conversation_id`。
2. OpenClaw 首轮回复播放成功，Miloco 进入访客监听窗口。
3. 感知窗口内如果门铃语音来源 DID 有明显 VAD/音频活动，提取该窗口音频。
4. 将音频转为 `wav` 或 `mp3`。
5. 调用 `mimo-v2.5-asr` 获取纯文本转写。
6. 将转写包装为门铃访客语音：`门外访客说：<asr_text>`。
7. 优先送入当前门铃会话状态机。
8. 原 omni 感知链路继续保留，用于视觉、环境音、普通语音提醒和规则判断。

## 触发策略建议

ASR 通道不必每个窗口都调用，建议只在以下条件同时满足时触发：

- 当前存在状态为 `listening` 的门铃会话。
- 感知窗口的 `source_device_ids` 命中当前会话接受的门铃语音来源 DID。
- `_gate_speech_prob_<did>` 达到阈值，或 `_gate_audio_energy_<did>` 达到阈值。
- omni 当前窗口没有产出可接管的完整 `speeches`。
- 当前窗口尚未被同一 `conversation_id` 调用过 ASR，避免重复转写。

## 配置项建议

后续可新增独立配置项，避免影响普通感知：

```json
{
  "miot": {
    "doorbell_asr_enabled": false,
    "doorbell_asr_model": "mimo-v2.5-asr",
    "doorbell_asr_audio_format": "wav",
    "doorbell_asr_min_speech_probability": 0.5,
    "doorbell_asr_min_energy": 0.1,
    "doorbell_asr_timeout_seconds": 10.0
  }
}
```

含义：

- `doorbell_asr_enabled`：是否启用门铃专用 ASR 通道，默认关闭。
- `doorbell_asr_model`：ASR 模型名，默认 `mimo-v2.5-asr`。
- `doorbell_asr_audio_format`：发给 ASR 的音频格式，建议 `wav` 或 `mp3`。
- `doorbell_asr_min_speech_probability`：触发 ASR 的人声概率阈值。
- `doorbell_asr_min_energy`：触发 ASR 的音频能量阈值。
- `doorbell_asr_timeout_seconds`：单次 ASR 请求超时时间。

## 需要注意的问题

- 音频窗口切分：访客一句话可能跨多个 4 秒窗口，需要考虑是否合并最近 N 秒音频。
- 去重：同一句访客语音可能被 omni 和 ASR 同时识别，需要按 `conversation_id + text + source_did` 去重。
- 时序：ASR 返回可能晚于静默兜底，需要在检测到音频活动时延长监听窗口。
- 格式：现有 omni 音频路径是 `m4a`，ASR 路径需要单独转 `wav/mp3`。
- 成本：只在门铃会话监听期且有音频活动时调用，避免普通感知持续调用 ASR。
- 日志：必须记录 `conversation_id`、`source_did`、窗口时间、VAD/energy、ASR 模型、耗时、结果文本和是否接管门铃会话。

## 当前状态

暂不实现代码。当前仅保存方案，后续如果门铃访客语音仍频繁被 omni 漏识别，再按上述架构接入门铃专用 ASR。
