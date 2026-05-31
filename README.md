# astrbot_plugin_no_reply

不回消息插件 —— 用一个便宜的小LLM根据上下文判断是否应该继续回复某个用户。

## 解决什么问题

- 有人用AI机器人加你，主LLM跟它打太极一来一回，token蒸发。
- 有人灌水、复读、引诱长输出。
- 想让主助手只对真人/有意义的对话花钱。

## 工作原理

每次主LLM被调用前，本插件先用一个**便宜的小LLM**（在配置里指定）看一眼最近的聊天记录，输出 JSON 决策：

```json
{"reply": false, "reason": "对方为AI机器人, 套娃式提问消耗token", "confidence": 0.9}
```

- 决定不回 -> `event.stop_event()`，主LLM根本不会被调用
- 同一个用户连续 N 次被判不回 -> 进入静默期，期间所有消息都直接拦截，连小LLM都不调用
- 决策的推理过程会通过 `Context.send_message` 转发给主人私聊

## 配置项

| 配置 | 说明 |
| --- | --- |
| `judge_provider_id` | 用于判断的小LLM provider ID（强烈建议指定便宜的） |
| `scope` | 生效范围：private / group / both |
| `history_limit` | 判断时参考最近多少条历史消息 |
| `block_threshold` | 连续判定不回多少次后进入静默 |
| `silent_minutes` | 静默时长，0=永久 |
| `whitelist_user_ids` | 白名单用户ID（默认包含主人） |
| `forward_target_id` | 把决策报告转发给谁的私聊 |
| `forward_platform_id` | 转发用的平台 ID |
| `judge_timeout` | 判断LLM调用超时秒数 |
| `judge_system_prompt` | 自定义判断LLM的system prompt |

## 指令

- `/no_reply status` 查看当前所有用户判断状态
- `/no_reply unblock <user_id>` 解除某用户静默（不传则解除所有）
- `/no_reply block <user_id> [分钟]` 手动让某用户静默
- `/no_reply clear` 清空所有用户状态

## 注意

- 默认 `scope=private`，只对私聊生效（推荐，对付AI加好友）
- 默认白名单包含主人 `85854548`，主人永远不会被拦
- 判断LLM调用失败时**默认放行**，避免误伤
