"""元数据管理业务规则：状态流转、字典版本读取与属性同步都收在这里。

元数据不再保存一份独立的属性副本：字段类型、长度、业务含义、枚举说明
一律以「字典条目当前已发布版本」为准，读取时实时对齐。发布/同步时要
么整段成功（属性 + 版本号 + 发布批次 + 审计），要么整体回滚。
"""
from __future__ import annotations

from typing import Any

from app.services import release as release_rules
from app.services.dictionary import DRAFT as DICT_DRAFT
from app.services.dictionary import IN_REVIEW as DICT_IN_REVIEW
from app.services.dictionary import PUBLISHED as DICT_PUBLISHED
from app.services.dictionary import VOIDED as DICT_VOIDED
from app.store import store

MODULE = "metamodel"
MODULE_DICTIONARY = "dictionary"
MODULE_AUDIT = "auditlog"
MODULE_RELEASE = "release"

METADRAFT = "草稿"
META_IN_REVIEW = "元数据评审中"
META_PUBLISHED = "已发布"
META_OFFLINE = "已下线"
STATUS_ORDER = [METADRAFT, META_IN_REVIEW, META_PUBLISHED, META_OFFLINE]
SUBMIT_ACTION = "提交元数据评审"
PUBLISH_ACTION = "发布元数据"
OFFLINE_ACTION = "下线元数据"
SYNC_ACTION = "同步元数据属性"
ACTION_RULES = {SUBMIT_ACTION: META_IN_REVIEW, PUBLISH_ACTION: META_PUBLISHED, OFFLINE_ACTION: META_OFFLINE}
NEGATIVE_ACTIONS = [OFFLINE_ACTION]

REQUIRED_FIELDS = ["元数据编号", "对象类型", "对象名称"]
ATTRIBUTE_FIELDS = ["字段类型", "字段长度", "业务含义", "枚举说明"]
VERSION_FIELD = "数据字典版本"
TRANSACTION_MODULES = [MODULE, MODULE_DICTIONARY, MODULE_AUDIT, MODULE_RELEASE]


class MetamodelService:
    # ------------------------------------------------------------------ 查询
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        with store.lock:
            rows = [self._align(row) for row in store.rows(MODULE)]
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("元数据编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        with store.lock:
            entry = store.find(MODULE, entry_id)
            return self._align(entry) if entry else None

    def _align(self, entry: dict[str, Any]) -> dict[str, Any]:
        """按当前已发布字典版本读取属性；没有已发布版本时保留评审中的草稿值。"""
        detail = dict(entry)
        detail.setdefault(VERSION_FIELD, "")
        for field in ATTRIBUTE_FIELDS:
            detail.setdefault(field, "")
        dict_entry = self._linked_dictionary(entry)
        detail["字典条目编号"] = dict_entry.get("条目编号") if dict_entry else None
        detail["字典条目id"] = int(dict_entry.get("id", 0)) if dict_entry else None
        snapshot = release_rules.published_snapshot(dict_entry) if dict_entry else None
        if snapshot is not None:
            detail.update({field: snapshot.get(field) for field in ATTRIBUTE_FIELDS})
            detail[VERSION_FIELD] = snapshot.get("版本")
            detail["发布批次"] = snapshot.get("发布批次")
            detail["属性来源"] = "字典已发布版本"
        else:
            detail["属性来源"] = "待字典发布"
        return detail

    def _linked_dictionary(self, entry: dict[str, Any]) -> dict[str, Any] | None:
        return release_rules.find_dictionary_entry(metadata_entry=entry)

    # ------------------------------------------------------------------ 登记
    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str], str]:
        with store.lock:
            missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
            if missing:
                return None, missing, ""

            rows = store.rows(MODULE)
            duplicate = next(
                (row for row in rows if str(row.get("元数据编号") or "") == str(values["元数据编号"]).strip()),
                None,
            )
            if duplicate is not None:
                return None, [], f"元数据编号 {values['元数据编号']} 已存在"

            entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
            entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
            for field in ["属性名称", "属性值", "技术负责人", "业务负责人", "更新周期"]:
                entry[field] = str(values.get(field) or "").strip()
            entry["status"] = METADRAFT
            entry["pending"] = True
            entry["abnormal"] = False
            entry[VERSION_FIELD] = str(values.get(VERSION_FIELD) or "").strip()
            entry["字典条目id"] = self._dict_id_from_values(values)
            entry["操作人"] = str(values.get("操作人") or "值班管理员")
            rows.append(entry)
            return self._align(entry), [], "元数据项已登记"

    def _dict_id_from_values(self, values: dict[str, Any]) -> int | None:
        raw = values.get("字典条目id")
        if raw in (None, ""):
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    def update_entry(self, entry_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """修改元数据项；属性字段若已由字典发布锁定，则拒绝在元数据侧改写。"""
        with store.lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"元数据项 {entry_id} 不存在或已归档"
            if entry.get("status") == "已下线":
                return None, "已下线元数据项不能修改"

            dict_entry = self._linked_dictionary(entry)
            snapshot = release_rules.published_snapshot(dict_entry) if dict_entry else None
            changed_attrs = [field for field in ATTRIBUTE_FIELDS if field in values]
            if snapshot is not None and changed_attrs:
                return None, (
                    f"属性字段由字典条目 {dict_entry.get('条目编号')} 的已发布版本 "
                    f"{snapshot.get('版本')} 管理，请在数据字典中修改并重新发布"
                )

            for field in ["对象类型", "对象名称", "属性名称", "属性值", "技术负责人", "业务负责人", "更新周期"]:
                if field in values:
                    entry[field] = str(values.get(field) or "").strip()
            if VERSION_FIELD in values:
                entry[VERSION_FIELD] = str(values.get(VERSION_FIELD) or "").strip()
            if "字典条目id" in values:
                entry["字典条目id"] = self._dict_id_from_values(values)
            for field in ATTRIBUTE_FIELDS:
                if field in values and snapshot is None:
                    entry[field] = str(values.get(field) or "").strip()
            return self._align(entry), "元数据项已更新（尚未发布）"

    # ------------------------------------------------------------------ 动作
    def run_action(
        self,
        entry_id: int,
        action: str,
        values: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        with store.lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"元数据项 {entry_id} 不存在或已归档"

            if action in (SYNC_ACTION, "同步字典版本", "更新属性"):
                return self._sync_attributes(entry)
            if action not in ACTION_RULES:
                return None, f"动作「{action}」不属于元数据管理可执行范围"

            if action == SUBMIT_ACTION:
                return self._submit_for_review(entry)
            if action == PUBLISH_ACTION:
                return self._publish(entry)
            return self._offline(entry)

    def _submit_for_review(self, entry: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        current = entry.get("status")
        if current == META_IN_REVIEW:
            return None, "元数据项已在元数据评审中，不能重复提交"
        if current == METADRAFT:
            entry["status"] = META_IN_REVIEW
            entry["pending"] = True
            entry["abnormal"] = False
            return self._align(entry), "元数据项已提交元数据评审"
        if current == META_PUBLISHED:
            return None, "已发布元数据项不能直接重新进入评审，请先修改生成新版本"
        return None, "已下线元数据项不能提交评审"

    def _offline(self, entry: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        current = entry.get("status")
        if current == META_OFFLINE:
            return None, "元数据项已下线，不能重复下线"
        if current not in (METADRAFT, META_IN_REVIEW, META_PUBLISHED):
            return None, f"当前状态「{current}」不允许下线"
        entry["status"] = META_OFFLINE
        entry["pending"] = False
        entry["abnormal"] = True
        return self._align(entry), "元数据项已下线"

    def _publish(self, entry: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        current = entry.get("status")
        if current == META_PUBLISHED:
            return None, "元数据项已发布，重复发布不会被接受"
        if current != META_IN_REVIEW:
            return None, f"当前状态「{current}」不允许发布，必须先提交元数据评审"

        dict_entry = self._linked_dictionary(entry)
        snapshot = release_rules.published_snapshot(dict_entry) if dict_entry else None
        if dict_entry is not None and snapshot is None:
            # 字典仍在评审中：没有可仲裁的已发布版本，不能先发布元数据。
            if dict_entry.get("status") in (DICT_DRAFT, DICT_IN_REVIEW):
                self._rollback_dictionary(dict_entry, "元数据发布前置校验失败")
            return None, (
                f"字典条目 {dict_entry.get('条目编号')} 尚未完成评审发布，"
                "元数据不能先于字典显示已发布"
            )

        snapshot_modules = release_rules.snapshot_modules(TRANSACTION_MODULES)
        try:
            with store.lock:
                return self._commit_publish(entry, dict_entry, snapshot)
        except Exception:
            release_rules.restore_modules(snapshot_modules)
            try:
                self._rollback_dictionary(dict_entry, "元数据属性同步失败")
            except Exception:
                pass
            return None, "元数据发布失败，已回滚；字典条目回退到条目评审中，未留下半成品"

    def _commit_publish(
        self,
        entry: dict[str, Any],
        dict_entry: dict[str, Any] | None,
        snapshot: dict[str, Any] | None,
    ) -> tuple[dict[str, Any] | None, str]:
        previous_version = entry.get(VERSION_FIELD) or entry.get("属性值") or "无"
        if snapshot is not None:
            # 版本冲突交给她已完成评审的字典版本仲裁：冲突按已发布版本处理。
            version = release_rules.normalize_version(snapshot.get("版本"))
            entry.update({field: snapshot.get(field) for field in ATTRIBUTE_FIELDS})
            entry[VERSION_FIELD] = version
            entry["属性值"] = snapshot.get("字段类型")
            entry["发布批次"] = snapshot.get("发布批次")
            entry["字典条目id"] = int(dict_entry.get("id", 0))
            after = f"{version}（{snapshot.get('字段类型')}，{snapshot.get('枚举说明')}）"
            release_rules.write_audit(
                table_name="metamodel",
                change_type="按已发布字典版本发布",
                before=previous_version,
                after=after,
                batch_no=snapshot.get("发布批次") or "-",
                reason=f"元数据项 {entry.get('元数据编号')} 发布时按字典已发布版本仲裁",
                operator=entry.get("操作人") or "值班管理员",
            )
            message = f"元数据项已发布，按字典版本 {version} 同步属性"
        else:
            version = release_rules.normalize_version(entry.get(VERSION_FIELD))
            message = "元数据项已发布"

        entry["status"] = META_PUBLISHED
        entry["pending"] = False
        entry["abnormal"] = False
        entry["release_version"] = version
        return self._align(entry), message

    def _sync_attributes(self, entry: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """按当前已发布字典版本同步属性；失败时字典回退到条目评审中。"""
        dict_entry = self._linked_dictionary(entry)
        if dict_entry is None:
            return None, "未找到对应的字典条目，无法同步属性"

        snapshot = release_rules.published_snapshot(dict_entry)
        if snapshot is None:
            self._rollback_dictionary(dict_entry, "属性同步时字典没有已发布版本")
            return None, (
                f"字典条目 {dict_entry.get('条目编号')} 没有已发布版本，同步失败，"
                "字典已回退到条目评审中"
            )

        snapshot_modules = release_rules.snapshot_modules(TRANSACTION_MODULES)
        try:
            with store.lock:
                version = release_rules.normalize_version(snapshot.get("版本"))
                previous = entry.get(VERSION_FIELD) or "无"
                entry.update({field: snapshot.get(field) for field in ATTRIBUTE_FIELDS})
                entry[VERSION_FIELD] = version
                entry["属性值"] = snapshot.get("字段类型")
                entry["发布批次"] = snapshot.get("发布批次")
                entry["字典条目id"] = int(dict_entry.get("id", 0))
                entry["status"] = META_PUBLISHED
                entry["pending"] = False
                entry["abnormal"] = False
                release_rules.write_audit(
                    table_name="metamodel",
                    change_type="同步字典版本",
                    before=previous,
                    after=f"{version}（{snapshot.get('字段类型')}，{snapshot.get('枚举说明')}）",
                    batch_no=snapshot.get("发布批次") or "-",
                    reason=f"元数据项 {entry.get('元数据编号')} 按当前已发布字典版本同步",
                    operator=entry.get("操作人") or "值班管理员",
                )
        except Exception:
            release_rules.restore_modules(snapshot_modules)
            # 审计写入本身也可能失败，回退动作不能再抛出二次异常。
            try:
                self._rollback_dictionary(dict_entry, "属性同步失败")
            except Exception:
                pass
            return None, "属性同步失败，已回滚；字典条目回退到条目评审中，未留下半成品"

        return self._align(entry), f"元数据属性已按字典版本 {version} 同步"

    def _rollback_dictionary(self, dict_entry: dict[str, Any] | None, reason: str) -> None:
        """同步失败时把字典放回条目评审中，保持两端状态一致。"""
        if dict_entry is None:
            return
        # 回滚会整表替换，必须按 id 重新取当前表里的活对象再改状态。
        live = release_rules.find_dictionary_entry(dict_entry_id=dict_entry.get("id"))
        if live is None:
            return
        current = live.get("status")
        if current == DICT_VOIDED:
            return
        if current == DICT_DRAFT:
            live["status"] = DICT_IN_REVIEW
        elif current != DICT_IN_REVIEW:
            # 已发布版本不可变，只回退当前发布标记，历史版本原样保留。
            live["status"] = DICT_IN_REVIEW
        live["pending"] = True
        live["abnormal"] = False
        live["release_version"] = None
        release_rules.write_audit(
            table_name="dictionary",
            change_type="同步失败回退",
            before=DICT_PUBLISHED,
            after=DICT_IN_REVIEW,
            batch_no="-",
            reason=reason,
            operator=live.get("操作人") or "值班管理员",
        )
