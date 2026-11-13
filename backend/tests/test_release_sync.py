"""字典与元数据版本同步的回归测试。

不依赖 pytest，直接运行：
    cd backend && .venv/bin/python tests/test_release_sync.py
"""
from __future__ import annotations

import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services import release as release_rules  # noqa: E402
from app.services.dictionary import DictionaryService  # noqa: E402
from app.services.metamodel import MetamodelService  # noqa: E402
from app.store import store  # noqa: E402

D = DictionaryService()
M = MetamodelService()


def fresh() -> None:
    store.reset()


def eq(actual, expected, label: str) -> None:
    assert actual == expected, f"{label}: {actual!r} != {expected!r}"
    print(f"  ok  {label}")


def test_review_required_before_publish() -> None:
    fresh()
    res, msg = D.run_action(1, "发布条目")
    eq(res, None, "草稿不能直接发布")
    res, msg = D.run_action(2, "提交条目评审")
    eq(res, None, "评审中不能重复提交")
    res, msg = D.run_action(3, "提交条目评审")
    eq(res, None, "已发布不能直接回到评审")


def test_publish_requires_complete_fields() -> None:
    fresh()
    entry, missing, _ = D.create_entry({"条目编号": "DICT_NEW", "表名": "t", "字段名称": "f"})
    assert entry is not None and not missing
    assert D.run_action(entry["id"], "提交条目评审")[0] is not None
    res, msg = D.run_action(entry["id"], "发布条目")
    eq(res, None, "缺字段不能发布")
    eq(D.get_entry(entry["id"])["status"], "条目评审中", "缺字段时保持评审中")
    eq(any(b.get("字典条目编号") == "DICT_NEW" for b in store.rows("release")), False, "没有留下发布批次")


def test_metadata_reads_published_version() -> None:
    fresh()
    eq(D.get_entry(4)["status"], "条目评审中", "DICT_PAY_CHANNEL 处于 v5 评审")
    eq(M.get_entry(2)["数据字典版本"], "v4", "元数据读取已发布 v4")
    eq(M.get_entry(2)["枚举说明"], "CARD、CASH", "元数据不含评审中的 TRANSFER")
    res, _ = D.run_action(4, "发布条目")
    assert res is not None
    eq(M.get_entry(2)["数据字典版本"], "v5", "发布后元数据切到 v5")
    eq(M.get_entry(2)["枚举说明"], "CARD、CASH、TRANSFER", "发布后元数据枚举完整")
    eq(M.get_entry(2)["发布批次"], res["release_batch"], "两端使用同一发布批次")
    eq(res["versions"][-1]["枚举说明"], "CARD、CASH、TRANSFER", "v5 已冻结为不可变版本")


def test_both_modules_audited_under_same_batch() -> None:
    fresh()
    res, _ = D.run_action(4, "发布条目")
    batch = res["release_batch"]
    audit = [a for a in store.rows("auditlog") if a.get("发布批次") == batch]
    eq(sorted(a["操作表名"] for a in audit), ["dictionary", "metamodel"], "两个模块同步审计")
    eq(all(batch in a["变更原因"] for a in audit), True, "审计原因带批次号")


def test_repeat_publish_rejected() -> None:
    fresh()
    assert D.run_action(4, "发布条目")[0] is not None
    res, msg = D.run_action(4, "发布条目")
    eq(res, None, "重复发布被拒绝")
    eq(len(D.get_entry(4)["versions"]), 2, "重复发布不新增版本")


def test_modify_published_creates_new_version() -> None:
    fresh()
    assert D.run_action(4, "发布条目")[0] is not None
    res, msg = D.update_entry(4, {"枚举说明": "CARD、CASH、TRANSFER、OTHER"})
    assert res is not None, msg
    eq(res["status"], "条目评审中", "修改已发布条目进入评审")
    eq(res["字典版本"], "v6", "自动推进到 v6")
    eq(res["versions"][1]["枚举说明"], "CARD、CASH、TRANSFER", "v5 内容保持不可变")
    eq(M.get_entry(2)["数据字典版本"], "v5", "评审期间元数据仍读 v5")
    res, _ = D.run_action(4, "发布条目")
    assert res is not None
    eq(M.get_entry(2)["数据字典版本"], "v6", "v6 发布后元数据同步")
    eq(len(D.get_entry(4)["versions"]), 3, "三个已发布版本依次保留")


def test_version_conflict_uses_published_version() -> None:
    fresh()
    entry, _, _ = D.create_entry({
        "条目编号": "DICT_CONFLICT", "表名": "conf", "字段名称": "f",
        "字段类型": "INT", "字段长度": "10", "业务含义": "x", "枚举说明": "1",
        "字典版本": "v3", "元数据项id": 2,
    })
    assert D.run_action(entry["id"], "提交条目评审")[0] is not None
    res, msg = D.run_action(entry["id"], "发布条目")
    eq(res, None, "低于已发布版本的新发布被拒绝")
    eq(D.get_entry(entry["id"])["status"], "条目评审中", "冲突后保持评审中")
    eq(M.get_entry(2)["数据字典版本"], "v4", "元数据保持已发布 v4")


def test_batch_numbers_stay_unique_across_rebumps() -> None:
    fresh()
    assert D.run_action(4, "发布条目")[0] is not None
    D.update_entry(4, {"枚举说明": "CARD、CASH、TRANSFER、OTHER"})
    res, _ = D.run_action(4, "发布条目")
    assert res is not None
    batches = [b["批次编号"] for b in store.rows("release")]
    eq(len(batches), len(set(batches)), "换版后批次号不重复")
    eq(res["release_batch"], store.rows("release")[-1]["批次编号"], "详情批次与批次表一致")
    eq(M.get_entry(2)["发布批次"], res["release_batch"], "元数据读到同一批次")


def test_void_from_review_and_published() -> None:
    fresh()
    res, _ = D.run_action(2, "作废条目")
    eq(res["status"], "已作废", "评审中可直接作废")
    res, msg = D.run_action(2, "作废条目")
    eq(res, None, "作废不能重复")
    res, msg = D.run_action(2, "发布条目")
    eq(res, None, "已作废不能发布")


def test_metadata_sync_failure_falls_back_to_review() -> None:
    fresh()
    assert D.run_action(4, "发布条目")[0] is not None
    eq(M.get_entry(2)["数据字典版本"], "v5", "发布后元数据 v5")
    original = release_rules.write_audit

    def boom(**kwargs):
        raise RuntimeError("audit store failed")

    release_rules.write_audit = boom
    try:
        res, msg = M.run_action(2, "同步元数据属性")
    finally:
        release_rules.write_audit = original
    eq(res, None, "同步失败返回失败")
    eq(D.get_entry(4)["status"], "条目评审中", "字典回退到评审中")
    eq(len(D.get_entry(4)["versions"]), 2, "已发布版本历史不被破坏")


def test_higher_metadata_version_wins() -> None:
    fresh()
    a, _, _ = D.create_entry({
        "条目编号": "DICT_HIGH", "表名": "conf", "字段名称": "f",
        "字段类型": "INT", "字段长度": "10", "业务含义": "a", "枚举说明": "1",
        "字典版本": "v5", "元数据项id": 1,
    })
    b, _, _ = D.create_entry({
        "条目编号": "DICT_LOW", "表名": "conf", "字段名称": "f",
        "字段类型": "INT", "字段长度": "10", "业务含义": "b", "枚举说明": "2",
        "字典版本": "v4", "元数据项id": 1,
    })
    assert D.run_action(a["id"], "提交条目评审")[0] is not None
    assert D.run_action(a["id"], "发布条目")[0] is not None
    assert D.run_action(b["id"], "提交条目评审")[0] is not None
    res, msg = D.run_action(b["id"], "发布条目")
    eq(res, None, "旧版本不覆盖已同步的更高版本")
    eq(M.get_entry(1)["数据字典版本"], "v5", "元数据保持 v5")
    eq(D.get_entry(b["id"])["status"], "条目评审中", "未获接受的字典留在评审中")


def test_publish_failure_rolls_back() -> None:
    fresh()
    entry, _, _ = D.create_entry({
        "条目编号": "DICT_FAIL", "表名": "fail", "字段名称": "f",
        "字段类型": "INT", "字段长度": "10", "业务含义": "x", "枚举说明": "1",
        "字典版本": "v1", "元数据项id": 3,
    })
    assert D.run_action(entry["id"], "提交条目评审")[0] is not None
    original = release_rules.record_batch
    release_rules.record_batch = lambda **kwargs: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        res, msg = D.run_action(entry["id"], "发布条目")
    finally:
        release_rules.record_batch = original
    eq(res, None, "写入失败时发布失败")
    eq(D.get_entry(entry["id"])["status"], "条目评审中", "字典回退到评审中")
    eq(M.get_entry(3)["数据字典版本"], "v1", "元数据未被半成品修改")
    eq(len(store.rows("release")), 2, "没有留下发布批次")


def test_concurrent_publish_single_winner() -> None:
    fresh()
    entry, _, _ = D.create_entry({
        "条目编号": "DICT_RACE", "表名": "race", "字段名称": "f",
        "字段类型": "INT", "字段长度": "10", "业务含义": "x", "枚举说明": "1",
        "字典版本": "v1", "元数据项id": 1,
    })
    assert D.run_action(entry["id"], "提交条目评审")[0] is not None
    results: list[tuple] = []

    def worker() -> None:
        results.append(D.run_action(entry["id"], "发布条目"))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    eq(sum(1 for r in results if r[0] is not None), 1, "并发发布只有一个成功")
    eq(len(D.get_entry(entry["id"])["versions"]), 1, "只登记一个已发布版本")


def main() -> None:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        print(f"[{test.__name__}]")
        test()
    print(f"\n{len(tests)} tests passed")


if __name__ == "__main__":
    main()
