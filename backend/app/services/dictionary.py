"""数据字典业务规则：状态流转、字段校验、版本管理和发布事务都收在这里。

发布流程是一条完整的链路：
    草稿 →（提交条目评审）→ 条目评审中 →（发布条目）→ 已发布

发布条目不是只改一个状态字段，而是要在同一把锁（``store.lock``）里完成：
    1. 校验字段类型、字段长度、业务含义、枚举说明、字典版本是否齐全；
    2. 补齐/推进字典版本号，快照当前字段为不可变版本记录；
    3. 找到对应元数据项，按当前字典版本同步属性；
    4. 登记发布批次并把批次号回写到字典版本与元数据项；
    5. 两侧各写一条变更审计。
任何一步失败都整体回滚，并把字典条目退回「条目评审中」，不留下半成品。
"""
from __future__ import annotations

from typing import Any

from app.services import release as release_rules
from app.store import store

MODULE = "dictionary"
METAMODULE = "metamodel"
AUDITMODULE = "auditlog"
RELEASEMODULE = "release"

DRAFT = "草稿"
IN_REVIEW = "条目评审中"
PUBLISHED = "已发布"
VOIDED = "已作废"
STATUS_ORDER = [DRAFT, IN_REVIEW, PUBLISHED, VOIDED]

SUBMIT_ACTION = "提交条目评审"
PUBLISH_ACTION = "发布条目"
VOID_ACTION = "作废条目"
ACTION_RULES = {SUBMIT_ACTION: IN_REVIEW, PUBLISH_ACTION: PUBLISHED, VOID_ACTION: VOIDED}
NEGATIVE_ACTIONS = [VOID_ACTION]

REQUIRED_FIELDS = ["条目编号", "表名", "字段名称"]
FIELD_FIELDS = ["字段类型", "字段长度", "业务含义", "枚举说明"]
VERSION_FIELD = "字典版本"
PUBLISH_FIELDS = [*FIELD_FIELDS, VERSION_FIELD]

# 发布事务涉及的四张表，回滚时整表恢复。
TRANSACTION_MODULES = [MODULE, METAMODULE, AUDITMODULE, RELEASEMODULE]


class DictionaryService:
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
            rows = list(store.rows(MODULE))
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("条目编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        with store.lock:
            entry = store.find(MODULE, entry_id)
            return self._detail(entry) if entry else None

    # ------------------------------------------------------------------ 登记
    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str], str]:
        """登记草稿：必填字段必须齐全，版本号与字段类型的完整性留到发布前校验。"""
        with store.lock:
            missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
            if missing:
                return None, missing, ""

            rows = store.rows(MODULE)
            duplicate = next(
                (row for row in rows if str(row.get("条目编号") or "") == str(values["条目编号"]).strip()),
                None,
            )
            if duplicate is not None:
                return None, [], f"条目编号 {values['条目编号']} 已存在"

            entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
            entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
            for field in PUBLISH_FIELDS:
                entry[field] = str(values.get(field) or "").strip()
            entry["status"] = DRAFT
            entry["pending"] = True
            entry["abnormal"] = False
            entry["版本"] = ""
            entry["published_version"] = None
            entry["versions"] = []
            entry["release_batches"] = []
            entry["同步状态"] = "未发布"
            entry["元数据项id"] = self._metadata_id_from_values(values)
            entry["元数据编号"] = str(values.get("元数据编号") or "").strip() or None
            entry["操作人"] = str(values.get("操作人") or "值班管理员")
            rows.append(entry)
            return self._detail(entry), [], "字典条目已登记"

    @staticmethod
    def _metadata_id_from_values(values: dict[str, Any]) -> int | None:
        raw = values.get("元数据项id")
        if raw in (None, ""):
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    def update_entry(self, entry_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """已发布条目再次修改：生成新的评审版本，旧版本保持不可变。

        未发布条目直接改当前字段；已发布/已作废条目只允许生成新的评审版本，
        不能覆盖已发布版本的内容。
        """
        with store.lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"字典条目 {entry_id} 不存在或已归档"

            provided = {field: str(values.get(field) or "").strip() for field in FIELD_FIELDS if field in values}
            requested_version = str(values.get(VERSION_FIELD) or "").strip()

            if entry.get("status") in (PUBLISHED, VOIDED):
                if entry.get("status") == VOIDED:
                    return None, "已作废条目不能修改，请先登记新条目"
                return self._open_revision(entry, provided, requested_version)

            # 评审中的条目只能基于最近已发布版本向前推进，不能用旧版本号覆盖。
            published = release_rules.published_snapshot(entry)
            if published is not None:
                baseline = release_rules.normalize_version(published.get("版本"))
                target = release_rules.normalize_version(requested_version or entry.get(VERSION_FIELD) or baseline)
                if release_rules.version_key(target) <= release_rules.version_key(baseline):
                    return None, f"版本 {target} 不高于已发布版本 {baseline}，冲突按已发布版本处理"
                if release_rules.version_is_published(entry, target):
                    return None, f"版本 {target} 已发布，不能重复使用"

            entry.update({field: value for field, value in provided.items() if value})
            if requested_version:
                entry[VERSION_FIELD] = release_rules.normalize_version(requested_version)
            return self._detail(entry), "字典条目已更新（尚未发布）"

    def _open_revision(
        self,
        entry: dict[str, Any],
        provided: dict[str, str],
        requested_version: str,
    ) -> tuple[dict[str, Any] | None, str]:
        """为已发布条目开新评审版本，不触碰历史版本记录。"""
        current_version = release_rules.normalize_version(entry.get("published_version") or entry.get(VERSION_FIELD))
        if requested_version:
            new_version = release_rules.normalize_version(requested_version)
            if release_rules.version_key(new_version) <= release_rules.version_key(current_version):
                return None, f"新版本号 {new_version} 必须高于当前已发布版本 {current_version}"
        else:
            new_version = release_rules.next_version(current_version)

        if release_rules.version_is_published(entry, new_version):
            return None, f"版本 {new_version} 已发布，不能重复使用"

        entry.update(provided)
        entry[VERSION_FIELD] = new_version
        entry["status"] = IN_REVIEW
        entry["pending"] = True
        entry["abnormal"] = False
        entry["release_version"] = None
        return self._detail(entry), f"已生成新评审版本 {new_version}，发布后生效"

    # ------------------------------------------------------------------ 动作
    def run_action(
        self,
        entry_id: int,
        action: str,
        values: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """状态机入口：只允许 草稿→条目评审中→已发布→已作废 单向推进。

        附带字段值时先走版本化修改（已发布条目会生成新的评审版本），
        再执行动作；这样「发布后修改」不会覆盖旧版本。
        """
        with store.lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"字典条目 {entry_id} 不存在或已归档"

            values = values or {}
            if action in ("修改条目", "更新条目", "保存草稿"):
                return self.update_entry(entry_id, values)

            attached = {
                field: values.get(field)
                for field in [*FIELD_FIELDS, VERSION_FIELD]
                if field in values and values.get(field) not in (None, "")
            }
            if attached and action in (SUBMIT_ACTION, PUBLISH_ACTION):
                updated, update_message = self.update_entry(entry_id, attached)
                if updated is None:
                    return None, update_message
                entry = store.find(MODULE, entry_id)
                if entry is None:
                    return None, f"字典条目 {entry_id} 不存在或已归档"
                if action == SUBMIT_ACTION and entry.get("status") == IN_REVIEW:
                    # 修改已发布条目已经生成了评审版本，无需再做一次状态推进。
                    return self._detail(entry), update_message

            if action not in ACTION_RULES:
                return None, f"动作「{action}」不属于数据字典可执行范围"

            if action == SUBMIT_ACTION:
                return self._submit_for_review(entry)
            if action == PUBLISH_ACTION:
                return self._publish(entry)
            return self._void(entry)

    def _submit_for_review(self, entry: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        current = entry.get("status")
        if current == DRAFT:
            pass
        elif current == IN_REVIEW:
            return None, "字典条目已在条目评审中，不能重复提交"
        elif current == PUBLISHED:
            # 已发布条目不能直接回到评审，必须先通过修改生成新的评审版本。
            return None, "已发布条目不能直接重新进入评审，请先修改生成新版本"
        else:
            return None, "已作废条目不能提交评审"

        entry["status"] = IN_REVIEW
        entry["pending"] = True
        entry["abnormal"] = False
        return self._detail(entry), "字典条目已提交条目评审"

    def _void(self, entry: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        current = entry.get("status")
        if current == VOIDED:
            return None, "字典条目已作废，不能重复作废"
        if current not in (DRAFT, IN_REVIEW, PUBLISHED):
            return None, f"当前状态「{current}」不允许作废"
        entry["status"] = VOIDED
        entry["pending"] = False
        entry["abnormal"] = True
        return self._detail(entry), "字典条目已作废"

    # ------------------------------------------------------------------ 发布
    def _publish(self, entry: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        if entry.get("status") != IN_REVIEW:
            return None, self._publish_precondition_message(entry)

        missing = [field for field in PUBLISH_FIELDS if not str(entry.get(field) or "").strip()]
        if missing:
            return None, f"发布前必须补齐字段：{'、'.join(missing)}"

        version = release_rules.normalize_version(entry.get(VERSION_FIELD))
        if release_rules.version_is_published(entry, version):
            return None, f"版本 {version} 已发布，不能重复发布"

        current_published = release_rules.published_snapshot(entry)
        if current_published is not None:
            published_version = release_rules.normalize_version(current_published.get("版本"))
            if release_rules.version_key(version) < release_rules.version_key(published_version):
                return None, (
                    f"版本 {version} 低于已发布版本 {published_version}，"
                    "冲突按已发布版本处理，请提升版本号"
                )
        else:
            published_version = None

        snapshot = release_rules.snapshot_modules(TRANSACTION_MODULES)
        try:
            with store.lock:
                result = self._commit_publish(entry, version, published_version)
        except Exception:
            release_rules.restore_modules(snapshot)
            entry = store.find(MODULE, int(entry.get("id", 0))) or entry
            entry["status"] = IN_REVIEW
            entry["pending"] = True
            entry["abnormal"] = False
            entry["release_version"] = None
            try:
                release_rules.write_audit(
                    table_name="dictionary",
                    change_type="发布失败回滚",
                    before=PUBLISHED,
                    after=IN_REVIEW,
                    batch_no="-",
                    reason=f"字典条目 {entry.get('条目编号')} 发布失败，已回退到条目评审中",
                    operator=entry.get("操作人") or "值班管理员",
                )
            except Exception:
                pass
            return None, "发布失败，字典条目已回退到条目评审中，未留下半成品"
        return result

    def _publish_precondition_message(self, entry: dict[str, Any]) -> str:
        current = entry.get("status")
        if current == DRAFT:
            return "字典条目仍为草稿，必须先提交条目评审后才能发布"
        if current == PUBLISHED:
            return "字典条目已发布，重复发布不会被接受"
        if current == VOIDED:
            return "已作废条目不能发布"
        return f"当前状态「{current}」不允许发布"

    def _commit_publish(
        self,
        entry: dict[str, Any],
        version: str,
        published_version: str | None,
    ) -> tuple[dict[str, Any] | None, str]:
        """发布事务主体。调用方已持有 store.lock 并在失败时整体回滚。"""
        sequence = release_rules.next_batch_sequence(entry)
        batch_no = release_rules.make_batch_no(entry, version, sequence)
        fields = {field: str(entry.get(field) or "").strip() for field in FIELD_FIELDS}

        entry[VERSION_FIELD] = version
        entry["版本"] = version
        entry["published_version"] = version

        # 1. 冻结历史已发布版本（不可变），再登记本次发布版本。
        entry.setdefault("versions", [])
        entry["versions"].append({
            "版本": version,
            "版本序号": sequence,
            "发布批次": batch_no,
            **fields,
            "状态": PUBLISHED,
            "发布时间": release_rules._now(),
            "元数据项id": entry.get("元数据项id"),
        })

        # 2. 同步元数据：按当前已发布字典版本更新属性，版本号与批次号必须一致。
        metadata = release_rules.find_metadata_entry(entry)
        if metadata is not None:
            metadata_id = int(metadata.get("id", 0))
            metadata_code = str(metadata.get("元数据编号") or "")
            previous_version = metadata.get("数据字典版本") or metadata.get("属性值")
            stored_version = release_rules.normalize_version(previous_version)
            if release_rules.version_key(stored_version) > release_rules.version_key(version):
                # 并发换版：已有更高的已发布版本，本次先提交的旧版本不被接受。
                entry["versions"].pop()
                entry["status"] = IN_REVIEW
                entry["pending"] = True
                entry[VERSION_FIELD] = entry.get("published_version") or stored_version
                entry["版本"] = entry[VERSION_FIELD]
                entry["release_version"] = entry.get("published_version")
                return None, (
                    f"元数据已按更高版本 {stored_version} 同步，版本 {version} 未被接受，"
                    "字典条目保持在条目评审中，版本号已对齐已发布版本"
                )
            metadata.update({
                "属性值": fields["字段类型"],
                "字段类型": fields["字段类型"],
                "字段长度": fields["字段长度"],
                "业务含义": fields["业务含义"],
                "枚举说明": fields["枚举说明"],
                "数据字典版本": version,
                "发布批次": batch_no,
                "字典条目id": int(entry.get("id", 0)),
                "status": PUBLISHED,
                "pending": False,
                "abnormal": False,
                "更新时间": release_rules._now(),
            })
            entry["元数据项id"] = metadata_id
            entry["元数据编号"] = metadata_code or entry.get("元数据编号")
            release_rules.write_audit(
                table_name="metamodel",
                change_type="同步字典版本",
                before=f"{previous_version or '无'}",
                after=f"{version}（{fields['字段类型']}，{fields['枚举说明']}）",
                batch_no=batch_no,
                reason=f"元数据项 {metadata_code} 按字典条目 {entry.get('条目编号')} 发布同步",
                operator=entry.get("操作人") or "值班管理员",
            )
        else:
            metadata_id = None
            metadata_code = None
            entry["同步状态"] = "无对应元数据项"

        # 3. 发布批次作为两个业务面的串联凭证。
        batch = release_rules.record_batch(
            batch_no=batch_no,
            sequence=sequence,
            entry=entry,
            metadata_id=metadata_id,
            metadata_label=metadata_code,
            status="发布成功",
            message=f"字典版本 {version} 已同步到元数据属性",
        )
        entry.setdefault("release_batches", []).append(batch_no)
        entry["versions"][-1]["元数据项id"] = metadata_id

        # 4. 字典侧审计。
        before = f"{published_version or '无'}（{entry.get('发布前枚举说明') or '无枚举'}）"
        release_rules.write_audit(
            table_name="dictionary",
            change_type="发布字典版本",
            before=before,
            after=f"{version}（{fields['字段类型']}，{fields['枚举说明']}）",
            batch_no=batch_no,
            reason=f"字典条目 {entry.get('条目编号')} 发布并同步元数据",
            operator=entry.get("操作人") or "值班管理员",
        )

        entry["status"] = PUBLISHED
        entry["pending"] = False
        entry["abnormal"] = False
        entry["release_version"] = version
        entry["release_batch"] = batch_no
        entry["同步状态"] = "已同步" if metadata_id is not None else entry.get("同步状态")
        return self._detail(entry), f"字典条目已发布，版本 {version}，发布批次 {batch_no}"

    # ------------------------------------------------------------------ 明细
    def _detail(self, entry: dict[str, Any]) -> dict[str, Any]:
        """详情视图：补齐已发布版本快照与发布批次，供详情页与原版本核对。"""
        detail = dict(entry)
        detail["已发布快照"] = release_rules.published_snapshot(entry)
        detail["版本历史"] = list(entry.get("versions") or [])
        detail["发布批次编号"] = entry.get("release_batch")
        return detail
