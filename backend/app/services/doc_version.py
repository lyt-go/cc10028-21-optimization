"""文档版本服务：版本快照、记录级版本差异与版本差异选交清单。

版本快照不只登记产物数量，还把实体、关系记录的身份快照一并落盘（*.records.json 附件）。
因此当两个版本的实体数/关系数完全相同时，仍能定位到具体哪条记录被删除、
哪条关系换了端点，并据此形成"版本差异选交清单"：

* 接收方持有可核实基准 -> 只选交变更记录，未变更依赖按"已存在"处理；
* 接收方持有基准但无法核实 -> 依赖不能视为已存在，端点实体随包补齐；
* 接收方首次下载 -> 包里提供目标版本的完整实体与关系。

每个变动条目都带前后版本与记录身份（record_id），供交付包和导入回执追溯。
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from app.services.base import BaseModuleService

# 每次重新抽取都会变化的字段，不纳入"记录内容是否变化"的比较。
VOLATILE_FIELDS = {
    "recognized_at",
    "extracted_at",
    "embedded_at",
    "ingested_at",
    "generated_at",
    "生成时间",
    "采集时间",
    "交付时间",
}

# 记录身份字段，单独作为身份变更提示，不直接算作内容修改。
IDENTITY_FIELDS = {"entity_id", "relation_id", "id", "triple_id"}

INTERNAL_PREFIX = "_"


def _norm(value: Any) -> Any:
    """把数字字符串与数字统一，避免 "36" 与 36 被误判为差异。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        try:
            return float(text)
        except ValueError:
            return text
    return value


def _plain_record(record: dict[str, Any]) -> dict[str, Any]:
    """去掉仅供内部比对的以下划线开头的键，得到可交付的原始记录。"""
    return {k: v for k, v in record.items() if not str(k).startswith(INTERNAL_PREFIX)}


class DocVersionService(BaseModuleService):
    MODULE = "doc_version"
    ENTITY = "版本快照"
    OPERATION_TERM = "归档"
    LIST_FIELDS = ["快照编号", "文档编号", "版本号", "变更摘要", "分块数量", "实体数量", "生成方式", "生成时间"]
    REQUIRED_FIELDS = ["快照编号", "文档编号", "版本号"]
    STATUS_ORDER = ["草稿", "待确认", "已发布", "已归档"]
    ACTION_RULES = {"提交确认": "待确认", "发布快照": "已发布", "标记归档": "已归档"}
    NEGATIVE_ACTIONS = []

    # ------------------------------------------------------------------
    # 快照创建
    # ------------------------------------------------------------------

    def create_snapshot(self, doc_id: str, version: str = "v1.0") -> dict[str, Any]:
        """为指定文档创建版本快照，汇总产物统计并落盘记录级身份快照。"""
        parsed = self._read_jsonl(self._data_dir("parsed") / f"{doc_id}_parsed.jsonl")
        chunks = self._read_jsonl(self._data_dir("chunks") / f"{doc_id}_parsed_chunks.jsonl")
        entities_raw = self._read_jsonl(self._data_dir("entities") / f"{doc_id}_parsed_entities.jsonl")
        relations_raw = self._read_jsonl(self._data_dir("relations") / f"{doc_id}_parsed_relations.jsonl")
        graph = self._read_jsonl(self._data_dir("graph") / f"{doc_id}_parsed_graph.jsonl")

        pages_count = len(parsed)
        chunk_count = len(chunks)
        triple_count = len(graph)

        entities = self._normalize_entities(entities_raw)
        relations = self._normalize_relations(relations_raw, entities)
        # 计数口径与记录级快照保持一致（按身份去重后的条数）。
        entity_count = len(entities)
        relation_count = len(relations)

        snapshot_id = f"SNAP-{doc_id}-{version}"
        output_path = self._snapshot_path(doc_id, version)
        created_at = time.strftime("%Y-%m-%d %H:%M:%S")
        records = {
            "snapshot_id": snapshot_id,
            "doc_id": doc_id,
            "version": version,
            "created_at": created_at,
            "counts": {
                "entities": entity_count,
                "relations": relation_count,
                "chunks": chunk_count,
                "triples": triple_count,
            },
            "entities": entities,
            "relations": relations,
        }

        # 记录级快照附件：差异查询只读这里，不再依赖会被覆盖的当前产物目录。
        records_path = self._records_path(doc_id, version)
        self._write_json(records_path, records)

        # 同一版本重复归档时覆盖旧文件，避免追加产生重复快照行。
        snapshot = {
            "快照编号": snapshot_id,
            "文档编号": doc_id,
            "版本号": version,
            "变更摘要": "",
            "分块数量": chunk_count,
            "实体数量": entity_count,
            "关系数量": relation_count,
            "生成方式": "自动",
            "生成时间": created_at,
        }
        previous = self._previous_records(doc_id, version)
        snapshot["变更摘要"] = self._build_change_summary(previous, records)
        self._write_jsonl_replace(output_path, [snapshot])

        self._write_checkpoint(f"version_{doc_id}_{version}", {
            "doc_id": doc_id,
            "version": version,
            "pages": pages_count,
            "chunks": chunk_count,
            "entities": entity_count,
            "relations": relation_count,
            "triples": triple_count,
            "change_summary": snapshot["变更摘要"],
            "records_file": str(records_path),
        })

        # 跨模块触发：结果交付（默认整包，接收方可在交付时另行选择增量模式）
        triggers = {}
        trig_deliver = self._trigger_result_deliver(snapshot["快照编号"])
        triggers["result_deliver"] = trig_deliver

        return {
            "snapshot": snapshot,
            "doc_id": doc_id,
            "version": version,
            "pages": pages_count,
            "chunks": chunk_count,
            "entities": entity_count,
            "relations": relation_count,
            "triples": triple_count,
            "records_file": str(records_path),
            "triggers": triggers,
        }

    def _build_change_summary(
        self, previous: dict[str, Any] | None, current: dict[str, Any]
    ) -> str:
        """对比上一个版本快照，生成能体现"计数未变但记录变化"的摘要。"""
        if not previous:
            return "初始版本快照"

        changes: list[str] = []
        prev_counts = previous.get("counts", {})
        cur_counts = current.get("counts", {})
        for label, key in (("分块数", "chunks"), ("实体数", "entities"),
                           ("关系数", "relations"), ("三元组数", "triples")):
            if prev_counts.get(key, 0) != cur_counts.get(key, 0):
                changes.append(f"{label}: {prev_counts.get(key, 0)} → {cur_counts.get(key, 0)}")

        diff = self._diff_records(
            previous, current, previous.get("version", ""), current.get("version", "")
        )
        if diff["entries"]:
            stats = diff["summary"]["by_change"]
            detail = (
                f"记录变化: 新增{stats['added']}、删除{stats['removed']}、修改{stats['modified']}"
            )
            if diff["counts_equal"]:
                changes.append(f"计数未变但{detail}")
            else:
                changes.append(detail)

        return "; ".join(changes) if changes else "无变化"

    # ------------------------------------------------------------------
    # 版本差异查询
    # ------------------------------------------------------------------

    def compare_versions(
        self,
        doc_id: str,
        version_a: str,
        version_b: str,
        *,
        receiver_id: str = "default",
        receiver_has_baseline: bool = True,
        baseline_verified: bool = True,
    ) -> dict[str, Any]:
        """比较两个版本，返回记录级差异与版本差异选交清单。

        receiver_* 描述接收方基准情况，决定选交清单里"哪些数据必须进包"。
        """
        snap_a = self._load_snapshot(doc_id, version_a)
        snap_b = self._load_snapshot(doc_id, version_b)
        if snap_a is None or snap_b is None:
            return {"error": "版本快照不完整，至少一个版本不存在"}

        records_a = self._load_records(doc_id, version_a)
        records_b = self._load_records(doc_id, version_b)

        # 兼容旧快照：没有 records 附件时只能返回数量差。
        if records_a is None or records_b is None:
            diffs = self._count_diffs(snap_a, snap_b)
            return {
                "doc_id": doc_id,
                "version_a": version_a,
                "version_b": version_b,
                "diffs": diffs,
                "changed_fields": list(diffs.keys()),
                "record_diff_available": False,
                "message": "版本快照缺少记录级附件，仅能比较数量差；请用新版本重新创建快照",
                "entries": [],
                "summary": self._empty_summary(),
                "selection": None,
            }

        diff = self._diff_records(records_a, records_b, version_a, version_b)
        receiver = {
            "receiver_id": receiver_id,
            "has_baseline": bool(receiver_has_baseline),
            "baseline_verified": bool(baseline_verified),
            "baseline_version": version_a if receiver_has_baseline else None,
        }
        selection = self._build_selection(
            version_a, version_b, records_a, records_b, diff["entries"], receiver
        )

        diffs = self._count_diffs(snap_a, snap_b)
        return {
            "doc_id": doc_id,
            "version_a": version_a,
            "version_b": version_b,
            # 兼容旧接口字段
            "diffs": diffs,
            "changed_fields": list(diffs.keys()),
            # 记录级差异
            "record_diff_available": True,
            "counts": diff["counts"],
            "counts_equal": diff["counts_equal"],
            "equal_count_data_change": diff["equal_count_data_change"],
            "entries": diff["entries"],
            "identity_reassigned": diff["identity_reassigned"],
            "summary": diff["summary"],
            # 选交清单
            "receiver": receiver,
            "mode": selection["mode"],
            "baseline": selection["baseline"],
            "selection": selection,
        }

    def build_selection_manifest(
        self,
        doc_id: str,
        version_a: str,
        version_b: str,
        *,
        receiver_id: str = "default",
        receiver_has_baseline: bool = True,
        baseline_verified: bool = True,
    ) -> dict[str, Any]:
        """只返回版本差异选交清单，供结果交付构造增量数据包。"""
        result = self.compare_versions(
            doc_id,
            version_a,
            version_b,
            receiver_id=receiver_id,
            receiver_has_baseline=receiver_has_baseline,
            baseline_verified=baseline_verified,
        )
        if "error" in result:
            return result
        return {
            "doc_id": doc_id,
            "base_version": version_a,
            "target_version": version_b,
            "record_diff_available": result["record_diff_available"],
            "counts": result["counts"],
            "counts_equal": result["counts_equal"],
            "equal_count_data_change": result["equal_count_data_change"],
            "summary": result["summary"],
            "receiver": result["receiver"],
            "baseline": result["baseline"],
            "mode": result["mode"],
            "selection": result["selection"],
            "entries": result["entries"],
        }

    def list_versions(self, doc_id: str) -> dict[str, Any]:
        """列出某文档的所有版本快照及记录级附件状态。"""
        versions = []
        for path in self._sorted_version_paths(doc_id):
            data = self._read_jsonl(path)
            if not data:
                continue
            snap = data[0]
            records_path = path.with_suffix(".records.json")
            versions.append({
                "快照编号": snap.get("快照编号"),
                "文档编号": snap.get("文档编号"),
                "版本号": snap.get("版本号"),
                "实体数量": snap.get("实体数量"),
                "关系数量": snap.get("关系数量"),
                "变更摘要": snap.get("变更摘要"),
                "records_available": records_path.exists(),
                "file": str(path),
            })
        return {"doc_id": doc_id, "versions": versions, "count": len(versions)}

    # ------------------------------------------------------------------
    # 记录级差异核心
    # ------------------------------------------------------------------

    def _diff_records(
        self,
        records_a: dict[str, Any],
        records_b: dict[str, Any],
        version_a: str,
        version_b: str,
    ) -> dict[str, Any]:
        entries: list[dict[str, Any]] = []
        identity_reassigned = 0
        unchanged = 0

        for object_type, field in (("entity", "entities"), ("relation", "relations")):
            old_items = records_a.get(field, []) or []
            new_items = records_b.get(field, []) or []
            old_by_key = {item["_natural_key"]: item for item in old_items}
            new_by_key = {item["_natural_key"]: item for item in new_items}
            old_by_id = {item["_record_id"]: item for item in old_items if item.get("_record_id")}

            for key in sorted(set(old_by_key) | set(new_by_key)):
                old = old_by_key.get(key)
                new = new_by_key.get(key)
                if old is not None and new is not None:
                    changed_fields = self._changed_fields(old, new)
                    reassigned = old.get("_record_id") != new.get("_record_id")
                    if reassigned:
                        identity_reassigned += 1
                    # 内容没变、仅记录编号被重排时仍作为"修改"条目出现，
                    # 让接收方能把旧记录身份映射到新身份。
                    if not changed_fields and not reassigned:
                        unchanged += 1
                        continue
                    entries.append(self._entry(
                        object_type, "modified", key, version_a, old, version_b, new,
                        changed_fields=changed_fields,
                        identity_reassigned=reassigned,
                    ))
                elif old is not None:
                    entries.append(self._entry(
                        object_type, "removed", key, version_a, old, version_b, None,
                    ))
                else:
                    entries.append(self._entry(
                        object_type, "added", key, version_a, None, version_b, new,
                    ))

            # 记录编号被复用但身份已换：给删除/新增条目互相标注，便于追溯记录身份。
            added_by_id = {
                e["after"]["record_id"]: e for e in entries
                if e["object_type"] == object_type and e["change"] == "added"
            }
            removed_by_id = {
                e["before"]["record_id"]: e for e in entries
                if e["object_type"] == object_type and e["change"] == "removed"
            }
            for record_id, added in added_by_id.items():
                old = old_by_id.get(record_id)
                if old is not None and old.get("_natural_key") != added["natural_key"]:
                    added["replaces"] = {
                        "record_id": record_id,
                        "natural_key": old.get("_natural_key"),
                        "version": version_a,
                    }
                    removed = removed_by_id.get(record_id)
                    if removed is not None:
                        removed["replaced_by"] = {
                            "record_id": added["after"]["record_id"],
                            "natural_key": added["natural_key"],
                            "version": version_b,
                        }

        by_change = {"added": 0, "removed": 0, "modified": 0}
        by_object = {
            "entity": {"added": 0, "removed": 0, "modified": 0},
            "relation": {"added": 0, "removed": 0, "modified": 0},
        }
        for entry in entries:
            by_change[entry["change"]] += 1
            by_object[entry["object_type"]][entry["change"]] += 1

        counts_a = records_a.get("counts", {})
        counts_b = records_b.get("counts", {})
        counts = {
            "base": {
                "entities": counts_a.get("entities", len(records_a.get("entities", []))),
                "relations": counts_a.get("relations", len(records_a.get("relations", []))),
            },
            "target": {
                "entities": counts_b.get("entities", len(records_b.get("entities", []))),
                "relations": counts_b.get("relations", len(records_b.get("relations", []))),
            },
        }
        counts_equal = (
            counts["base"]["entities"] == counts["target"]["entities"]
            and counts["base"]["relations"] == counts["target"]["relations"]
        )

        return {
            "entries": entries,
            "identity_reassigned": identity_reassigned,
            "counts": counts,
            "counts_equal": counts_equal,
            "equal_count_data_change": bool(counts_equal and entries),
            "summary": {
                "total_entries": len(entries),
                "unchanged": unchanged,
                "by_change": by_change,
                "by_object": by_object,
                "counts_equal": counts_equal,
                "equal_count_data_change": bool(counts_equal and entries),
            },
        }

    def _entry(
        self,
        object_type: str,
        change: str,
        key: str,
        version_a: str,
        old: dict[str, Any] | None,
        version_b: str,
        new: dict[str, Any] | None,
        *,
        changed_fields: list[str] | None = None,
        identity_reassigned: bool = False,
    ) -> dict[str, Any]:
        return {
            "change_id": f"{object_type}:{change}:{key}",
            "object_type": object_type,
            "change": change,
            "natural_key": key,
            "changed_fields": changed_fields or [],
            "identity_reassigned": identity_reassigned,
            "before": {
                "version": version_a,
                "exists": old is not None,
                "record_id": old.get("_record_id") if old else None,
                "record": _plain_record(old) if old else None,
            },
            "after": {
                "version": version_b,
                "exists": new is not None,
                "record_id": new.get("_record_id") if new else None,
                "record": _plain_record(new) if new else None,
            },
        }

    def _changed_fields(self, old: dict[str, Any], new: dict[str, Any]) -> list[str]:
        keys = (set(old) | set(new)) - VOLATILE_FIELDS - IDENTITY_FIELDS
        changed = []
        for key in sorted(keys):
            if str(key).startswith(INTERNAL_PREFIX):
                continue
            if _norm(old.get(key)) != _norm(new.get(key)):
                changed.append(key)
        return changed

    # ------------------------------------------------------------------
    # 选交清单
    # ------------------------------------------------------------------

    def _build_selection(
        self,
        version_a: str,
        version_b: str,
        records_a: dict[str, Any],
        records_b: dict[str, Any],
        entries: list[dict[str, Any]],
        receiver: dict[str, Any],
    ) -> dict[str, Any]:
        has_baseline = receiver["has_baseline"] and bool(receiver.get("baseline_version"))
        verified = has_baseline and receiver["baseline_verified"]
        notes: list[str] = []
        if not has_baseline:
            mode = "full"
            baseline = {
                "status": "absent",
                "usable": False,
                "version": None,
                "notes": ["接收方首次下载，包内提供目标版本完整实体与关系，不依赖任何既有基准"],
            }
            notes.append("首次下载：不做基准依赖判断，实体与关系全量入包")
        elif not verified:
            mode = "closure"
            baseline = {
                "status": "unverified",
                "usable": False,
                "version": receiver.get("baseline_version"),
                "notes": ["基准无法核实，关系端点依赖不能视为已存在，端点实体随包补齐"],
            }
            notes.append("基准不可核实：已交付关系的端点实体必须进包，除非它在本次变更里")
        else:
            mode = "incremental"
            baseline = {
                "status": "verified",
                "usable": True,
                "version": receiver.get("baseline_version"),
                "notes": ["基准可读，仅选交变更记录；未变更端点按基准已存在处理"],
            }
            notes.append("基准可核实：未变更的端点实体留在基准侧，不进增量包")

        target_entities = records_b.get("entities", []) or []
        target_relations = records_b.get("relations", []) or []
        base_entity_keys = {item["_natural_key"] for item in (records_a.get("entities") or [])}
        target_entity_by_key = {item["_natural_key"]: item for item in target_entities}
        target_entity_by_text: dict[str, dict[str, Any]] = {}
        for item in target_entities:
            target_entity_by_text.setdefault(item.get("entity_text", ""), item)

        selected_entities: dict[str, dict[str, Any]] = {}
        selected_relations: dict[str, dict[str, Any]] = {}
        tombstones: list[dict[str, Any]] = []

        def select_entity(item: dict[str, Any], role: str, reason: str) -> None:
            selected_entities.setdefault(
                item["_natural_key"], {"record": item, "role": role, "reason": reason}
            )

        def select_relation(item: dict[str, Any], role: str, reason: str) -> None:
            selected_relations.setdefault(
                item["_natural_key"], {"record": item, "role": role, "reason": reason}
            )

        if mode == "full":
            for item in target_entities:
                select_entity(item, "snapshot", "首次下载：完整实体集")
            for item in target_relations:
                select_relation(item, "snapshot", "首次下载：完整关系集")
        else:
            for entry in entries:
                key = entry["natural_key"]
                if entry["object_type"] == "entity":
                    if entry["change"] in ("added", "modified"):
                        item = target_entity_by_key.get(key)
                        if item is not None:
                            select_entity(item, "changed", f"实体{entry['change']}")
                    else:
                        tombstones.append(
                            self._tombstone("entity", entry["natural_key"], entry["before"])
                        )
                elif entry["change"] in ("added", "modified"):
                    target_relation = next(
                        (r for r in target_relations if r["_natural_key"] == key), None
                    )
                    if target_relation is not None:
                        select_relation(
                            target_relation,
                            "changed",
                            "新增关系" if entry["change"] == "added" else "关系端点或内容变化",
                        )
                else:
                    tombstones.append(
                        self._tombstone("relation", entry["natural_key"], entry["before"])
                    )

        # 关系端点解析：交付的每条关系都必须能在目标侧补齐端点。
        dependencies: list[dict[str, Any]] = []
        for relation in list(selected_relations.values()):
            record = relation["record"]
            for role, key_field, text_field in (
                ("head", "_head_key", "head_entity"),
                ("tail", "_tail_key", "tail_entity"),
            ):
                dep_key = record.get(key_field) or ""
                endpoint = target_entity_by_key.get(dep_key) or target_entity_by_text.get(
                    record.get(text_field, "")
                )
                if endpoint is None:
                    dependencies.append({
                        "object_type": "entity",
                        "object_id": None,
                        "natural_key": dep_key or f"::{record.get(text_field, '')}",
                        "entity_text": record.get(text_field, ""),
                        "role": role,
                        "relation_key": record["_natural_key"],
                        "version": version_b,
                        "status": "missing",
                        "deliver": False,
                        "reason": "关系端点在目标版本中不存在，无法补齐",
                    })
                    notes.append(
                        f"关系 {record['_natural_key']} 的 {role} 端点"
                        f"「{record.get(text_field, '')}」在目标版本缺失"
                    )
                    continue

                endpoint_key = endpoint["_natural_key"]
                if endpoint_key in selected_entities:
                    status = "included"
                    dependency_version = version_b
                elif mode == "incremental" and endpoint_key in base_entity_keys:
                    status = "existing"
                    dependency_version = version_a
                else:
                    status = "required"
                    dependency_version = version_b
                    select_entity(endpoint, "dependency", "关系端点依赖补全")
                    notes.append(
                        f"关系 {record['_natural_key']} 的 {role} 端点"
                        f"「{endpoint.get('entity_text', '')}」无法按基准视为已存在，已随包补齐"
                    )
                dependencies.append({
                    "object_type": "entity",
                    "object_id": endpoint.get("_record_id"),
                    "natural_key": endpoint_key,
                    "entity_text": endpoint.get("entity_text", ""),
                    "role": role,
                    "relation_key": record["_natural_key"],
                    "version": dependency_version,
                    "status": status,
                    "deliver": status in ("included", "required"),
                })

        items: list[dict[str, Any]] = []
        for entry in entries:
            if entry["object_type"] == "entity":
                deliver = entry["natural_key"] in selected_entities
            else:
                deliver = entry["natural_key"] in selected_relations
            items.append({
                "item_type": "change",
                "object_type": entry["object_type"],
                "change": entry["change"],
                "natural_key": entry["natural_key"],
                "changed_fields": entry["changed_fields"],
                "before_version": entry["before"]["version"],
                "before_record_id": entry["before"]["record_id"],
                "after_version": entry["after"]["version"],
                "after_record_id": entry["after"]["record_id"],
                "deliver": deliver,
                "reason": self._change_reason(entry, deliver),
            })

        for tombstone in tombstones:
            items.append({
                "item_type": "tombstone",
                "object_type": tombstone["object_type"],
                "change": "removed",
                "natural_key": tombstone["natural_key"],
                "before_version": tombstone["version"],
                "before_record_id": tombstone.get("record_id"),
                "after_version": version_b,
                "after_record_id": None,
                "deliver": True,
                "reason": "旧记录已删除，增量包通知目标侧移除",
            })

        for key in sorted(selected_entities):
            value = selected_entities[key]
            if value["role"] != "dependency":
                continue
            item = value["record"]
            items.append({
                "item_type": "dependency",
                "object_type": "entity",
                "change": "unchanged",
                "natural_key": key,
                "before_version": version_a,
                "before_record_id": item.get("_record_id"),
                "after_version": version_b,
                "after_record_id": item.get("_record_id"),
                "deliver": True,
                "reason": value["reason"],
            })

        if not has_baseline and tombstones:
            notes.append("首次下载模式不产生删除墓碑，接收方本来就没有旧数据")

        return {
            "mode": mode,
            "baseline": baseline,
            "notes": notes,
            "items": items,
            "entities": [
                {
                    "record": _plain_record(value["record"]),
                    "natural_key": key,
                    "role": value["role"],
                    "reason": value["reason"],
                    "version": version_b if value["role"] != "dependency" else version_a,
                }
                for key, value in sorted(selected_entities.items())
            ],
            "relations": [
                {
                    "record": _plain_record(value["record"]),
                    "natural_key": key,
                    "role": value["role"],
                    "reason": value["reason"],
                    "version": version_b,
                }
                for key, value in sorted(selected_relations.items())
            ],
            "tombstones": tombstones,
            "dependencies": dependencies,
            "record_counts": {
                "entities": len(selected_entities),
                "relations": len(selected_relations),
                "tombstones": len(tombstones),
                "dependency_entities": sum(
                    1 for value in selected_entities.values() if value["role"] == "dependency"
                ),
                "changes": len(entries),
            },
        }

    @staticmethod
    def _change_reason(entry: dict[str, Any], deliver: bool) -> str:
        if deliver:
            return "变更记录进入交付包"
        if entry["change"] == "removed":
            return "删除记录以墓碑形式通知"
        return "不进入交付包"

    def _tombstone(
        self, object_type: str, natural_key: str, before: dict[str, Any]
    ) -> dict[str, Any]:
        record = before.get("record") or {}
        keys = (
            ("entity_id", "entity_type", "entity_text")
            if object_type == "entity"
            else ("relation_id", "head_entity", "relation_type", "tail_entity")
        )
        return {
            "object_type": object_type,
            "natural_key": natural_key,
            "record_id": before.get("record_id"),
            "version": before.get("version"),
            "record": {key: record.get(key) for key in keys if key in record},
        }

    # ------------------------------------------------------------------
    # 快照与记录规范化
    # ------------------------------------------------------------------

    def _normalize_entities(self, raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """给实体补上稳定身份键，并按身份键去重，供跨版本按内容对齐。

        产物文件是追加写入的，重复执行抽取会在同一文件里留下重复行；
        快照只保留每个身份的最后一条，保证计数与差异比较口径一致。
        """
        by_key: dict[str, dict[str, Any]] = {}
        for item in raw:
            natural_key = "|".join(str(item.get(field, "")) for field in (
                "entity_type", "entity_text", "chunk_id"
            ))
            enriched = dict(item)
            enriched["_natural_key"] = natural_key
            enriched["_record_id"] = str(item.get("entity_id") or "")
            by_key[natural_key] = enriched
        return sorted(by_key.values(), key=lambda e: e["_natural_key"])

    def _normalize_relations(
        self, raw: list[dict[str, Any]], entities: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """给关系补上稳定身份键与端点实体键，并按身份键去重。"""
        by_text: dict[str, str] = {}
        for entity in entities:
            by_text.setdefault(str(entity.get("entity_text", "")), entity["_natural_key"])
        by_key: dict[str, dict[str, Any]] = {}
        for item in raw:
            natural_key = "|".join(str(item.get(field, "")) for field in (
                "head_entity", "relation_type", "tail_entity"
            ))
            enriched = dict(item)
            enriched["_natural_key"] = natural_key
            enriched["_record_id"] = str(item.get("relation_id") or "")
            enriched["_head_key"] = by_text.get(str(item.get("head_entity", "")), "")
            enriched["_tail_key"] = by_text.get(str(item.get("tail_entity", "")), "")
            by_key[natural_key] = enriched
        return sorted(by_key.values(), key=lambda r: r["_natural_key"])

    def _previous_records(self, doc_id: str, version: str) -> dict[str, Any] | None:
        """按版本号顺序取当前版本之前的最近一份记录级快照。"""
        current_key = self._version_sort_key(self._snapshot_path(doc_id, version))
        previous_path: Path | None = None
        for path in self._sorted_version_paths(doc_id):
            if self._version_sort_key(path) >= current_key:
                break
            previous_path = path
        if previous_path is None:
            return None
        return self._read_json(previous_path.with_suffix(".records.json"))

    def _load_snapshot(self, doc_id: str, version: str) -> dict[str, Any] | None:
        data = self._read_jsonl(self._snapshot_path(doc_id, version))
        if data:
            return data[0]
        # 兼容已发布副本或旧命名：按文档+版本在 versions 目录里回查。
        for path in sorted(self._data_dir("versions").glob("*.jsonl")):
            for snap in self._read_jsonl(path):
                if snap.get("文档编号") == doc_id and snap.get("版本号") == version:
                    return snap
        snapshot_id = f"SNAP-{doc_id}-{version}"
        for path in sorted(self._data_dir("versions").glob(f"{snapshot_id}_published.jsonl")):
            data = self._read_jsonl(path)
            if data:
                return data[0]
        return None

    def _load_records(self, doc_id: str, version: str) -> dict[str, Any] | None:
        return self._read_json(self._records_path(doc_id, version))

    def _sorted_version_paths(self, doc_id: str) -> list[Path]:
        """按版本号自然排序（v2.0 在 v10.0 之前），排除已发布副本。"""
        paths = [
            p for p in self._data_dir("versions").glob(f"{doc_id}_v*.jsonl")
            if "_published" not in p.name
        ]
        return sorted(paths, key=self._version_sort_key)

    @staticmethod
    def _version_sort_key(path: Path) -> tuple[Any, ...]:
        stem = path.name
        marker = "_v"
        index = stem.rfind(marker)
        version = stem[index + len(marker):].removesuffix(".jsonl") if index >= 0 else stem
        parts: list[Any] = []
        for chunk in re.split(r"(\d+)", version):
            if not chunk:
                continue
            parts.append(int(chunk) if chunk.isdigit() else chunk)
        return tuple(parts)

    def _snapshot_path(self, doc_id: str, version: str) -> Path:
        return self._data_dir("versions") / f"{doc_id}_v{version}.jsonl"

    def _records_path(self, doc_id: str, version: str) -> Path:
        return self._data_dir("versions") / f"{doc_id}_v{version}.records.json"

    @staticmethod
    def _count_diffs(snap_a: dict[str, Any], snap_b: dict[str, Any]) -> dict[str, Any]:
        diffs: dict[str, Any] = {}
        for key in ("分块数量", "实体数量", "关系数量"):
            if key not in snap_a and key not in snap_b:
                continue
            va = snap_a.get(key, 0)
            vb = snap_b.get(key, 0)
            if isinstance(va, str):
                va = int(va) if va.isdigit() else 0
            if isinstance(vb, str):
                vb = int(vb) if vb.isdigit() else 0
            if va != vb:
                diffs[key] = {"from": va, "to": vb, "delta": vb - va}
        return diffs

    @staticmethod
    def _empty_summary() -> dict[str, Any]:
        return {
            "total_entries": 0,
            "unchanged": 0,
            "by_change": {"added": 0, "removed": 0, "modified": 0},
            "by_object": {
                "entity": {"added": 0, "removed": 0, "modified": 0},
                "relation": {"added": 0, "removed": 0, "modified": 0},
            },
            "counts_equal": False,
            "equal_count_data_change": False,
        }

    # ------------------------------------------------------------------
    # 快照发布
    # ------------------------------------------------------------------

    def publish_snapshot(self, snapshot_id: str) -> dict[str, Any]:
        """发布指定版本快照到目标系统，将快照状态更新为'已发布'。"""
        for vf in sorted(self._data_dir("versions").glob("*.jsonl")):
            for snap in self._read_jsonl(vf):
                if snap.get("快照编号") == snapshot_id:
                    snap["status"] = "已发布"
                    output_path = self._data_dir("versions") / f"{snapshot_id}_published.jsonl"
                    self._write_jsonl_replace(output_path, [snap])
                    return {
                        "snapshot_id": snapshot_id,
                        "status": "已发布",
                        "output": str(output_path),
                    }
        return {"error": f"版本快照 {snapshot_id} 不存在"}
