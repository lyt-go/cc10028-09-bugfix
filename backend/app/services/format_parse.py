"""格式解析服务：PDF/Word/HTML等格式文档的解析器选择、页码范围切分与输出格式控制。

解析完成后写入已解析文件，自动触发分块切分和版式还原。
"""
from __future__ import annotations

import time
import re
from pathlib import Path
from typing import Any

from app.services.base import BaseModuleService, _ensure_dir
from app.store import store


class FormatParseService(BaseModuleService):
    MODULE = "format_parse"
    ENTITY = "解析任务"
    OPERATION_TERM = "解析"
    LIST_FIELDS = ["任务编号", "文档编号", "解析器类型", "页码范围", "解析模式", "输出格式", "超时秒数", "解析器版本"]
    REQUIRED_FIELDS = ["任务编号", "文档编号", "解析器类型"]
    STATUS_ORDER = ["草稿", "待解析", "解析中", "已完成", "解析失败"]
    ACTION_RULES = {"启动解析": "解析中", "标记完成": "已完成", "标记失败": "解析失败"}
    NEGATIVE_ACTIONS = ["标记失败"]

    # 支持的解析器类型与对应正则
    PARSER_REGISTRY = {
        "PDF": {"description": "PDF文档解析器，提取文本段落与页面布局"},
        "DOCX": {"description": "Word文档解析器，提取段落样式与层级结构"},
        "HTML": {"description": "HTML文档解析器，提取标签语义与DOM树"},
# 数据产物：从接入批次中批量创建解析任务数据集
        "TXT": {"description": "纯文本文档解析器，按字符集编码读取"},
        "MARKDOWN": {"description": "Markdown解析器，提取标题层级与代码块"},
    }

    def create_from_batch(self, batch_key: str) -> dict[str, Any]:
        """从接入批次创建解析任务，读取批次中的文档清单逐个生成解析任务。"""
        # 读取已校验批次文件
        batch_file = self._data_dir("batches") / "validated.jsonl"
        batches = self._read_jsonl(batch_file)
        matched = [b for b in batches if b.get("batch_key") == batch_key]
        if not matched:
            return {"error": f"批次 {batch_key} 未找到校验记录，可能未通过校验"}
        batch = matched[0]

        batch_rows = [r for r in store.rows("file_intake") if r.get("批次编号") == batch_key]
        if not batch_rows:
            return {"error": f"批次 {batch_key} 未在file_intake登记"}

        batch_row = batch_rows[0]
        file_list = batch_row.get("文件清单", "").split(",")
        file_type = batch_row.get("文件类型", "PDF")
        parser_type = self.PARSER_REGISTRY.get(file_type, self.PARSER_REGISTRY["PDF"])

        created = []
        for i, fname in enumerate(file_list):
            fname = fname.strip()
            if not fname:
                continue
            task_values = {
                "任务编号": f"PARSE-{batch_key}-{i+1:03d}",
                "文档编号": fname,
                "解析器类型": file_type,
                "页码范围": "",
                "解析模式": "full",
                "输出格式": "jsonl",
                "超时秒数": "300",
                "解析器版本": "v2.1.0",
            }
            result, missing = self.create_entry(task_values)
            if result:
                created.append(result["任务编号"])
        return {"batch_key": batch_key, "tasks_created": len(created), "task_ids": created}

    def execute_parse(self, task_id: int) -> dict[str, Any]:
        """执行文档解析任务，将解析产物持久化为JSONL文件，同时触发下游分块切分。"""
        task = store.find(self.MODULE, task_id)
        if task is None:
# 数据操作：解析文档生成已解析页面产物与区域检测
            return {"error": f"解析任务 {task_id} 不存在"}
        if task.get("status") not in ("草稿", "待解析", "解析中"):
            return {"error": f"解析任务 {task_id} 状态不允许执行解析，当前状态: {task.get('status')}"}

        task["status"] = "解析中"
        start_time = time.time()
        doc_id = task["文档编号"]
        parser = task["解析器类型"]

        try:
            # 模拟文档解析
            content = self._simulate_doc_content(doc_id, paragraphs=8)
            pages = self._simulate_pages(content)
            table_regions = self._detect_table_regions(pages)
            image_regions = self._detect_image_regions(pages)

            # 写入解析产物
            output_path = self._data_dir("parsed") / f"{doc_id}_parsed.jsonl"
            parsed_records = []
            for p in pages:
                parsed_records.append({
                    "doc_id": doc_id,
# 数据产物：解析page产物数据写入jsonl文件
                    "page_num": p["page"],
                    "paragraphs": len(p["paragraphs"]),
                    "text_length": p["text_length"],
                    "has_tables": p["has_tables"],
                    "has_images": p["has_images"],
                    "parser": parser,
                    "content_hash": self._compute_checksum("\n".join(p["paragraphs"])),
                })
            written = self._write_jsonl(output_path, parsed_records)

            # 写入表格区域到共享产物，供table_extract读取
            if table_regions:
                table_path = self._data_dir("layout") / f"{doc_id}_tables.jsonl"
                self._write_jsonl(table_path, table_regions)

            # 写入图片区域到共享产物，供ocr_engine读取
            if image_regions:
                image_path = self._data_dir("layout") / f"{doc_id}_images.jsonl"
                self._write_jsonl(image_path, image_regions)

            elapsed = time.time() - start_time
# 数据操作：解析完毕写入检查点，存储数据集至持久化文件
            task["status"] = "已完成"
            task["pending"] = False
            if elapsed > float(task.get("超时秒数") or 300):
                task["abnormal"] = True

            # 写入检查点
            self._write_checkpoint(f"parse_{doc_id}", {
                "task_id": task_id,
                "doc_id": doc_id,
                "pages": len(pages),
                "records_written": written,
                "elapsed": elapsed,
                "table_count": len(table_regions),
                "image_count": len(image_regions),
            })

            # 跨模块自动触发
            triggers = {}
            trig_chunk = self._trigger_chunk_split(f"{doc_id}_parsed")
            triggers["chunk_split"] = trig_chunk
            trig_layout = self._trigger_layout_restore(doc_id)
            triggers["layout_restore"] = trig_layout
            trig_quality = self._trigger_quality_check(doc_id)
            triggers["extract_quality"] = trig_quality

            return {
                "task_id": task_id,
                "doc_id": doc_id,
                "pages": len(pages),
                "records_written": written,
# 【数据工程】解析操作完成后触发下游分块数据集消费
                "elapsed": round(elapsed, 2),
                "output": str(output_path),
                "triggers": triggers,
            }
        except Exception as e:
            task["status"] = "解析失败"
            task["abnormal"] = True
            return {"task_id": task_id, "error": str(e), "doc_id": doc_id}

    def resume_from_retry(self, retry_id: str, batch_key: str | None = None) -> dict[str, Any]:
        """消费重采终态确认中获准续做的范围，落盘续做凭据对应的解析页明细。

        只有「已完成」的终态确认才带续做凭据；重复受理同一重采编号时
        按页幂等覆盖明细，不新建解析任务记录，也不重复追加解析页。
        """
        try:
            retry_cls = self._safe_import("app.services.parse_retry", "ParseRetryService")
            retry_svc = retry_cls()
        except Exception as e:
            return {"error": f"重采服务不可用：{e}"}

        confirmation = retry_svc.get_terminal_confirmation(retry_id, batch_key)
        if confirmation is None:
            return {"error": f"重采任务 {retry_id} 尚无终态确认，不允许续做"}
        if confirmation.get("终态") != "已完成":
            return {
                "error": f"重采任务 {retry_id} 终态为{confirmation.get('终态')}，不进入续做",
                "confirmation_id": confirmation.get("确认编号"),
            }

        credential = confirmation.get("续做凭据")
        if not credential:
            return {"error": f"重采任务 {retry_id} 的终态确认缺少续做凭据"}

        # 同一终态确认重复到达时直接返回首次落盘结果，不重复生成解析页。
        stored_detail = confirmation.get("解析明细")
        if stored_detail and Path(str(stored_detail.get("output", ""))).exists():
            return stored_detail

        scope = confirmation.get("续做范围") or {}
        doc_id = scope.get("文档编号") or ""
        page_numbers = scope.get("页码范围") or [1, 2, 3]
        if not doc_id:
            return {"error": f"重采任务 {retry_id} 的续做范围缺少文档编号"}

        page_records = []
        for page_num in page_numbers:
            text = self._simulate_doc_content(f"{doc_id}-p{page_num}", paragraphs=2)
            page_records.append({
                "page_id": f"{credential}:{page_num}",
                "续做凭据": credential,
                "重采编号": retry_id,
                "确认编号": confirmation.get("确认编号"),
                "doc_id": doc_id,
                "page_num": int(page_num),
                "paragraph_count": len(text),
                "text_length": sum(len(line) for line in text),
                "parser": "PDF",
                "content_hash": self._compute_checksum("\n".join(text)),
            })

        output_path = self._data_dir("parsed") / f"{credential}_pages.jsonl"
        written = 0
        for record in page_records:
            # 按页键幂等覆盖：同一续做凭据重复受理只保留一份页明细。
            written += self._write_jsonl_keyed(output_path, [record], "page_id", record["page_id"])

        recovered = self._mark_source_parse_recovered(confirmation, doc_id, credential)

        detail = {
            "retry_id": retry_id,
            "confirmation_id": confirmation.get("确认编号"),
            "resume_credential": credential,
            "doc_id": doc_id,
            "pages": len(page_records),
            "records_written": written,
            "output": str(output_path),
            "recovered_parse_tasks": recovered,
        }
        retry_svc.attach_resume_detail(retry_id, detail, batch_key)
        return detail

    def _mark_source_parse_recovered(
        self, confirmation: dict[str, Any], doc_id: str, credential: str,
    ) -> list[str]:
        """把获准续做范围内仍停在校验失败态的原始解析任务推进到已完成。

        只更新既有解析任务的状态，不新增任务行；同一终态确认重复到达时结果不变。
        """
        recovered = []
        for row in store.rows(self.MODULE):
            if row.get("文档编号") != doc_id:
                continue
            if row.get("status") not in ("解析失败", "待解析", "解析中", "草稿"):
                continue
            row["status"] = "已完成"
            row["pending"] = False
            row["abnormal"] = False
            row["终态确认"] = confirmation.get("确认编号")
            row["续做凭据"] = credential
            recovered.append(str(row.get("任务编号", row.get("id", ""))))
        return recovered

    def _simulate_pages(self, content: list[str]) -> list[dict[str, Any]]:
        """模拟将文本内容切分为多页文档，每页含段落、表格和图片区域标记。"""
        pages = []
        for page_num in range(1, 4):
            start = (page_num - 1) * (len(content) // 3 + 1)
            end = min(page_num * (len(content) // 3 + 1), len(content))
            page_lines = content[start:end]
            pages.append({
                "page": page_num,
                "paragraphs": page_lines,
                "text_length": sum(len(l) for l in page_lines),
                "has_tables": page_num % 2 == 1,
                "has_images": page_num == 2,
            })
        return pages

    def _detect_table_regions(self, pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """检测页面中的表格区域边界并生成结构化输出标记。"""
        regions = []
        for p in pages:
            if p["has_tables"]:
                regions.append({
                    "page": p["page"],
                    "doc_id": "from_parse",
                    "bbox": f"({p['page']*10}, {p['page']*8}, {p['page']*10+200}, {p['page']*8+80})",
                    "rows": 4,
                    "cols": 5,
                    "header_row": 1,
                    "merged_cells": 2,
                })
        return regions

    def _detect_image_regions(self, pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """检测页面中的图片区域边界并标记识别语言和精度要求。"""
        regions = []
        for p in pages:
            if p["has_images"]:
                regions.append({
                    "page": p["page"],
                    "doc_id": "from_parse",
                    "bbox": f"({p['page']*5}, {p['page']*4+1}, {p['page']*5+150}, {p['page']*4+151})",
                    "language": "chinese",
                    "precision": "high",
                })
        return regions