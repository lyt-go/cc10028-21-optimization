"""结果交付服务：按版本差异选交清单生成增量数据包、断点续传与交付回执。

一次交付请求由 (文档, 基准版本, 目标版本, 接收方, 模式) 唯一确定 request_key，
因此导入失败后重试能识别为同一次请求，不会重复投递或把半包当成新包。

增量包内容取决于接收方基准：
* 可核实基准 -> 只装变更记录与删除墓碑；
* 基准不可核实或首次下载 -> 关系端点实体随包补齐 / 全量实体与关系。

回执列出依赖对象及其所属版本，目标导入时据此判断端点是否能补齐。
"""
from __future__ import annotations

import hashlib
import time
from typing import Any

from app.services.base import BaseModuleService

# 交付状态机
PENDING = "待导出"
PACKAGED = "已打包"
FAILED = "失败"
DELIVERED = "已交付"


class ResultDeliverService(BaseModuleService):
    MODULE = "result_deliver"
    ENTITY = "交付任务"
    OPERATION_TERM = "导出"
    LIST_FIELDS = ["交付编号", "快照编号", "交付格式", "目标系统", "字段范围", "记录条数", "分片大小", "交付时间"]
    REQUIRED_FIELDS = ["交付编号", "快照编号", "交付格式"]
    STATUS_ORDER = ["草稿", "待导出", "导出中", "已交付"]
    ACTION_RULES = {"启动导出": "待导出", "确认交付": "已交付", "标记失败": "已交付"}
    NEGATIVE_ACTIONS = []

    SUPPORTED_FORMATS = {
        "jsonl": {"mime": "application/x-ndjson", "description": "每行一条JSON记录的文本格式"},
        "csv": {"mime": "text/csv", "description": "逗号分隔值表格格式"},
        "parquet": {"mime": "application/octet-stream", "description": "列式存储格式"},
        "xlsx": {"mime": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "description": "Excel表格格式"},
    }

    SHARD_SIZE = 200

    # ------------------------------------------------------------------
    # 整包交付（兼容既有链路）
    # ------------------------------------------------------------------

    def deliver_from_snapshot(self, snapshot_id: str) -> dict[str, Any]:
        """从版本快照导出抽取产物到目标系统，支持分片分批交付。"""
        snapshot_data = self._find_snapshot(snapshot_id)
        if not snapshot_data:
            return {"error": f"版本快照 {snapshot_id} 不存在，请先创建版本快照"}

        doc_id = snapshot_data.get("文档编号", "")
        fmt = "jsonl"
        target = "下游知识库"

        deliverables = self._collect_deliverables(doc_id)
        if not deliverables:
            return {"error": f"快照 {snapshot_id} 无可交付的抽取产物"}

        shards = self._chunk(deliverables, 500)
        output_files = []
        for i, shard in enumerate(shards):
            output_path = self._data_dir("deliveries") / f"{snapshot_id}_shard_{i+1:03d}.jsonl"
            self._write_jsonl_replace(output_path, shard)
            output_files.append(str(output_path))

        delivery = {
            "交付编号": f"DELIVER-{snapshot_id}",
            "快照编号": snapshot_id,
            "交付格式": fmt,
            "目标系统": target,
            "字段范围": "实体+关系+三元组+向量",
            "记录条数": len(deliverables),
            "分片大小": 500,
            "交付时间": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self._write_jsonl_replace(
            self._data_dir("deliveries") / f"{snapshot_id}_delivery.jsonl", [delivery]
        )
        self._write_checkpoint(f"deliver_{snapshot_id}", {
            "snapshot_id": snapshot_id,
            "format": fmt,
            "target": target,
            "total_records": len(deliverables),
            "shards": len(shards),
        })

        return {
            "snapshot_id": snapshot_id,
            "delivery_id": delivery["交付编号"],
            "format": fmt,
            "target": target,
            "total_records": len(deliverables),
            "shards": len(shards),
            "output_files": output_files,
        }

    # ------------------------------------------------------------------
    # 版本差异增量交付
    # ------------------------------------------------------------------

    def deliver_incremental(
        self,
        doc_id: str,
        base_version: str,
        target_version: str,
        *,
        receiver_id: str = "default",
        receiver_has_baseline: bool = True,
        baseline_verified: bool = True,
        request_id: str | None = None,
        fail_after_shards: int | None = None,
    ) -> dict[str, Any]:
        """按版本差异选交清单生成增量数据包并登记回执。

        fail_after_shards 仅用于验证断点：打包到第 N 个分片后模拟失败，
        状态落到 FAILED，再次调用同一请求会从断点续传而不是重开一次交付。
        """
        from app.services.doc_version import DocVersionService

        manifest = DocVersionService().build_selection_manifest(
            doc_id,
            base_version,
            target_version,
            receiver_id=receiver_id,
            receiver_has_baseline=receiver_has_baseline,
            baseline_verified=baseline_verified,
        )
        if "error" in manifest:
            return {"ok": False, **manifest}

        mode = manifest["mode"]
        request_key = self._request_key(doc_id, base_version, target_version, receiver_id, mode)
        request_dir = self._request_dir(request_key)
        state_path = request_dir / "state.json"
        state = self._read_json(state_path)
        # 同一 request_key 已有状态，说明这是同一次请求的重试/续传。
        retried = state is not None

        if state is None:
            state = {
                "request_key": request_key,
                "request_id": request_id or request_key,
                "doc_id": doc_id,
                "base_version": base_version,
                "target_version": target_version,
                "receiver_id": receiver_id,
                "mode": mode,
                "status": PENDING,
                "completed_shards": 0,
                "total_shards": 0,
                "created_at": self._now(),
            }
            state["total_shards"] = max(
                1, (len(self._package_items(manifest, mode)) + self.SHARD_SIZE - 1) // self.SHARD_SIZE
            )
            self._write_package_meta(request_dir, manifest, state)
            # 固化本次请求的包内容：续传时按同一份清单重新分片，不受产物后续变化影响。
            self._write_jsonl_replace(request_dir / "items.jsonl", self._package_items(manifest, mode))
            self._write_json(state_path, state)

        # 已交付的同一请求直接返回原回执，不重复打包。
        if state.get("status") == DELIVERED:
            receipt = self._read_json(request_dir / "receipt.json") or {}
            return {
                "ok": True,
                "request_key": request_key,
                "request_id": state["request_id"],
                "duplicate_suppressed": True,
                "resumed": retried,
                "receipt": receipt,
                "message": "同一交付请求已成功交付，返回既有回执",
            }

        package = self._read_json(request_dir / "package.json") or {}
        # 续传优先使用首次请求固化的包内容，保证同一次请求投递的数据前后一致。
        items = self._read_jsonl(request_dir / "items.jsonl") or self._package_items(manifest, mode)
        shards = self._chunk(items, self.SHARD_SIZE)
        state["total_shards"] = len(shards)
        state["status"] = PACKAGED

        start = int(state.get("completed_shards") or 0)
        written_shards = []
        for index in range(start, len(shards)):
            if fail_after_shards is not None and index - start >= fail_after_shards:
                state["status"] = FAILED
                state["failed_at_shard"] = index
                state["updated_at"] = self._now()
                self._write_json(state_path, state)
                receipt = self._build_receipt(state, manifest, package, status=FAILED)
                self._write_json(request_dir / "receipt.json", receipt)
                return {
                    "ok": False,
                    "request_key": request_key,
                    "request_id": state["request_id"],
                    "status": FAILED,
                    "resumed": retried,
                    "completed_shards": start,
                    "total_shards": len(shards),
                    "failed_at_shard": index,
                    "receipt": receipt,
                }
            shard_path = request_dir / f"shard_{index + 1:03d}.jsonl"
            self._write_jsonl_replace(shard_path, shards[index])
            written_shards.append(str(shard_path))
            state["completed_shards"] = index + 1
            state["updated_at"] = self._now()
            self._write_json(state_path, state)

        state["status"] = DELIVERED
        state["delivered_at"] = self._now()
        state["updated_at"] = state["delivered_at"]
        state.pop("failed_at_shard", None)
        self._write_json(state_path, state)

        receipt = self._build_receipt(state, manifest, package, status=DELIVERED)
        self._write_json(request_dir / "receipt.json", receipt)

        # 回写交付任务列表，便于在结果交付页看到这次交付。
        row, _ = self.create_entry({
            "交付编号": f"DELIVER-{request_key}",
            "快照编号": f"SNAP-{doc_id}-{target_version}",
            "交付格式": "jsonl",
            "目标系统": f"接收方:{receiver_id}",
            "字段范围": self._field_scope(mode),
            "记录条数": len(items),
            "分片大小": self.SHARD_SIZE,
            "交付时间": state["delivered_at"],
        })
        if row is not None:
            row["status"] = "已交付"
            row["pending"] = False
            row["request_key"] = request_key

        self._write_checkpoint(f"deliver_request_{request_key}", {
            "request_key": request_key,
            "request_id": state["request_id"],
            "status": DELIVERED,
            "mode": mode,
            "records": len(items),
            "shards": len(shards),
        })

        return {
            "ok": True,
            "request_key": request_key,
            "request_id": state["request_id"],
            "status": DELIVERED,
            "resumed": retried,
            "duplicate_suppressed": False,
            "written_shards": written_shards,
            "package": package,
            "receipt": receipt,
        }

    def resume_delivery(self, request_key: str, *, fail_after_shards: int | None = None) -> dict[str, Any]:
        """恢复同一次交付请求：按 request_key 读取断点，不重新生成请求。"""
        state = self._read_json(self._request_dir(request_key) / "state.json")
        if state is None:
            return {"ok": False, "error": f"交付请求 {request_key} 不存在"}
        return self.deliver_incremental(
            state["doc_id"],
            state["base_version"],
            state["target_version"],
            receiver_id=state["receiver_id"],
            receiver_has_baseline=state["mode"] != "full",
            baseline_verified=state["mode"] == "incremental",
            request_id=state.get("request_id"),
            fail_after_shards=fail_after_shards,
        )

    def verify_package(self, request_key: str, *, baseline_entity_keys: list[str] | None = None) -> dict[str, Any]:
        """目标导入前校验：交付的关系端点是否都能补齐。

        基准不可核实时（closure/full 模式）不接受外部 baseline 声明，
        因此不会把未核实基准里的实体当成已存在。
        """
        request_dir = self._request_dir(request_key)
        state = self._read_json(request_dir / "state.json")
        meta = self._read_json(request_dir / "package.json")
        if state is None or meta is None:
            return {"ok": False, "request_key": request_key, "error": "交付请求或包元数据不存在"}

        entities = self._read_jsonl(request_dir / "entities.jsonl")
        package_entity_keys = {
            "|".join(str(item.get(field, "")) for field in ("entity_type", "entity_text", "chunk_id"))
            for item in entities
        }
        known = set(package_entity_keys)
        baseline_usable = state["mode"] == "incremental"
        if baseline_usable and baseline_entity_keys:
            known |= set(baseline_entity_keys)

        missing = []
        for dependency in meta.get("dependencies", []):
            if dependency.get("status") == "missing":
                missing.append(dependency)
            elif dependency.get("deliver") and dependency.get("natural_key") not in known:
                missing.append(dependency)

        return {
            "ok": not missing,
            "request_key": request_key,
            "mode": state["mode"],
            "baseline_usable": baseline_usable,
            "package_entities": len(package_entity_keys),
            "missing_dependencies": missing,
            "message": "端点依赖可补齐" if not missing else "存在无法补齐的端点依赖，禁止导入",
        }

    def get_receipt(self, request_key: str) -> dict[str, Any]:
        """读取某次交付请求的回执，回执含依赖对象与所属版本。"""
        receipt = self._read_json(self._request_dir(request_key) / "receipt.json")
        if receipt is None:
            return {"ok": False, "error": f"交付请求 {request_key} 无回执"}
        return {"ok": True, "request_key": request_key, "receipt": receipt}

    def list_receipts(self) -> list[dict[str, Any]]:
        """列出全部交付回执摘要，供页面核对。"""
        receipts = []
        for directory in sorted(self._data_dir("deliveries").glob("REQ-*")):
            receipt = self._read_json(directory / "receipt.json")
            if receipt:
                receipts.append(receipt)
        return receipts

    # ------------------------------------------------------------------
    # 包构造与回执
    # ------------------------------------------------------------------

    def _package_items(self, manifest: dict[str, Any], mode: str) -> list[dict[str, Any]]:
        selection = manifest["selection"]
        items: list[dict[str, Any]] = []
        for entry in selection["entities"]:
            items.append({"type": "entity", "role": entry.get("role"), "data": entry["record"]})
        for entry in selection["relations"]:
            items.append({"type": "relation", "role": entry.get("role"), "data": entry["record"]})
        if mode != "full":
            for tombstone in selection["tombstones"]:
                items.append({"type": "tombstone", "data": tombstone})
        return items

    def _write_package_meta(self, request_dir, manifest: dict[str, Any], state: dict[str, Any]) -> None:
        selection = manifest["selection"]
        package = {
            "request_key": state["request_key"],
            "request_id": state["request_id"],
            "doc_id": state["doc_id"],
            "base_version": state["base_version"],
            "target_version": state["target_version"],
            "mode": state["mode"],
            "receiver": manifest["receiver"],
            "baseline": manifest["baseline"],
            "counts": manifest["counts"],
            "summary": manifest["summary"],
            "dependencies": selection["dependencies"],
            "tombstones": selection["tombstones"],
            "record_counts": selection["record_counts"],
            "notes": selection["notes"],
        }
        self._write_json(request_dir / "package.json", package)

        # 把包内记录平铺成可直接导入的文件，便于目标侧按文件读取。
        self._write_jsonl_replace(
            request_dir / "entities.jsonl",
            [entry["record"] for entry in selection["entities"]],
        )
        self._write_jsonl_replace(
            request_dir / "relations.jsonl",
            [entry["record"] for entry in selection["relations"]],
        )
        self._write_jsonl_replace(
            request_dir / "tombstones.jsonl",
            selection["tombstones"] if state["mode"] != "full" else [],
        )

    def _build_receipt(
        self,
        state: dict[str, Any],
        manifest: dict[str, Any],
        package: dict[str, Any],
        *,
        status: str,
    ) -> dict[str, Any]:
        selection = manifest["selection"]
        dependencies = [
            {
                "object_type": item["object_type"],
                "object_id": item["object_id"],
                "natural_key": item["natural_key"],
                "entity_text": item.get("entity_text"),
                "role": item["role"],
                "relation_key": item["relation_key"],
                "version": item["version"],
                "status": item["status"],
                "deliver": item["deliver"],
            }
            for item in selection["dependencies"]
        ]
        missing = [item for item in dependencies if item["status"] == "missing"]
        return {
            "回执编号": f"RCPT-{state['request_key']}",
            "request_key": state["request_key"],
            "request_id": state["request_id"],
            "status": status,
            "doc_id": state["doc_id"],
            "base_version": state["base_version"],
            "target_version": state["target_version"],
            "target_snapshot": f"SNAP-{state['doc_id']}-{state['target_version']}",
            "mode": state["mode"],
            "receiver": manifest["receiver"],
            "baseline": manifest["baseline"],
            "counts": {
                "entities": selection["record_counts"]["entities"],
                "relations": selection["record_counts"]["relations"],
                "tombstones": selection["record_counts"]["tombstones"],
                "dependency_entities": selection["record_counts"]["dependency_entities"],
                "changes": selection["record_counts"]["changes"],
                "package_records": package.get("record_counts", {}),
            },
            "dependencies": dependencies,
            "missing_dependencies": missing,
            "shards": {
                "total": state.get("total_shards", 0),
                "completed": state.get("completed_shards", 0),
                "failed_at": state.get("failed_at_shard"),
            },
            "field_scope": self._field_scope(state["mode"]),
            "created_at": state.get("created_at"),
            "updated_at": state.get("updated_at"),
            "delivered_at": state.get("delivered_at"),
        }

    @staticmethod
    def _field_scope(mode: str) -> str:
        if mode == "full":
            return "实体+关系（首次下载全量）"
        if mode == "closure":
            return "变更实体+变更关系+端点补全实体+删除墓碑"
        return "变更实体+变更关系+删除墓碑"

    # ------------------------------------------------------------------
    # 路径与工具
    # ------------------------------------------------------------------

    def _request_key(
        self, doc_id: str, base_version: str, target_version: str, receiver_id: str, mode: str
    ) -> str:
        raw = f"{doc_id}|{base_version}|{target_version}|{receiver_id}|{mode}"
        return f"REQ-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16].upper()}"

    def _request_dir(self, request_key: str):
        path = self._data_dir("deliveries") / request_key
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def _chunk(items: list[Any], size: int) -> list[list[Any]]:
        if not items:
            return []
        return [items[i:i + size] for i in range(0, len(items), size)]

    @staticmethod
    def _now() -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S")

    def _find_snapshot(self, snapshot_id: str) -> dict[str, Any] | None:
        for vf in sorted(self._data_dir("versions").glob("*.jsonl")):
            for snap in self._read_jsonl(vf):
                if snap.get("快照编号") == snapshot_id:
                    return snap
        return None

    def _collect_deliverables(self, doc_id: str) -> list[dict[str, Any]]:
        """收集指定文档的所有可交付抽取产物。"""
        items = []
        entity_files = sorted(self._data_dir("entities").glob(f"{doc_id}*.jsonl"))
        for ef in entity_files:
            for e in self._read_jsonl(ef):
                items.append({"type": "entity", "source": str(ef), "data": e})

        relation_files = sorted(self._data_dir("relations").glob(f"{doc_id}*.jsonl"))
        for rf in relation_files:
            for r in self._read_jsonl(rf):
                items.append({"type": "relation", "source": str(rf), "data": r})

        vector_files = sorted(self._data_dir("vectors").glob(f"{doc_id}*.jsonl"))
        for vf in vector_files:
            for v in self._read_jsonl(vf):
                items.append({"type": "vector", "source": str(vf), "data": {
                    "chunk_id": v.get("chunk_id"),
                    "doc_id": v.get("doc_id"),
                    "dimension": v.get("vector_dim"),
                    "index": v.get("index"),
                }})

        return items

    def list_formats(self) -> list[dict[str, Any]]:
        """列出所有支持的交付格式。"""
        return [
            {"format": k, "mime": v["mime"], "description": v["description"]}
            for k, v in self.SUPPORTED_FORMATS.items()
        ]
