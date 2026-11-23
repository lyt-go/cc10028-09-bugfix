"""失败重采服务：解析、识别与抽取失败环节的失败原因归档与按范围重采恢复。

从解析失败任务中创建重采任务，按失败环节恢复，支持断点续采。

终态（已完成/已放弃）通过一份只写一次的「终态确认」记录落地：
- 同一重采编号重复到达时直接重放首次应答，不重新累计重试次数；
- 已完成的重采编号不会因为再次受理而回退到待重采或改判为放弃；
- 已放弃的重采编号不会被悄悄重新打开；
- 获准续做的范围（续做凭据 + 文档/页码）由格式解析消费并落盘解析页明细，
  同一续做凭据只保留一份明细，重复受理不会新建解析记录。
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class ParseRetryService(BaseModuleService):
    MODULE = "parse_retry"
    ENTITY = "重采任务"
    OPERATION_TERM = "重采"
    LIST_FIELDS = ["重采编号", "失败环节", "原始批次", "失败原因", "重试次数", "重采范围", "恢复游标", "重采结果"]
    REQUIRED_FIELDS = ["重采编号", "失败环节", "原始批次"]
    STATUS_ORDER = ["草稿", "待重采", "重采中", "已完成", "已放弃"]
    ACTION_RULES = {"启动重采": "待重采", "标记完成": "已完成", "标记放弃": "已放弃"}
    NEGATIVE_ACTIONS = ["标记放弃"]

    TERMINAL_STATUSES = ("已完成", "已放弃")
    MAX_RETRIES = 3
    CONFIRMATION_FILE = "terminal_confirmations.jsonl"

    # -- 终态确认读写 --

    def _confirmation_path(self):
        return self._data_dir("retry_confirmations") / self.CONFIRMATION_FILE

    def list_terminal_confirmations(self) -> list[dict[str, Any]]:
        """列出全部终态确认记录，供续做入口与重采详情核对同一次确认。"""
        return self._read_jsonl(self._confirmation_path())

    def get_terminal_confirmation(
        self, retry_id: str, batch_key: str | None = None,
    ) -> dict[str, Any] | None:
        """按重采编号读取终态确认；同一编号始终只对应一条确认记录。

        重采编号在不同批次间可能被复用，传入批次键时只匹配该批次下的确认，
        避免把别的批次的终态套到当前任务上。
        """
        for record in self._read_jsonl(self._confirmation_path()):
            if record.get("重采编号") != retry_id:
                continue
            if batch_key is None or record.get("原始批次") == batch_key:
                return record
        return None

    @staticmethod
    def _confirmation_key(task: dict[str, Any]) -> str:
        """终态确认的业务键：同一批次内的重采编号才算同一次终态。"""
        return f"{task.get('原始批次', '')}|{task.get('重采编号', '')}"

    def _confirmation_matches(self, confirmation: dict[str, Any], task: dict[str, Any]) -> bool:
        """确认记录与当前任务是否属于同一次受理。

        同一重采编号被复用到另一个原始批次/范围时视为新的任务实例，
        避免历史确认把新任务直接当作已完成重放。
        """
        return (
            confirmation.get("原始批次") == task.get("原始批次")
            and confirmation.get("失败环节") == task.get("失败环节")
            and self._scope_key(confirmation.get("续做范围")) == self._scope_key(task.get("重采范围"))
        )

    @staticmethod
    def _scope_key(scope: Any) -> str:
        if isinstance(scope, dict):
            return str(scope.get("文档编号") or scope.get("范围") or "")
        return str(scope or "")

    @staticmethod
    def _scope_doc_id(scope: Any) -> str:
        if isinstance(scope, dict):
            return str(scope.get("文档编号") or scope.get("范围") or "")
        return str(scope or "")

    def _record_terminal_confirmation(
        self,
        retry_id: str,
        task: dict[str, Any],
        terminal_status: str,
        *,
        reason: str = "",
        recovered_offset: int = 0,
        response: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """写入终态确认（按批次+重采编号幂等替换，保证一次终态只有一条确认）。"""
        existing = self.get_terminal_confirmation(retry_id, task.get("原始批次"))
        credential = f"RESUME-{retry_id}" if terminal_status == "已完成" else ""
        scope = {
            "文档编号": self._scope_doc_id(task.get("重采范围")),
            "失败环节": task.get("失败环节", ""),
            "页码范围": [1, 2, 3],
            "起始游标": recovered_offset,
        }
        confirmation = {
            "确认编号": f"CONFIRM-{retry_id}",
            "重采编号": retry_id,
            "_确认键": self._confirmation_key(task),
            "原始批次": task.get("原始批次", ""),
            "失败环节": task.get("失败环节", ""),
            "终态": terminal_status,
            "续做凭据": credential,
            "续做范围": scope,
            "确认时间": (existing or {}).get("确认时间") or time.strftime("%Y-%m-%d %H:%M:%S"),
            "确认原因": reason,
            "解析明细": (existing or {}).get("解析明细"),
            "首次应答": response if response is not None else (existing or {}).get("首次应答"),
        }
        if existing:
            # 幂等重放：保留首次确认时间、解析明细与首次应答，只补齐缺失字段。
            for key in ("确认时间", "解析明细", "首次应答"):
                if existing.get(key) is not None:
                    confirmation[key] = existing[key]
        self._write_jsonl_keyed(
            self._confirmation_path(), [confirmation], "_确认键", self._confirmation_key(task),
        )
        return confirmation

    def attach_resume_detail(
        self, retry_id: str, detail: dict[str, Any], batch_key: str | None = None,
    ) -> dict[str, Any] | None:
        """把格式解析落盘结果回写到终态确认，供续做入口重复核对。"""
        confirmation = self.get_terminal_confirmation(retry_id, batch_key)
        if confirmation is None:
            return None
        confirmation["解析明细"] = detail
        self._write_jsonl_keyed(
            self._confirmation_path(), [confirmation], "_确认键",
            self._confirmation_key(confirmation),
        )
        return confirmation

    # -- 任务文件定位与落盘 --

    def _find_task(self, retry_id: str) -> tuple[Any | None, list[dict[str, Any]], dict[str, Any] | None]:
        """在retries目录中定位重采编号对应的任务文件与记录。"""
        for retry_file in sorted(self._data_dir("retries").glob("*.jsonl")):
            tasks = self._read_jsonl(retry_file)
            for task in tasks:
                if task.get("重采编号") == retry_id:
                    return retry_file, tasks, task
        return None, [], None

    @staticmethod
    def _persist_tasks(retry_file, tasks: list[dict[str, Any]]) -> None:
        with open(retry_file, "w", encoding="utf-8") as f:
            for task in tasks:
                f.write(json.dumps(task, ensure_ascii=False, default=str) + "\n")

    def _sync_store_row(self, retry_id: str, status: str, confirmation: dict[str, Any]) -> None:
        """把终态同步到内存重采详情行，避免列表详情与续做应答对不上。"""
        batch_key = confirmation.get("原始批次")
        for row in store.rows(self.MODULE):
            if row.get("重采编号") != retry_id:
                continue
            if batch_key and row.get("原始批次") and row.get("原始批次") != batch_key:
                continue
            row["status"] = status
            row["pending"] = False
            row["abnormal"] = status == "已放弃"
            row["终态确认"] = confirmation.get("确认编号")
            row["续做凭据"] = confirmation.get("续做凭据") or ""

    def _replay_response(
        self, confirmation: dict[str, Any], task: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """重放首次终态应答；缺失首次应答时按确认记录重建同构应答。"""
        response = confirmation.get("首次应答")
        if response:
            return dict(response)
        status = confirmation.get("终态", "")
        if status == "已完成":
            rebuilt = {
                "retry_id": confirmation.get("重采编号"),
                "status": "已完成",
                "attempts": self._int_or_zero((task or {}).get("重试次数")),
                "recovered_offset": (confirmation.get("续做范围") or {}).get("起始游标", 0),
                "confirmation_id": confirmation.get("确认编号"),
                "resume_credential": confirmation.get("续做凭据"),
                "resume_scope": confirmation.get("续做范围"),
            }
            if confirmation.get("解析明细"):
                rebuilt["resume"] = confirmation["解析明细"]
            return rebuilt
        return {
            "retry_id": confirmation.get("重采编号"),
            "status": "已放弃",
            "reason": confirmation.get("确认原因", "终态已确认"),
            "confirmation_id": confirmation.get("确认编号"),
        }

    def _replay_terminal(
        self, retry_id: str, task: dict[str, Any], confirmation: dict[str, Any],
    ) -> dict[str, Any]:
        """按已有终态确认重放：同步详情行、按需补齐续做，并只记录一次首次应答。"""
        status = str(confirmation.get("终态", ""))
        self._sync_store_row(retry_id, status, confirmation)

        # 已完成终态需要保证续做范围已经落盘；resume_from_retry 自身幂等，
        # 已有解析明细时直接返回首次落盘结果，不会重复生成解析页或解析任务。
        if status == "已完成" and not confirmation.get("解析明细"):
            self._trigger_resume(retry_id, task.get("原始批次"))
            confirmation = self.get_terminal_confirmation(retry_id, task.get("原始批次")) or confirmation

        response = self._replay_response(confirmation, task)
        if not confirmation.get("首次应答"):
            stored = self._record_terminal_confirmation(
                retry_id, task, status,
                reason=str(confirmation.get("确认原因") or "终态已确认"),
                recovered_offset=self._int_or_zero((confirmation.get("续做范围") or {}).get("起始游标")),
                response=response,
            )
            self._sync_store_row(retry_id, status, stored)
        return response

    def _trigger_resume(self, retry_id: str, batch_key: str | None = None) -> dict[str, Any] | None:
        """完成确认后触发格式解析消费获准续做的范围。"""
        confirmation = self.get_terminal_confirmation(retry_id, batch_key)
        if confirmation is None or confirmation.get("终态") != "已完成":
            return None
        if confirmation.get("失败环节") != "格式解析":
            return None
        try:
            cls = self._safe_import("app.services.format_parse", "FormatParseService")
            return cls().resume_from_retry(retry_id, batch_key)
        except Exception as e:  # 解析消费失败不能改写终态
            return {"retry_id": retry_id, "error": str(e)}

    # -- 动作流转 --

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        """重采动作：终态确认后不再允许回退或改判，重复确认按重放处理。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        target = self.ACTION_RULES.get(action)
        if target is None:
            return None, f"动作「{action}」不属于{self.MODULE}可执行范围"

        retry_id = str(entry.get("重采编号") or "")
        batch_key = str(entry.get("原始批次") or "")
        confirmation = self.get_terminal_confirmation(retry_id, batch_key) if retry_id else None

        if confirmation is not None:
            terminal = confirmation.get("终态")
            if terminal == target:
                # 重复确认只重放，不再走一遍流转和续做触发。
                return entry, f"{self.ENTITY}已{action}（重放终态确认 {confirmation.get('确认编号')}）"
            return entry, f"{self.ENTITY}终态已确认（{terminal}，确认 {confirmation.get('确认编号')}），不能流转为{target}"

        if entry.get("status") in self.TERMINAL_STATUSES:
            # 历史终态数据没有确认记录时补记一次，之后按确认记录重放。
            confirmation = self._record_terminal_confirmation(
                retry_id, entry, str(entry.get("status")),
                reason=str(entry.get("重采结果") or "历史终态"), response=None,
            )
            self._sync_store_row(retry_id, str(entry.get("status")), confirmation)
            if target == entry.get("status"):
                return entry, f"{self.ENTITY}已{action}（重放终态确认 {confirmation.get('确认编号')}）"
            return entry, f"{self.ENTITY}终态已确认（{entry.get('status')}），不能流转为{target}"

        if action == "启动重采" and target == "待重采" and retry_id:
            # 启动只推进到重采中，不允许把已有进度重置回待重采。
            entry["status"] = "重采中"
            entry["pending"] = True
            entry["abnormal"] = False
            return entry, f"{self.ENTITY}已{action}"

        entry["status"] = target
        entry["pending"] = target not in self.TERMINAL_STATUSES
        entry["abnormal"] = action in self.NEGATIVE_ACTIONS

        if target in self.TERMINAL_STATUSES and retry_id:
            confirmation = self._record_terminal_confirmation(
                retry_id, entry, target,
                reason=str(entry.get("重采结果") or f"动作「{action}」确认"),
            )
            self._sync_store_row(retry_id, target, confirmation)
            if target == "已完成":
                self._trigger_resume(retry_id, batch_key)
            return entry, f"{self.ENTITY}已{action}（终态确认 {confirmation.get('确认编号')}）"
        return entry, f"{self.ENTITY}已{action}"

    # -- 重采任务创建与执行 --

    def _next_retry_sequence(self) -> int:
        """取现有重采编号的最大序号，保证新任务编号跨批次也不重复。"""
        max_seq = 0
        for retry_file in self._data_dir("retries").glob("*.jsonl"):
            for task in self._read_jsonl(retry_file):
                matched = re.fullmatch(r"RETRY-(\d+)", str(task.get("重采编号", "")))
                if matched:
                    max_seq = max(max_seq, int(matched.group(1)))
        return max_seq

    def create_retry_from_failures(self, batch_key: str) -> dict[str, Any]:
        """从解析失败的批次中创建重采任务，按失败环节归档。"""
        retry_tasks = []
        retry_counter = self._next_retry_sequence()

        # 检查格式解析失败
        parse_rows = [r for r in store.rows("format_parse") if r.get("status") == "解析失败" and r.get("abnormal")]
        for pr in parse_rows:
            retry_counter += 1
            retry_tasks.append({
                "重采编号": f"RETRY-{retry_counter:05d}",
                "失败环节": "格式解析",
                "原始批次": batch_key,
                "失败原因": f"解析任务 {pr.get('任务编号', '?')} 异常失败",
                "重试次数": "0",
                "重采范围": pr.get("文档编号", "全量"),
# 数据操作：重采失败任务数据按环节归档创建重采任务
                "恢复游标": "0",
                "重采结果": "",
                "status": "待重采",
            })

        # 检查OCR失败
        ocr_rows = [r for r in store.rows("ocr_engine") if r.get("status") == "识别失败" and r.get("abnormal")]
        for ocr in ocr_rows:
            retry_counter += 1
            retry_tasks.append({
                "重采编号": f"RETRY-{retry_counter:05d}",
                "失败环节": "OCR识别",
                "原始批次": batch_key,
                "失败原因": f"识别任务 {ocr.get('任务编号', '?')} 异常失败",
                "重试次数": "0",
                "重采范围": ocr.get("图片区域", "全量"),
                "恢复游标": "0",
                "重采结果": "",
                "status": "待重采",
            })

        if not retry_tasks:
            return {"batch_key": batch_key, "retry_tasks": 0, "message": "未发现需重试的失败任务"}

        output_path = self._data_dir("retries") / f"{batch_key}_retries.jsonl"
        written = self._write_jsonl(output_path, retry_tasks)

        self._write_checkpoint(f"retry_{batch_key}", {
            "batch_key": batch_key,
            "tasks_created": len(retry_tasks),
# 数据产物：重采任务清单写入retries目录持久化文件
            "by_stage": {
                "格式解析": sum(1 for t in retry_tasks if t["失败环节"] == "格式解析"),
                "OCR识别": sum(1 for t in retry_tasks if t["失败环节"] == "OCR识别"),
            },
        })

        return {
            "batch_key": batch_key,
            "retry_tasks": len(retry_tasks),
            "records_written": written,
            "output": str(output_path),
        }

    def execute_retry(self, retry_id: str) -> dict[str, Any]:
        """执行重采任务：终态编号直接重放确认，未终态时从上次失败位点恢复。"""
        retry_file, tasks, task = self._find_task(retry_id)
        if task is None:
            return {"error": f"重采任务 {retry_id} 不存在"}

        confirmation = self.get_terminal_confirmation(retry_id, task.get("原始批次"))
        if confirmation is not None and not self._confirmation_matches(confirmation, task):
            # 同一编号被复用到不同任务实例，旧确认不适用，按新任务重新受理。
            confirmation = None

        if confirmation is not None:
            # 终态已确认：无论第几次到达都只重放首次应答，不改写状态与重试次数。
            return self._replay_terminal(retry_id, task, confirmation)

        # 历史任务已处于终态但缺少确认记录：补记一次终态确认后重放。
        if task.get("status") in self.TERMINAL_STATUSES:
            reason = str(task.get("重采结果") or "历史终态")
            confirmation = self._record_terminal_confirmation(
                retry_id, task, str(task.get("status")), reason=reason,
                recovered_offset=self._int_or_zero(task.get("恢复游标")),
            )
            return self._replay_terminal(retry_id, task, confirmation)

        # 从检查点恢复游标，同一次重采编号只累计一次重试次数。
        cp_name = f"retry_{retry_id}"
        cp = self._read_checkpoint(cp_name) or {"retry_id": retry_id, "attempts": 0, "offset": 0}
        cp["attempts"] = cp.get("attempts", 0) + 1
        cp["offset"] = cp.get("offset", 0)
        cp["last_attempt_at"] = time.time()

        if cp["attempts"] > self.MAX_RETRIES:
            reason = f"超过{self.MAX_RETRIES}次重试上限"
# 数据产物：重试用尽边界归档记录标记放弃状态并归档数据
            task["重采结果"] = f"放弃：已达最大重试次数({self.MAX_RETRIES})"
            task["status"] = "已放弃"
            task["重试次数"] = str(cp["attempts"])
            self._persist_tasks(retry_file, tasks)
            confirmation = self._record_terminal_confirmation(
                retry_id, task, "已放弃", reason=reason, recovered_offset=cp["offset"],
            )
            self._sync_store_row(retry_id, "已放弃", confirmation)
            return {"retry_id": retry_id, "status": "已放弃", "reason": reason,
                    "confirmation_id": confirmation["确认编号"]}

        # 模拟重采成功
        recovered_offset = cp["offset"] + 100
        task["重采结果"] = "成功"
        task["status"] = "已完成"
        task["重试次数"] = str(cp["attempts"])
        task["恢复游标"] = str(recovered_offset)
        self._write_checkpoint(cp_name, cp)
        self._persist_tasks(retry_file, tasks)

        confirmation = self._record_terminal_confirmation(
            retry_id, task, "已完成", reason="重采执行成功", recovered_offset=recovered_offset,
        )
        self._sync_store_row(retry_id, "已完成", confirmation)

        # 格式解析消费终态记录中获准续做的范围，落盘同一续做凭据的解析页明细。
        resume_detail = self._trigger_resume(retry_id, task.get("原始批次"))

        response = {
            "retry_id": retry_id,
            "status": "已完成",
            "attempts": cp["attempts"],
            "recovered_offset": recovered_offset,
            "confirmation_id": confirmation["确认编号"],
            "resume_credential": confirmation["续做凭据"],
            "resume_scope": confirmation["续做范围"],
        }
        if resume_detail is not None:
            response["resume"] = resume_detail
        self._record_terminal_confirmation(
            retry_id, task, "已完成", reason="重采执行成功",
            recovered_offset=recovered_offset, response=response,
        )
        return response

    @staticmethod
    def _int_or_zero(value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    def get_failure_summary(self, batch_key: str) -> dict[str, Any]:
        """获取指定批次的失败汇总，按环节分组统计失败数量和原因。"""
        # 收集各环节的失败记录
        parse_failures = [r for r in store.rows("format_parse") if r.get("status") == "解析失败"]
        ocr_failures = [r for r in store.rows("ocr_engine") if r.get("status") == "识别失败"]
        entity_failures = [r for r in store.rows("entity_recognize") if r.get("status") == "已驳回"]

        summary = {
            "batch_key": batch_key,
            "total_failures": len(parse_failures) + len(ocr_failures) + len(entity_failures),
            "by_stage": {
                "格式解析": len(parse_failures),
                "OCR识别": len(ocr_failures),
                "实体识别": len(entity_failures),
            },
            "retryable": len(parse_failures) + len(ocr_failures),
        }
        return summary
