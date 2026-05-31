"""
astrbot_plugin_no_reply
不回消息插件 - 用便宜的小LLM根据上下文判断是否应该继续回复某个用户。
判断为不回时直接 stop_event()，主LLM不被调用，省token。
同时把"我为什么没回这条"作为一条内部记录追加到当前会话历史，
下次主LLM被唤醒时能从上下文里看见自己上次为什么沉默。

作者: 紫电幽冥
"""

import asyncio
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.provider import ProviderRequest, Provider
from astrbot.api import logger, AstrBotConfig


@dataclass
class UserState:
    consecutive_no_reply: int = 0
    silent_until: float = 0.0
    last_reason: str = ""
    last_judge_at: float = 0.0
    total_blocked_count: int = 0


def _parse_json_decision(text: str) -> Optional[dict]:
    if not text:
        return None
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"\{[^{}]*\"reply\"[^{}]*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    reply_m = re.search(r'"?reply"?\s*[:：]\s*(true|false)', text, re.IGNORECASE)
    reason_m = re.search(r'"?reason"?\s*[:：]\s*"([^"]+)"', text)
    if reply_m:
        return {
            "reply": reply_m.group(1).lower() == "true",
            "reason": reason_m.group(1) if reason_m else "",
            "confidence": 0.5,
        }
    return None


@register(
    "astrbot_plugin_no_reply",
    "紫电幽冥",
    "用便宜的小LLM判断是否需要回复，对付AI机器人骚扰、避免token蒸发",
    "0.2.0",
)
class NoReplyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.user_states: Dict[str, UserState] = {}
        self._lock = asyncio.Lock()
        logger.debug(f"[no_reply] 插件已加载, enabled={self.config.get('enabled')}")

    # ---------- 工具方法 ----------

    def _user_key(self, event: AstrMessageEvent) -> str:
        return f"{event.get_platform_name()}:{event.get_sender_id()}"

    def _get_state(self, key: str) -> UserState:
        st = self.user_states.get(key)
        if st is None:
            st = UserState()
            self.user_states[key] = st
        return st

    def _is_in_scope(self, event: AstrMessageEvent) -> bool:
        scope = self.config.get("scope", "private")
        is_private = event.is_private_chat()
        if scope == "private":
            return is_private
        if scope == "group":
            return not is_private
        return True

    def _is_whitelisted(self, sender_id: str) -> bool:
        wl_raw = str(self.config.get("whitelist_user_ids", "") or "").strip()
        if not wl_raw:
            return False
        wl = {x.strip() for x in wl_raw.split(",") if x.strip()}
        return str(sender_id) in wl

    def _get_judge_provider(self, event: AstrMessageEvent) -> Optional[Provider]:
        pid = str(self.config.get("judge_provider_id", "") or "").strip()
        if pid:
            p = self.context.get_provider_by_id(pid)
            if isinstance(p, Provider):
                return p
            logger.warning(f"[no_reply] 找不到 judge_provider_id={pid}, 回退默认")
        try:
            p = self.context.get_using_provider(event.unified_msg_origin)
            if isinstance(p, Provider):
                return p
        except Exception:
            pass
        return None

    async def _build_history_text(self, event: AstrMessageEvent, current_text: str) -> str:
        limit = int(self.config.get("history_limit", 10))
        lines: List[str] = []
        try:
            cm = self.context.conversation_manager
            curr_cid = await cm.get_curr_conversation_id(event.unified_msg_origin)
            if curr_cid:
                conv = await cm.get_conversation(event.unified_msg_origin, curr_cid)
                hist = []
                if conv and getattr(conv, "history", None):
                    raw = conv.history
                    if isinstance(raw, str):
                        try:
                            hist = json.loads(raw)
                        except Exception:
                            hist = []
                    elif isinstance(raw, list):
                        hist = raw
                for item in hist[-limit:]:
                    if not isinstance(item, dict):
                        continue
                    role = item.get("role", "?")
                    content = item.get("content", "")
                    if isinstance(content, list):
                        text_parts = []
                        for c in content:
                            if isinstance(c, dict) and c.get("type") == "text":
                                text_parts.append(c.get("text", ""))
                        content = " ".join(text_parts)
                    content = str(content).strip().replace("\n", " ")
                    if len(content) > 200:
                        content = content[:200] + "..."
                    label = {"user": "用户", "assistant": "助手", "system": "系统"}.get(role, role)
                    lines.append(f"[{label}] {content}")
        except Exception as e:
            logger.debug(f"[no_reply] 读取历史失败: {e}")

        sender_name = event.get_sender_name() or event.get_sender_id()
        lines.append(f"[最新-{sender_name}] {current_text}")
        return "\n".join(lines)

    async def _ask_judge(
        self, event: AstrMessageEvent, current_text: str
    ) -> Tuple[bool, str, float, str]:
        provider = self._get_judge_provider(event)
        if provider is None:
            return True, "未配置可用判断LLM, 默认放行", 0.0, ""

        sys_prompt = self.config.get("judge_system_prompt", "") or ""
        history_text = await self._build_history_text(event, current_text)
        prompt = (
            "以下是最近的聊天记录:\n```\n"
            + history_text
            + "\n```\n\n请根据上面的对话判断主助手是否应该回复『最新』那条消息。严格按要求输出JSON。"
        )

        timeout = int(self.config.get("judge_timeout", 15))
        try:
            resp = await asyncio.wait_for(
                provider.text_chat(
                    prompt=prompt,
                    system_prompt=sys_prompt,
                    session_id=uuid.uuid4().hex,
                    contexts=[],
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            return True, "判断LLM超时, 默认放行", 0.0, ""
        except Exception as e:
            logger.warning(f"[no_reply] 调用判断LLM出错: {e}")
            return True, f"判断LLM异常({e}), 默认放行", 0.0, ""

        raw = (getattr(resp, "completion_text", "") or "").strip()
        decision = _parse_json_decision(raw)
        if not decision:
            return True, f"无法解析判断LLM输出, 默认放行: {raw[:80]}", 0.0, raw
        should_reply = bool(decision.get("reply", True))
        reason = str(decision.get("reason", "")).strip()
        try:
            conf = float(decision.get("confidence", 0.5))
        except Exception:
            conf = 0.5
        return should_reply, reason, conf, raw

    async def _record_decision_to_history(
        self,
        event: AstrMessageEvent,
        reason: str,
        confidence: float,
        action: str,
        current_text: str,
    ):
        """
        把"我为什么没回这条消息"以 assistant 角色追加到当前会话历史。
        下次主 LLM 启动时，会自然地从上下文里看到这段，知道自己上次为什么沉默。
        """
        try:
            cm = self.context.conversation_manager
            umo = event.unified_msg_origin
            cid = await cm.get_curr_conversation_id(umo)
            if not cid:
                return
            conv = await cm.get_conversation(umo, cid)
            if not conv:
                return

            raw_hist = getattr(conv, "history", None)
            history: list = []
            if isinstance(raw_hist, list):
                history = list(raw_hist)
            elif isinstance(raw_hist, str):
                try:
                    parsed = json.loads(raw_hist or "[]")
                    if isinstance(parsed, list):
                        history = parsed
                except Exception:
                    history = []

            sender_name = event.get_sender_name() or event.get_sender_id()
            preview = current_text.replace("\n", " ")
            if len(preview) > 120:
                preview = preview[:120] + "..."

            ts = time.strftime("%Y-%m-%d %H:%M", time.localtime())
            note_lines = [
                "[no_reply 内部决策记录 / 我自己的备忘]",
                f"时间: {ts}",
                f"我刚才没有回复 {sender_name}({event.get_sender_id()}) 的一条消息。",
                f"  对方原文: {preview}",
                f"  动作: {action}",
                f"  理由: {reason} (置信度 {confidence:.2f})",
                "如果之后这个人再来, 或主人问起这次沉默, 我应该记得这是我主动选择不回的, 不是宕机或没收到。",
            ]
            note = "\n".join(note_lines)

            history.append({"role": "assistant", "content": note})

            try:
                await cm.update_conversation(umo, cid, history=history)
            except TypeError:
                await cm.update_conversation(umo, cid, history)
        except Exception as e:
            logger.warning(f"[no_reply] 写入决策到对话历史失败: {e}")

    # ---------- 钩子 ----------

    @filter.on_llm_request(priority=200)
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        if not self.config.get("enabled", True):
            return
        if not self._is_in_scope(event):
            return

        sender_id = str(event.get_sender_id() or "")
        if not sender_id:
            return
        if self._is_whitelisted(sender_id):
            return

        current_text = (event.message_str or "").strip()
        if not current_text:
            return

        key = self._user_key(event)
        async with self._lock:
            state = self._get_state(key)

            now = time.time()
            if state.silent_until and now < state.silent_until:
                state.total_blocked_count += 1
                remain = int(state.silent_until - now)
                logger.debug(
                    f"[no_reply] 用户{sender_id}处于静默期, 剩余{remain}s, 直接拦截"
                )
                action = f"静默期内拦截(剩余{remain}s, 累计{state.total_blocked_count}次)"
                # 跳出锁去写历史
                _reason_silent = state.last_reason or "上次判定为不应回复, 处于静默期"

        # 静默期分支：写历史 + 拦截
        if state.silent_until and time.time() < state.silent_until:
            await self._record_decision_to_history(
                event,
                reason=_reason_silent,
                confidence=1.0,
                action=action,
                current_text=current_text,
            )
            event.stop_event()
            return

        # 真正调用小LLM做判断
        should_reply, reason, confidence, raw = await self._ask_judge(event, current_text)

        async with self._lock:
            state = self._get_state(key)
            state.last_judge_at = time.time()
            state.last_reason = reason or state.last_reason

            if should_reply:
                if state.consecutive_no_reply > 0:
                    logger.debug(f"[no_reply] 用户{sender_id}恢复正常, 清零连续不回计数")
                state.consecutive_no_reply = 0
                state.silent_until = 0.0
                logger.debug(
                    f"[no_reply] 放行: {sender_id} | {reason} | conf={confidence:.2f}"
                )
                return

            state.consecutive_no_reply += 1
            threshold = int(self.config.get("block_threshold", 2))
            silent_minutes = int(self.config.get("silent_minutes", 60))

            if state.consecutive_no_reply >= threshold:
                if silent_minutes > 0:
                    state.silent_until = time.time() + silent_minutes * 60
                    action = f"进入静默{silent_minutes}分钟(连续{state.consecutive_no_reply}次判定不回)"
                else:
                    state.silent_until = time.time() + 365 * 24 * 3600
                    action = f"进入永久静默(连续{state.consecutive_no_reply}次判定不回, 需 /no_reply unblock 解除)"
                state.total_blocked_count += 1
            else:
                action = f"暂不回复(累计{state.consecutive_no_reply}/{threshold}, 未触发静默)"
                state.total_blocked_count += 1

        await self._record_decision_to_history(
            event,
            reason=reason,
            confidence=confidence,
            action=action,
            current_text=current_text,
        )
        event.stop_event()

    # ---------- 指令 ----------

    @filter.command_group("no_reply")
    def no_reply_group(self):
        """不回消息插件管理"""
        pass

    @no_reply_group.command("status")
    async def cmd_status(self, event: AstrMessageEvent):
        if not self.user_states:
            yield event.plain_result("[no_reply] 当前没有任何用户记录")
            return
        lines = ["[no_reply] 用户状态:"]
        now = time.time()
        for key, st in list(self.user_states.items())[:30]:
            silent = "-"
            if st.silent_until and st.silent_until > now:
                silent = f"{int((st.silent_until - now)/60)}min"
            lines.append(
                f"- {key} | 连续不回={st.consecutive_no_reply} | 静默剩余={silent} | 累计拦截={st.total_blocked_count} | 理由: {st.last_reason[:40]}"
            )
        yield event.plain_result("\n".join(lines))

    @no_reply_group.command("unblock")
    async def cmd_unblock(self, event: AstrMessageEvent, target: str = ""):
        if target:
            removed = 0
            for key in list(self.user_states.keys()):
                if key.endswith(f":{target}"):
                    self.user_states[key].silent_until = 0
                    self.user_states[key].consecutive_no_reply = 0
                    removed += 1
            yield event.plain_result(f"[no_reply] 已解除 {removed} 条记录的静默 (target={target})")
        else:
            for st in self.user_states.values():
                st.silent_until = 0
                st.consecutive_no_reply = 0
            yield event.plain_result(f"[no_reply] 已解除所有用户的静默 ({len(self.user_states)} 条)")

    @no_reply_group.command("block")
    async def cmd_block(self, event: AstrMessageEvent, target: str, minutes: int = 60):
        platform = event.get_platform_name()
        key = f"{platform}:{target}"
        st = self._get_state(key)
        st.silent_until = time.time() + max(1, minutes) * 60
        st.last_reason = f"主人手动拉黑 {minutes} 分钟"
        st.consecutive_no_reply = max(
            st.consecutive_no_reply, int(self.config.get("block_threshold", 2))
        )
        yield event.plain_result(f"[no_reply] 已对 {key} 静默 {minutes} 分钟")

    @no_reply_group.command("clear")
    async def cmd_clear(self, event: AstrMessageEvent):
        n = len(self.user_states)
        self.user_states.clear()
        yield event.plain_result(f"[no_reply] 已清空 {n} 条用户状态")

    async def terminate(self):
        self.user_states.clear()
        logger.debug("[no_reply] 插件已卸载")
