from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from src.A_memorix.host_service import a_memorix_host_service
from src.common.logger import get_logger


logger = get_logger("memory_service")


@dataclass
class MemoryHit:
    content: str
    score: float = 0.0
    hit_type: str = ""
    source: str = ""
    hash_value: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    episode_id: str = ""
    title: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "content": self.content,
            "score": self.score,
            "type": self.hit_type,
            "source": self.source,
            "hash": self.hash_value,
            "metadata": self.metadata,
            "episode_id": self.episode_id,
            "title": self.title,
        }


@dataclass
class MemorySearchResult:
    summary: str = ""
    hits: List[MemoryHit] = field(default_factory=list)
    filtered: bool = False
    success: bool = True
    error: str = ""

    def to_text(self, limit: int = 5, *, truncate_content: bool = True, max_content_chars: int = 160) -> str:
        if not self.hits:
            return ""
        lines = []
        for index, item in enumerate(self.hits[: max(1, int(limit))], start=1):
            content = item.content.strip().replace("\n", " ")
            if truncate_content and len(content) > max_content_chars:
                content = content[:max_content_chars] + "..."
            origin = str(item.metadata.get('origin_label') or '来源未标注')
            lines.append(f"{index}. [{origin}] {content}")
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "error": self.error,
            "summary": self.summary,
            "hits": [item.to_dict() for item in self.hits],
            "filtered": self.filtered,
        }


@dataclass
class MemoryWriteResult:
    success: bool
    stored_ids: List[str] = field(default_factory=list)
    skipped_ids: List[str] = field(default_factory=list)
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "stored_ids": self.stored_ids,
            "skipped_ids": self.skipped_ids,
            "detail": self.detail,
        }


@dataclass
class PersonProfileResult:
    summary: str = ""
    traits: List[str] = field(default_factory=list)
    evidence: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"summary": self.summary, "traits": self.traits, "evidence": self.evidence}


class MemoryService:
    async def _invoke(
        self,
        component_name: str,
        args: Optional[Dict[str, Any]] = None,
        *,
        timeout_ms: Optional[int] = None,
    ) -> Any:
        if timeout_ms is None:
            response = await a_memorix_host_service.invoke(component_name, args or {})
        else:
            response = await a_memorix_host_service.invoke(component_name, args or {}, timeout_ms=timeout_ms)
        if isinstance(response, dict):
            return response
        payload = getattr(response, "payload", None)
        if isinstance(payload, dict):
            if isinstance(payload.get("result"), dict):
                return payload["result"]
            return payload
        model_dump = getattr(response, "model_dump", None)
        if callable(model_dump):
            dumped = model_dump()
            if isinstance(dumped, dict):
                inner_payload = dumped.get("payload")
                if isinstance(inner_payload, dict):
                    if isinstance(inner_payload.get("result"), dict):
                        return inner_payload["result"]
                    return inner_payload
        return response

    async def _invoke_admin(
        self,
        component_name: str,
        *,
        action: str,
        timeout_ms: Optional[int] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        if timeout_ms is None:
            payload = await self._invoke(component_name, {"action": action, **kwargs})
        else:
            payload = await self._invoke(component_name, {"action": action, **kwargs}, timeout_ms=timeout_ms)
        return payload if isinstance(payload, dict) else {"success": False, "error": "invalid_payload"}

    @staticmethod
    def _coerce_write_result(payload: Any) -> MemoryWriteResult:
        if not isinstance(payload, dict):
            return MemoryWriteResult(success=False, detail="invalid_payload")
        stored_ids = [str(item) for item in (payload.get("stored_ids") or []) if str(item).strip()]
        skipped_ids = [str(item) for item in (payload.get("skipped_ids") or []) if str(item).strip()]
        detail = str(payload.get("detail") or payload.get("reason") or "")
        if stored_ids or skipped_ids:
            success = True
        elif "success" in payload:
            success = bool(payload.get("success"))
        else:
            success = not bool(detail)
        return MemoryWriteResult(
            success=success,
            stored_ids=stored_ids,
            skipped_ids=skipped_ids,
            detail=detail,
        )

    @staticmethod
    def _coerce_search_result(payload: Any) -> MemorySearchResult:
        if not isinstance(payload, dict):
            return MemorySearchResult(success=False, error="invalid_payload")
        hits: List[MemoryHit] = []
        for item in payload.get("hits", []) or []:
            if not isinstance(item, dict):
                continue
            metadata = item.get("metadata", {}) or {}
            if not isinstance(metadata, dict):
                metadata = {}
            if "source_branches" in item and "source_branches" not in metadata:
                metadata["source_branches"] = item.get("source_branches") or []
            if "rank" in item and "rank" not in metadata:
                metadata["rank"] = item.get("rank")
            hits.append(
                MemoryHit(
                    content=str(item.get("content", "") or ""),
                    score=float(item.get("score", 0.0) or 0.0),
                    hit_type=str(item.get("type", "") or ""),
                    source=str(item.get("source", "") or ""),
                    hash_value=str(item.get("hash", "") or ""),
                    metadata=metadata,
                    episode_id=str(item.get("episode_id", "") or ""),
                    title=str(item.get("title", "") or ""),
                )
            )
        success_raw = payload.get("success")
        error = str(payload.get("error", "") or "")
        success = (not bool(error)) if success_raw is None else bool(success_raw)
        return MemorySearchResult(
            summary=str(payload.get("summary", "") or ""),
            hits=hits,
            filtered=bool(payload.get("filtered", False)),
            success=success,
            error=error,
        )

    @staticmethod
    def _coerce_profile_result(payload: Any) -> PersonProfileResult:
        if not isinstance(payload, dict):
            return PersonProfileResult()
        return PersonProfileResult(
            summary=str(payload.get("summary", "") or ""),
            traits=[str(item) for item in (payload.get("traits") or []) if str(item).strip()],
            evidence=[item for item in (payload.get("evidence") or []) if isinstance(item, dict)],
        )

    async def search(
        self,
        query: str,
        *,
        limit: int = 5,
        mode: str = "search",
        chat_id: str = "",
        person_id: str = "",
        time_start: str | float | None = None,
        time_end: str | float | None = None,
        respect_filter: bool = True,
        user_id: str = "",
        group_id: str = "",
        include_shared: bool = True,
    ) -> MemorySearchResult:
        clean_query = str(query or "").strip()
        normalized_time_start = None if time_start in {None, ""} else time_start
        normalized_time_end = None if time_end in {None, ""} else time_end
        if not clean_query and normalized_time_start is None and normalized_time_end is None:
            return MemorySearchResult()
        try:
            from utils.account_identity import linked_person_ids
            payloads = []
            for alias in linked_person_ids(person_id):
                try:
                    payloads.append(await self._invoke(
                    "search_memory",
                    {
                    "query": clean_query,
                    "limit": max(1, int(limit)),
                    "mode": mode,
                    "chat_id": chat_id,
                    "person_id": alias,
                    "time_start": normalized_time_start,
                    "time_end": normalized_time_end,
                    "respect_filter": bool(respect_filter),
                    "user_id": str(user_id or "").strip(),
                    "group_id": str(group_id or "").strip(),
                    "include_shared": bool(include_shared),
                    },
                    ))
                except Exception as exc:
                    payloads.append({'success':False,'error':str(exc)})
            results = [self._coerce_search_result(p) for p in payloads]
            result = results[0]
            if len(results)>1:
                hits={}
                for part in results:
                    for hit in part.hits:
                        key=hit.hash_value or (hit.hit_type,hit.content,hit.source)
                        if key not in hits or hit.score>hits[key].score:
                            hits[key]=hit
                result.hits=sorted(hits.values(),key=lambda h:h.score,reverse=True)[:max(1,int(limit))]
                result.success=any(r.success for r in results)
                result.filtered=all(r.filtered for r in results)
                if result.success:
                    result.error=''
            self._annotate_sources(result)
            return result
        except Exception as exc:
            logger.warning(f"长期记忆搜索失败: {exc}")
            return MemorySearchResult(success=False, error=str(exc))

    @staticmethod
    def _annotate_sources(result):
        from src.chat.message_receive.chat_manager import chat_manager
        for hit in result.hits:
            ids=set()
            for key in ('chat_id','session_id','stream_id','chat_ids','session_ids','stream_ids'):
                value=hit.metadata.get(key)
                if isinstance(value,str) and value:
                    ids.add(value)
                elif isinstance(value,list):
                    ids.update(str(v) for v in value if v)
            if hit.source.startswith('chat:'):
                ids.add(hit.source[5:])
            labels=[]
            for sid in sorted(ids):
                session=chat_manager.get_existing_session_by_session_id(sid)
                labels.append(f'{session.platform} | {session.group_name or session.group_id or "私聊"} | session={sid}' if session else f'session={sid}')
            hit.metadata['origin_label']='; '.join(labels) if labels else '来源未标注，不能视为当前群经历'

    async def enqueue_feedback_task(
        self,
        *,
        query_tool_id: str,
        session_id: str,
        query_timestamp: Any = None,
        structured_content: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        try:
            payload = await self._invoke(
                "enqueue_feedback_task",
                {
                    "query_tool_id": str(query_tool_id or "").strip(),
                    "session_id": str(session_id or "").strip(),
                    "query_timestamp": query_timestamp,
                    "structured_content": structured_content if isinstance(structured_content, dict) else {},
                },
                timeout_ms=10000,
            )
        except Exception as exc:
            logger.warning(f"反馈纠错任务入队失败: {exc}")
            return {"success": False, "queued": False, "reason": str(exc)}
        return (
            payload if isinstance(payload, dict) else {"success": False, "queued": False, "reason": "invalid_payload"}
        )

    async def ingest_summary(
        self,
        *,
        external_id: str,
        chat_id: str,
        text: str,
        participants: Optional[List[str]] = None,
        time_start: float | None = None,
        time_end: float | None = None,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        respect_filter: bool = True,
        user_id: str = "",
        group_id: str = "",
    ) -> MemoryWriteResult:
        try:
            payload = await self._invoke(
                "ingest_summary",
                {
                    "external_id": external_id,
                    "chat_id": chat_id,
                    "text": text,
                    "participants": participants or [],
                    "time_start": time_start,
                    "time_end": time_end,
                    "tags": tags or [],
                    "metadata": metadata or {},
                    "respect_filter": bool(respect_filter),
                    "user_id": str(user_id or "").strip(),
                    "group_id": str(group_id or "").strip(),
                },
            )
            return self._coerce_write_result(payload)
        except Exception as exc:
            logger.warning(f"长期记忆写入摘要失败: {exc}")
            return MemoryWriteResult(success=False, detail=str(exc))

    async def ingest_text(
        self,
        *,
        external_id: str,
        source_type: str,
        text: str,
        chat_id: str = "",
        person_ids: Optional[List[str]] = None,
        participants: Optional[List[str]] = None,
        timestamp: float | None = None,
        time_start: float | None = None,
        time_end: float | None = None,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        entities: Optional[List[str]] = None,
        relations: Optional[List[Dict[str, Any]]] = None,
        respect_filter: bool = True,
        user_id: str = "",
        group_id: str = "",
    ) -> MemoryWriteResult:
        try:
            payload = await self._invoke(
                "ingest_text",
                {
                    "external_id": external_id,
                    "source_type": source_type,
                    "text": text,
                    "chat_id": chat_id,
                    "person_ids": person_ids or [],
                    "participants": participants or [],
                    "timestamp": timestamp,
                    "time_start": time_start,
                    "time_end": time_end,
                    "tags": tags or [],
                    "metadata": metadata or {},
                    "entities": entities or [],
                    "relations": relations or [],
                    "respect_filter": bool(respect_filter),
                    "user_id": str(user_id or "").strip(),
                    "group_id": str(group_id or "").strip(),
                },
            )
            return self._coerce_write_result(payload)
        except Exception as exc:
            logger.warning(f"长期记忆写入文本失败: {exc}")
            return MemoryWriteResult(success=False, detail=str(exc))

    async def get_person_profile(self, person_id: str, *, chat_id: str = "", limit: int = 10) -> PersonProfileResult:
        clean_person_id = str(person_id or "").strip()
        if not clean_person_id:
            return PersonProfileResult()
        try:
            from utils.account_identity import linked_person_ids, linked_people
            labels = {p['person_id']:p['platform']+':'+p['user_id'] for p in linked_people(clean_person_id)}
            combined = PersonProfileResult()
            aliases = linked_person_ids(clean_person_id)
            for alias in aliases:
                try:
                    payload = await self._invoke('get_person_profile',
                        {'person_id':alias,'chat_id':chat_id,'limit':max(1,int(limit))})
                except Exception:
                    continue
                part = self._coerce_profile_result(payload)
                if part.summary:
                    origin = '['+labels.get(alias,alias)+'；同一人的不同平台经历，非本群即时对话]\n' if len(aliases)>1 else ''
                    combined.summary += ('\n' if combined.summary else '') + origin + part.summary
                combined.traits.extend(x for x in part.traits if x not in combined.traits)
                combined.evidence.extend({**e,'identity_source':labels.get(alias,alias)} for e in part.evidence)
            return combined
        except Exception as exc:
            logger.warning(f"获取人物画像失败: {exc}")
            return PersonProfileResult()

    async def maintain_memory(
        self,
        *,
        action: str,
        target: str = "",
        hours: float | None = None,
        reason: str = "",
        limit: int = 50,
    ) -> MemoryWriteResult:
        try:
            payload = await self._invoke(
                "maintain_memory",
                {"action": action, "target": target, "hours": hours, "reason": reason, "limit": limit},
            )
            if not isinstance(payload, dict):
                return MemoryWriteResult(success=False, detail="invalid_payload")
            return MemoryWriteResult(success=bool(payload.get("success")), detail=str(payload.get("detail", "") or ""))
        except Exception as exc:
            logger.warning(f"记忆维护失败: {exc}")
            return MemoryWriteResult(success=False, detail=str(exc))

    async def memory_stats(self) -> Dict[str, Any]:
        try:
            payload = await self._invoke("memory_stats", {})
            return payload if isinstance(payload, dict) else {}
        except Exception as exc:
            logger.warning(f"获取记忆统计失败: {exc}")
            return {}

    async def image_memory(self, *, action: str, timeout_ms: int = 120000, **kwargs: Any) -> Dict[str, Any]:
        """调用图片记忆的写入、相似检索和管理接口。"""

        try:
            payload = await self._invoke(
                "image_memory",
                {"action": str(action or "").strip(), **kwargs},
                timeout_ms=timeout_ms,
            )
            return payload if isinstance(payload, dict) else {"success": False, "error": "invalid_payload"}
        except Exception as exc:
            logger.warning(f"图片记忆调用失败: {exc}")
            return {"success": False, "error": str(exc)}

    async def graph_admin(self, *, action: str, **kwargs) -> Dict[str, Any]:
        try:
            return await self._invoke_admin("memory_graph_admin", action=action, **kwargs)
        except Exception as exc:
            logger.warning(f"图谱管理调用失败: {exc}")
            return {"success": False, "error": str(exc)}

    async def source_admin(self, *, action: str, **kwargs) -> Dict[str, Any]:
        try:
            return await self._invoke_admin("memory_source_admin", action=action, **kwargs)
        except Exception as exc:
            logger.warning(f"来源管理调用失败: {exc}")
            return {"success": False, "error": str(exc)}

    async def episode_admin(self, *, action: str, **kwargs) -> Dict[str, Any]:
        try:
            return await self._invoke_admin("memory_episode_admin", action=action, **kwargs)
        except Exception as exc:
            logger.warning(f"Episode 管理调用失败: {exc}")
            return {"success": False, "error": str(exc)}

    async def profile_admin(self, *, action: str, include_linked: bool = True, **kwargs) -> Dict[str, Any]:
        try:
            payload = await self._invoke_admin("memory_profile_admin", action=action, **kwargs)
            if action != 'query' or not include_linked or not isinstance(payload,dict):
                return payload
            from utils.account_identity import linked_people
            person_id = str(kwargs.get('person_id') or payload.get('person_id') or '')
            linked = []
            for person in linked_people(person_id) if person_id else []:
                if person['person_id'] == person_id:
                    continue
                try:
                    result = await self._invoke_admin('memory_profile_admin',action='query',
                        person_id=person['person_id'],limit=kwargs.get('limit',8),context_text=kwargs.get('context_text',''))
                    if isinstance(result,dict) and result.get('success'):
                        linked.append({**person,'profile_text':str(result.get('profile_text') or result.get('summary') or '')})
                except Exception as exc:
                    logger.debug('关联人物画像暂不可用: %s',type(exc).__name__)
            # Keep structured profiles separate: concatenating their headings
            # before parsing would overwrite sections and erase source labels.
            return {**payload,'linked_profiles':linked,'same_person_across_platforms':bool(linked)}
        except Exception as exc:
            logger.warning(f"画像管理调用失败: {exc}")
            return {"success": False, "error": str(exc)}

    async def feedback_admin(self, *, action: str, **kwargs) -> Dict[str, Any]:
        try:
            return await self._invoke_admin("memory_feedback_admin", action=action, **kwargs)
        except Exception as exc:
            logger.warning(f"反馈纠错管理调用失败: {exc}")
            return {"success": False, "error": str(exc)}

    async def fact_admin(self, *, action: str, **kwargs) -> Dict[str, Any]:
        try:
            return await self._invoke_admin("memory_fact_admin", action=action, **kwargs)
        except Exception as exc:
            logger.warning(f"事实账本管理调用失败: {exc}")
            return {"success": False, "error": str(exc)}

    async def runtime_admin(self, *, action: str, **kwargs) -> Dict[str, Any]:
        try:
            return await self._invoke_admin("memory_runtime_admin", action=action, **kwargs)
        except Exception as exc:
            logger.warning(f"运行时管理调用失败: {exc}")
            return {"success": False, "error": str(exc)}

    async def import_admin(self, *, action: str, timeout_ms: int = 120000, **kwargs) -> Dict[str, Any]:
        try:
            return await self._invoke_admin("memory_import_admin", action=action, timeout_ms=timeout_ms, **kwargs)
        except Exception as exc:
            logger.warning(f"导入管理调用失败: {exc}")
            return {"success": False, "error": str(exc)}

    async def bundle_admin(self, *, action: str, timeout_ms: int = 120000, **kwargs) -> Dict[str, Any]:
        try:
            return await self._invoke_admin("memory_bundle_admin", action=action, timeout_ms=timeout_ms, **kwargs)
        except Exception as exc:
            logger.warning(f"记忆包管理调用失败: {exc}")
            return {"success": False, "error": str(exc)}

    async def tuning_admin(self, *, action: str, timeout_ms: int = 120000, **kwargs) -> Dict[str, Any]:
        try:
            return await self._invoke_admin("memory_tuning_admin", action=action, timeout_ms=timeout_ms, **kwargs)
        except Exception as exc:
            logger.warning(f"调优管理调用失败: {exc}")
            return {"success": False, "error": str(exc)}

    async def v5_admin(self, *, action: str, timeout_ms: Optional[int] = None, **kwargs) -> Dict[str, Any]:
        try:
            return await self._invoke_admin("memory_v5_admin", action=action, timeout_ms=timeout_ms, **kwargs)
        except Exception as exc:
            logger.warning(f"V5 记忆管理调用失败: {exc}")
            return {"success": False, "error": str(exc)}

    async def delete_admin(self, *, action: str, timeout_ms: int = 120000, **kwargs) -> Dict[str, Any]:
        try:
            return await self._invoke_admin("memory_delete_admin", action=action, timeout_ms=timeout_ms, **kwargs)
        except Exception as exc:
            logger.warning(f"删除管理调用失败: {exc}")
            return {"success": False, "error": str(exc)}

    async def memory_correction_admin(self, *, action: str, timeout_ms: int = 120000, **kwargs) -> Dict[str, Any]:
        try:
            return await self._invoke_admin("memory_correction_admin", action=action, timeout_ms=timeout_ms, **kwargs)
        except Exception as exc:
            logger.warning(f"记忆修正管理调用失败: {exc}")
            return {"success": False, "error": str(exc)}

    async def fuzzy_modify_admin(self, *, action: str, timeout_ms: int = 120000, **kwargs) -> Dict[str, Any]:
        return await self.memory_correction_admin(action=action, timeout_ms=timeout_ms, **kwargs)

    async def get_recycle_bin(self, *, limit: int = 50) -> Dict[str, Any]:
        try:
            payload = await self._invoke(
                "maintain_memory", {"action": "recycle_bin", "limit": max(1, int(limit or 50))}
            )
            return payload if isinstance(payload, dict) else {"success": False, "error": "invalid_payload"}
        except Exception as exc:
            logger.warning(f"获取回收站失败: {exc}")
            return {"success": False, "error": str(exc)}

    async def restore_memory(self, *, target: str) -> MemoryWriteResult:
        return await self.maintain_memory(action="restore", target=target)

    async def reinforce_memory(self, *, target: str) -> MemoryWriteResult:
        return await self.maintain_memory(action="reinforce", target=target)

    async def freeze_memory(self, *, target: str) -> MemoryWriteResult:
        return await self.maintain_memory(action="freeze", target=target)

    async def protect_memory(self, *, target: str, hours: float | None = None) -> MemoryWriteResult:
        return await self.maintain_memory(action="protect", target=target, hours=hours)


memory_service = MemoryService()
