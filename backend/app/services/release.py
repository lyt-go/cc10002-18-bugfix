"""发布批次与版本号工具：字典发布、元数据同步、变更审计共用一套口径。

这里只放纯函数和批次记录，不依赖 dictionary / metamodel 服务，避免循环导入。
"""
from __future__ import annotations

import copy
import re
from datetime import datetime
from typing import Any

from app.store import store

MODULE = "release"
VERSION_FIELDS = ["字段类型", "字段长度", "业务含义", "枚举说明"]
PUBLISHED = "已发布"
VOIDED = "已作废"

_VERSION_PATTERN = re.compile(r"(\d+)")


def version_key(value: Any) -> tuple[int, str]:
    """返回可比较的版本键：v5、V5、5、版本5 都按数字比较。

    无法识别数字的版本排在最前，后续出现的 v1/v4/v5 总能覆盖旧占位值。
    """
    text = str(value or "").strip()
    match = _VERSION_PATTERN.search(text)
    if match:
        return int(match.group(1)), text.lower()
    return -1, text.lower()


def normalize_version(value: Any) -> str:
    """把 5 / V5 / v5 统一成 v5；其他写法保留原样。"""
    text = str(value or "").strip()
    match = re.fullmatch(r"(?:[vV])?(\d+)", text)
    if match:
        return f"v{int(match.group(1))}"
    return text


def next_version(value: Any) -> str:
    """在已有版本号上推进一版；无法识别数字时从 v1 开始。"""
    number, _ = version_key(value)
    if number >= 0:
        return f"v{number + 1}"
    return "v1"


def published_snapshot(entry: dict[str, Any]) -> dict[str, Any] | None:
    """读取字典条目最后一次已发布的版本快照。

    已发布版本一旦生成就冻结在 ``versions`` 里，后续草稿/评审版本只改当前字段，
    不会回写历史，因此这里始终返回不可变的旧版本内容。
    """
    versions = entry.get("versions") or []
    published = [item for item in versions if item.get("状态") == PUBLISHED]
    if published:
        item = max(published, key=lambda row: version_key(row.get("版本")))
        snapshot = {field: item.get(field) for field in VERSION_FIELDS}
        snapshot["版本"] = item.get("版本")
        snapshot["发布批次"] = item.get("发布批次")
        snapshot["发布时间"] = item.get("发布时间")
        return snapshot

    # 兼容尚未写入版本历史的示例数据：只有已发布/已作废的当前字段才可作为已发布快照。
    if entry.get("status") in (PUBLISHED, VOIDED):
        snapshot = {field: entry.get(field) for field in VERSION_FIELDS}
        snapshot["版本"] = entry.get("published_version") or entry.get("字典版本")
        snapshot["发布批次"] = entry.get("发布批次")
        snapshot["发布时间"] = entry.get("发布时间")
        return snapshot
    return None


def snapshot_modules(names: list[str]) -> dict[str, list[dict[str, Any]]]:
    """深拷贝若干业务表，供发布事务失败时整体回滚。"""
    return {name: copy.deepcopy(store.rows(name)) for name in names}


def restore_modules(snapshot: dict[str, list[dict[str, Any]]]) -> None:
    """把发布事务涉及的业务表恢复成事务开始前的样子。"""
    for name, rows in snapshot.items():
        store.replace_rows(name, copy.deepcopy(rows))


def find_dictionary_entry(
    *,
    metadata_entry: dict[str, Any] | None = None,
    dict_entry_id: Any = None,
    table_name: Any = None,
    field_name: Any = None,
) -> dict[str, Any] | None:
    """按显式关联、字典条目 id 或「表名 + 字段名称」定位字典条目。

    优先使用显式关联，避免同名表字段在换版时被错误地串到别的条目上。
    """
    dictionary_rows = store.rows("dictionary")
    if metadata_entry is not None:
        linked_id = metadata_entry.get("字典条目id")
        if linked_id is not None:
            for row in dictionary_rows:
                if int(row.get("id", 0)) == int(linked_id):
                    return row
            return None
        table_name = metadata_entry.get("对象名称")
        field_name = metadata_entry.get("属性名称")
    if dict_entry_id is not None:
        for row in dictionary_rows:
            if int(row.get("id", 0)) == int(dict_entry_id):
                return row
        return None
    if table_name and field_name:
        for row in dictionary_rows:
            if str(row.get("表名") or "") == str(table_name) and str(row.get("字段名称") or "") == str(field_name):
                return row
    return None


def find_metadata_entry(dict_entry: dict[str, Any]) -> dict[str, Any] | None:
    """找字典条目对应的元数据项：优先显式 id，其次表名 + 字段名称。"""
    metadata_rows = store.rows("metamodel")
    linked_id = dict_entry.get("元数据项id")
    if linked_id is not None:
        for row in metadata_rows:
            if int(row.get("id", 0)) == int(linked_id):
                return row
        return None
    table_name = str(dict_entry.get("表名") or "")
    field_name = str(dict_entry.get("字段名称") or "")
    for row in metadata_rows:
        if str(row.get("对象名称") or "") == table_name and str(row.get("属性名称") or "") == field_name:
            return row
    return None


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def next_batch_sequence(entry: dict[str, Any]) -> int:
    """同一条字典条目的发布批次序号；每次成功发布只推进一格。"""
    return len(entry.get("release_batches") or []) + 1


def make_batch_no(entry: dict[str, Any], version: Any, sequence: int) -> str:
    """批次号把字典条目与版本绑在一起：PUB-<条目编号>-<版本>-<序号>。

    批次号必须唯一：重复换版后不能和旧批次串号，否则详情与审计会指向同一批次。
    """
    code = str(entry.get("条目编号") or "DICT").strip() or "DICT"
    normalized = normalize_version(version) or "v1"
    used = {str(row.get("批次编号")) for row in store.rows(MODULE)}
    candidate = f"PUB-{code}-{normalized}-{sequence:02d}"
    suffix = 1
    while candidate in used:
        suffix += 1
        candidate = f"PUB-{code}-{normalized}-{sequence:02d}-{suffix}"
    return candidate


def published_versions(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """返回已发布版本历史（按版本号倒序），供冲突仲裁与重复发布检查。"""
    versions = [item for item in entry.get("versions") or [] if item.get("状态") == PUBLISHED]
    return sorted(versions, key=lambda row: version_key(row.get("版本")), reverse=True)


def version_is_published(entry: dict[str, Any], version: Any) -> bool:
    normalized = normalize_version(version)
    for item in published_versions(entry):
        if normalize_version(item.get("版本")) == normalized:
            return True
    # 兼容没有版本历史的示例数据。
    if not published_versions(entry) and entry.get("status") in (PUBLISHED, VOIDED):
        return normalize_version(entry.get("published_version") or entry.get("字典版本")) == normalized
    return False


def record_batch(
    *,
    batch_no: str,
    sequence: int,
    entry: dict[str, Any],
    metadata_id: int | None,
    metadata_label: str | None,
    status: str,
    message: str,
) -> dict[str, Any]:
    """登记一条发布批次：它是字典版本与元数据属性的串联凭证。"""
    rows = store.rows(MODULE)
    batch = {
        "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
        "批次编号": batch_no,
        "批次序号": sequence,
        "字典条目编号": entry.get("条目编号"),
        "表名": entry.get("表名"),
        "字段名称": entry.get("字段名称"),
        "字典版本": normalize_version(entry.get("字典版本")),
        "元数据编号": metadata_label,
        "元数据项id": metadata_id,
        "发布状态": status,
        "说明": message,
        "操作人": entry.get("操作人") or "值班管理员",
        "发布时间": _now(),
        "status": status,
        "pending": False,
        "abnormal": status != "发布成功",
    }
    rows.append(batch)
    return batch


def write_audit(
    *,
    table_name: str,
    change_type: str,
    before: str,
    after: str,
    batch_no: str,
    reason: str,
    operator: str,
) -> dict[str, Any]:
    """写一条变更审计；发布批次号同时出现在变更原因里，便于跨模块追溯。"""
    rows = store.rows("auditlog")
    record = {
        "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
        "记录编号": f"AUD-{len(rows) + 1:04d}",
        "操作表名": table_name,
        "变更类型": change_type,
        "变更前值": before,
        "变更后值": after,
        "操作人": operator,
        "操作时间": _now(),
        "变更原因": f"{reason}（发布批次 {batch_no}）",
        "发布批次": batch_no,
        "status": "已记录",
        "pending": False,
        "abnormal": False,
    }
    rows.append(record)
    return record


class ReleaseService:
    """发布批次查询：让两个业务面都能按批次号核对同一次发布。"""

    def list_batches(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        with store.lock:
            rows = list(store.rows(MODULE))
        if keyword:
            rows = [
                row for row in rows
                if keyword in str(row.get("批次编号", ""))
                or keyword in str(row.get("字典条目编号", ""))
            ]
        if status:
            rows = [row for row in rows if row.get("发布状态") == status or row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_batch(self, batch_id: int) -> dict[str, Any] | None:
        with store.lock:
            return store.find(MODULE, batch_id)

    def get_batch_by_no(self, batch_no: str) -> dict[str, Any] | None:
        with store.lock:
            for row in store.rows(MODULE):
                if row.get("批次编号") == batch_no:
                    return row
        return None
