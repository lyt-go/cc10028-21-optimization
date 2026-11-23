"""版本差异选交与增量交付测试：等计数快照、基准三态、断点续传与回执依赖。

场景固定为"实体数/关系数不变，但删除旧实体并把关系端点换掉"，
用来证明差异查询不是只看计数的。
"""
import json
import os
import sys
import uuid

import pytest

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND_DIR)

from app.services.base import DATA_ROOT
from app.services.chunk_split import ChunkSplitService
from app.services.doc_version import DocVersionService
from app.services.entity_recognize import EntityRecognizeService
from app.services.format_parse import FormatParseService
from app.services.kg_build import KgBuildService
from app.services.relation_extract import RelationExtractService
from app.services.result_deliver import DELIVERED, FAILED, ResultDeliverService

OLD_ENTITY = "首席科学家"


def _uid(prefix="DV"):
    return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"


def _write(records_path, records):
    with open(records_path, "w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _prepare_snapshot(doc_id: str, version: str = "v1.0") -> dict:
    task, _ = FormatParseService().create_entry({
        "任务编号": _uid("PR"), "文档编号": doc_id, "解析器类型": "PDF",
    })
    FormatParseService().execute_parse(task["id"])
    ChunkSplitService().split_from_document(f"{doc_id}_parsed")
    EntityRecognizeService().recognize_from_chunk(f"{doc_id}_parsed")
    RelationExtractService().extract_from_entities(f"{doc_id}_parsed")
    KgBuildService().build_from_relations(f"{doc_id}_parsed")
    return DocVersionService().create_snapshot(doc_id, version)


def _dedupe(records, fields):
    """产物文件是追加写入的，测试里先按身份键去重再改数据。"""
    seen = {}
    for record in records:
        key = tuple(str(record.get(field, "")) for field in fields)
        seen[key] = record
    return list(seen.values())


def _rewrite_equal_count_artifacts(doc_id: str) -> None:
    """删除旧实体，用同数量新实体顶替，并把相关关系端点换到新实体上。"""
    entities_path = DATA_ROOT / "entities" / f"{doc_id}_parsed_entities.jsonl"
    relations_path = DATA_ROOT / "relations" / f"{doc_id}_parsed_relations.jsonl"
    entities = _dedupe(_read(entities_path), ("entity_type", "entity_text", "chunk_id"))
    relations = _dedupe(_read(relations_path), ("head_entity", "relation_type", "tail_entity"))

    removed = [e for e in entities if e["entity_text"] == OLD_ENTITY]
    assert removed, "测试前置数据里应存在被删除的实体"
    kept = [e for e in entities if e["entity_text"] != OLD_ENTITY]

    replacements = []
    for index, original in enumerate(removed):
        replacement = dict(original)
        replacement["entity_id"] = f"ENT-9{index:05d}"
        replacement["entity_text"] = f"替补{index}博士"
        kept.append(replacement)
        replacements.append(replacement["entity_text"])

    rewritten_relations = []
    cursor = 0
    for relation in relations:
        updated = dict(relation)
        if updated["tail_entity"] == OLD_ENTITY:
            updated["tail_entity"] = replacements[cursor % len(replacements)]
            cursor += 1
        rewritten_relations.append(updated)

    _write(entities_path, kept)
    _write(relations_path, rewritten_relations)


@pytest.fixture()
def equal_count_versions():
    doc_id = _uid("EQ")
    _prepare_snapshot(doc_id, "v1.0")
    _rewrite_equal_count_artifacts(doc_id)
    DocVersionService().create_snapshot(doc_id, "v2.0")
    return doc_id


class TestEqualCountVersionDiff:
    def test_counts_equal_but_records_changed(self, equal_count_versions):
        result = DocVersionService().compare_versions(equal_count_versions, "v1.0", "v2.0")

        assert result["counts_equal"] is True
        assert result["equal_count_data_change"] is True
        assert result["diffs"] == {} or all(
            value["from"] == value["to"] for value in result["diffs"].values()
        )
        assert result["summary"]["total_entries"] > 0
        assert result["summary"]["by_change"]["removed"] > 0
        assert result["summary"]["by_change"]["added"] > 0

    def test_each_entry_has_before_and_after_identity(self, equal_count_versions):
        result = DocVersionService().compare_versions(equal_count_versions, "v1.0", "v2.0")

        removed_entities = [
            entry for entry in result["entries"]
            if entry["object_type"] == "entity" and entry["change"] == "removed"
        ]
        assert removed_entities, "应定位到被删除的实体"
        for entry in result["entries"]:
            assert entry["before"]["version"] == "v1.0"
            assert entry["after"]["version"] == "v2.0"
            if entry["change"] in ("added", "modified"):
                assert entry["after"]["exists"] is True
                assert entry["after"]["record_id"]
            if entry["change"] in ("removed", "modified"):
                assert entry["before"]["exists"] is True
                assert entry["before"]["record_id"]

    def test_endpoint_swap_surfaces_as_relation_change(self, equal_count_versions):
        result = DocVersionService().compare_versions(equal_count_versions, "v1.0", "v2.0")
        relation_changes = [
            entry for entry in result["entries"] if entry["object_type"] == "relation"
        ]
        assert relation_changes
        swapped = [
            entry for entry in relation_changes
            if OLD_ENTITY in (entry["before"]["record"] or {}).get("tail_entity", "")
        ]
        assert swapped, "换端点的关系必须作为差异条目出现"
        assert all(entry["change"] == "removed" for entry in swapped)

    def test_change_summary_mentions_equal_count_change(self, equal_count_versions):
        snapshot = DocVersionService()._read_jsonl(
            DocVersionService()._snapshot_path(equal_count_versions, "v2.0")
        )[0]
        assert "计数未变" in snapshot["变更摘要"]
        assert "删除" in snapshot["变更摘要"]

    def test_legacy_snapshot_without_records_reports_counts_only(self, equal_count_versions):
        records_path = DocVersionService()._records_path(equal_count_versions, "v2.0")
        records_path.unlink()
        result = DocVersionService().compare_versions(equal_count_versions, "v1.0", "v2.0")
        assert result["record_diff_available"] is False
        assert result["entries"] == []
        assert "缺少记录级附件" in result["message"]


class TestSelectionByReceiverBaseline:
    def test_verified_baseline_only_ships_changes(self, equal_count_versions):
        result = DocVersionService().compare_versions(
            equal_count_versions, "v1.0", "v2.0",
            receiver_id="alpha", receiver_has_baseline=True, baseline_verified=True,
        )
        selection = result["selection"]
        assert result["mode"] == "incremental"
        assert result["baseline"]["status"] == "verified"
        dependency_roles = {
            entry["role"] for entry in selection["entities"] if entry["role"] == "dependency"
        }
        assert not dependency_roles, "基准可核实时不应重复投递未变更端点"
        assert all(
            dep["status"] in ("existing", "included")
            for dep in selection["dependencies"]
        )

    def test_unverified_baseline_ships_endpoint_entities(self, equal_count_versions):
        result = DocVersionService().compare_versions(
            equal_count_versions, "v1.0", "v2.0",
            receiver_id="beta", receiver_has_baseline=True, baseline_verified=False,
        )
        selection = result["selection"]
        assert result["mode"] == "closure"
        assert result["baseline"]["status"] == "unverified"
        dependencies = [entry for entry in selection["entities"] if entry["role"] == "dependency"]
        assert dependencies, "基准不可核实时必须把端点实体随包补齐"
        assert any(dep["status"] == "required" for dep in selection["dependencies"])
        # 不可核实的基准不能把端点标记成已存在
        assert not any(dep["status"] == "existing" for dep in selection["dependencies"])

    def test_first_download_ships_full_target(self, equal_count_versions):
        result = DocVersionService().compare_versions(
            equal_count_versions, "v1.0", "v2.0",
            receiver_id="gamma", receiver_has_baseline=False,
        )
        selection = result["selection"]
        assert result["mode"] == "full"
        assert result["baseline"]["status"] == "absent"
        target = DocVersionService()._load_records(equal_count_versions, "v2.0")
        assert len(selection["entities"]) == len(target["entities"])
        assert len(selection["relations"]) == len(target["relations"])
        assert selection["tombstones"] == []

    def test_selection_items_carry_versions_and_identities(self, equal_count_versions):
        result = DocVersionService().compare_versions(equal_count_versions, "v1.0", "v2.0")
        items = result["selection"]["items"]
        assert items
        for item in items:
            assert item["before_version"] == "v1.0"
            assert item["after_version"] == "v2.0"
            assert "deliver" in item
            assert item["reason"]


class TestIncrementalDelivery:
    def test_package_is_built_from_selection(self, equal_count_versions):
        svc = ResultDeliverService()
        result = svc.deliver_incremental(equal_count_versions, "v1.0", "v2.0", receiver_id="alpha")

        assert result["ok"] is True
        assert result["status"] == DELIVERED
        receipt = result["receipt"]
        assert receipt["mode"] == "incremental"
        assert receipt["counts"]["relations"] == len(
            DocVersionService().compare_versions(
                equal_count_versions, "v1.0", "v2.0",
                receiver_id="alpha", receiver_has_baseline=True, baseline_verified=True,
            )["selection"]["relations"]
        )
        request_dir = svc._request_dir(result["request_key"])
        assert (request_dir / "entities.jsonl").exists()
        assert (request_dir / "relations.jsonl").exists()
        assert (request_dir / "state.json").exists()

    def test_failed_delivery_resumes_same_request(self, equal_count_versions):
        svc = ResultDeliverService()
        first = svc.deliver_incremental(
            equal_count_versions, "v1.0", "v2.0",
            receiver_id="delta", fail_after_shards=0,
        )
        assert first["ok"] is False
        assert first["status"] == FAILED
        assert first["receipt"]["status"] == FAILED

        resumed = svc.resume_delivery(first["request_key"])
        assert resumed["ok"] is True
        assert resumed["status"] == DELIVERED
        assert resumed["resumed"] is True
        assert resumed["request_key"] == first["request_key"]

        # 同一次请求重复提交不重复投递
        again = svc.deliver_incremental(equal_count_versions, "v1.0", "v2.0", receiver_id="delta")
        assert again["duplicate_suppressed"] is True
        assert again["request_key"] == first["request_key"]

    def test_receipt_states_dependency_objects_and_versions(self, equal_count_versions):
        svc = ResultDeliverService()
        delivered = svc.deliver_incremental(
            equal_count_versions, "v1.0", "v2.0",
            receiver_id="eps", baseline_verified=False,
        )
        receipt = delivered["receipt"]
        assert receipt["dependencies"]
        for dependency in receipt["dependencies"]:
            assert dependency["object_type"] == "entity"
            assert dependency["version"] in ("v1.0", "v2.0")
            assert dependency["status"] in ("existing", "included", "required")
            assert dependency["relation_key"]

        receipt_from_disk = svc.get_receipt(delivered["request_key"])
        assert receipt_from_disk["ok"] is True
        assert receipt_from_disk["receipt"]["request_key"] == delivered["request_key"]

    def test_verify_rejects_missing_dependency(self, equal_count_versions):
        svc = ResultDeliverService()
        delivered = svc.deliver_incremental(
            equal_count_versions, "v1.0", "v2.0",
            receiver_id="theta", baseline_verified=False,
        )
        # 提供一个不含任何包内实体的"基准"也不能豁免端点补齐要求
        verdict = svc.verify_package(delivered["request_key"], baseline_entity_keys=["不存在"])
        assert verdict["ok"] is True
        assert verdict["baseline_usable"] is False

        # 删除包内实体文件后，缺失依赖必须被识别
        request_dir = svc._request_dir(delivered["request_key"])
        (request_dir / "entities.jsonl").unlink()
        verdict = svc.verify_package(delivered["request_key"])
        assert verdict["ok"] is False
        assert verdict["missing_dependencies"]


class TestReceiverReopensReceipt:
    """任务场景：对可读基准与缺基准的接收方重新打开下载回执，分别核对导入结果。"""

    def test_readable_baseline_receipt_only_has_incremental_payload(self, equal_count_versions):
        svc = ResultDeliverService()
        delivered = svc.deliver_incremental(
            equal_count_versions, "v1.0", "v2.0",
            receiver_id="readable", receiver_has_baseline=True, baseline_verified=True,
        )
        receipt = svc.get_receipt(delivered["request_key"])["receipt"]

        assert receipt["mode"] == "incremental"
        assert receipt["baseline"]["usable"] is True
        assert receipt["counts"]["entities"] < 5
        assert receipt["counts"]["tombstones"] > 0
        # 包里的端点实体都能在可核实基准侧找到，因此不需要额外补全实体
        assert receipt["counts"]["dependency_entities"] == 0
        assert not receipt["missing_dependencies"]
        statuses = {dependency["status"] for dependency in receipt["dependencies"]}
        assert statuses <= {"existing", "included"}

        verdict = svc.verify_package(delivered["request_key"])
        assert verdict["ok"] is True
        assert verdict["baseline_usable"] is True

    def test_no_baseline_receipt_ships_full_target(self, equal_count_versions):
        svc = ResultDeliverService()
        delivered = svc.deliver_incremental(
            equal_count_versions, "v1.0", "v2.0",
            receiver_id="fresh", receiver_has_baseline=False,
        )
        receipt = svc.get_receipt(delivered["request_key"])["receipt"]

        assert receipt["mode"] == "full"
        assert receipt["baseline"]["status"] == "absent"
        target = DocVersionService()._load_records(equal_count_versions, "v2.0")
        entities_path = svc._request_dir(delivered["request_key"]) / "entities.jsonl"
        relations_path = svc._request_dir(delivered["request_key"]) / "relations.jsonl"
        assert len(_read(entities_path)) == len(target["entities"])
        assert len(_read(relations_path)) == len(target["relations"])
        assert receipt["counts"]["tombstones"] == 0
        assert not receipt["missing_dependencies"]

    def test_unverified_baseline_does_not_trust_existing_dependencies(self, equal_count_versions):
        svc = ResultDeliverService()
        delivered = svc.deliver_incremental(
            equal_count_versions, "v1.0", "v2.0",
            receiver_id="unverified", receiver_has_baseline=True, baseline_verified=False,
        )
        receipt = svc.get_receipt(delivered["request_key"])["receipt"]

        assert receipt["mode"] == "closure"
        assert receipt["baseline"]["usable"] is False
        assert receipt["counts"]["dependency_entities"] > 0
        assert all(
            dependency["status"] != "existing" for dependency in receipt["dependencies"]
        )
        assert all(
            dependency["version"] == "v2.0"
            for dependency in receipt["dependencies"]
            if dependency["status"] == "required"
        )

    def test_resume_keeps_frozen_package_contents(self, equal_count_versions):
        svc = ResultDeliverService()
        first = svc.deliver_incremental(
            equal_count_versions, "v1.0", "v2.0",
            receiver_id="frozen", fail_after_shards=0,
        )
        request_dir = svc._request_dir(first["request_key"])
        before = (request_dir / "items.jsonl").read_text(encoding="utf-8")

        # 首次请求失败后，产物侧再发生变化也不影响同一次请求的续传内容
        entities_path = DATA_ROOT / "entities" / f"{equal_count_versions}_parsed_entities.jsonl"
        _write(entities_path, [])

        resumed = svc.resume_delivery(first["request_key"])
        assert resumed["ok"] is True
        assert (request_dir / "items.jsonl").read_text(encoding="utf-8") == before
        assert resumed["receipt"]["counts"] == first["receipt"]["counts"]


class TestModifiedDetection:
    def test_field_change_is_modified_not_replaced(self):
        doc_id = _uid("MOD")
        _prepare_snapshot(doc_id, "v1.0")
        entities_path = DATA_ROOT / "entities" / f"{doc_id}_parsed_entities.jsonl"
        entities = _dedupe(_read(entities_path), ("entity_type", "entity_text", "chunk_id"))
        entities[0]["confidence"] = 0.42
        entities[1]["entity_id"] = "ENT-REASSIGNED"
        _write(entities_path, entities)
        DocVersionService().create_snapshot(doc_id, "v2.0")

        result = DocVersionService().compare_versions(doc_id, "v1.0", "v2.0")
        modified = [entry for entry in result["entries"] if entry["change"] == "modified"]
        assert len(modified) == 2
        assert result["summary"]["by_change"]["added"] == 0
        assert result["summary"]["by_change"]["removed"] == 0
        assert result["counts_equal"] is True

        by_key = {entry["natural_key"]: entry for entry in modified}
        field_change = next(
            entry for entry in modified if entry["changed_fields"] == ["confidence"]
        )
        assert field_change["identity_reassigned"] is False
        reassigned = next(
            entry for entry in modified if entry["identity_reassigned"] is True
        )
        assert reassigned["before"]["record_id"] != reassigned["after"]["record_id"]
        assert by_key[reassigned["natural_key"]]["before"]["exists"] is True
