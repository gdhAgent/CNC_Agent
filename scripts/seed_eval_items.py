#!/usr/bin/env python3
"""
seed_eval_items —— 标注 100 条评估集 → log.eval_items（W4.7）

分布（PLAN §7）：alarm_code 40 / symptom 30 / maintenance 15 / device_history 10 / multi_turn 5

expected 引用解析（seed 时动态反查 id，避免硬编码过期）：
  alarm  → code_norm 查 kb.alarms
  chunk  → heading_path 子串查 kb.chunks
  machine→ asset_no 查 ops.machines（device_history 用，评估走工具路径）

幂等：TRUNCATE log.eval_items 后重灌（评估集是可重建的测试资产）。
用法：python scripts/seed_eval_items.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

import psycopg  # noqa: E402

from app.config import get_settings  # noqa: E402

# (question, q_type, expected_spec, difficulty, note)
# expected_spec: [("alarm", "SV0401"), ("chunk", "主轴系统说明"), ("machine", "CN-003")]
ITEMS: list[tuple[str, str, list[tuple[str, str]], int, str]] = []


def A(code: str):
    return ("alarm", code)


def C(path: str):
    return ("chunk", path)


def M(asset: str):
    return ("machine", asset)


# ============ alarm_code 40 条 ============
_alarm_q = [
    ("SV0401 报警是什么意思", [A("SV0401")], 1, "基础：单码查询"),
    ("SV0404 怎么处理", [A("SV0404")], 1, "基础：单码查询"),
    ("SV0417 是什么故障", [A("SV0417")], 1, "基础：单码查询"),
    ("SV0420 同步误差报警排查", [A("SV0420")], 2, "双驱同步误差"),
    ("SV0440 伺服轴过载怎么办", [A("SV0440")], 2, "过载处置"),
    ("SP0740 报警", [A("SP0740")], 1, "刚性攻丝主轴偏差"),
    ("SP0742 主轴电机过热", [A("SP0742")], 1, "主轴过热"),
    ("SP1202 报警怎么处理", [A("SP1202")], 1, "主轴定向超时"),
    ("SP1210 换刀移动量溢出", [A("SP1210")], 2, "换刀"),
    ("OT0500 硬限位超程怎么复位", [A("OT0500")], 1, "超程复位"),
    ("OT0700 软限位超程", [A("OT0700")], 1, "软限位"),
    ("PS0111 浮点运算溢出报警", [A("PS0111")], 2, "宏程序"),
    ("PS0112 除零错误", [A("PS0112")], 2, "宏程序"),
    ("PS0200 刚性攻丝 S 代码非法", [A("PS0200")], 2, "攻丝 S 码"),
    ("EX1086 是什么报警", [A("EX1086")], 1, "PMC 急停"),
    ("EMG 急停报警", [A("EMG")], 1, "特殊码无数字"),
    ("AL10 母线欠压报警", [A("AL10")], 1, "三菱伺服"),
    ("AL20 编码器无信号", [A("AL20")], 1, "三菱伺服"),
    ("AL23 速度跟随偏差", [A("AL23")], 2, "三菱伺服"),
    ("AL24 接地故障排查", [A("AL24")], 2, "三菱伺服"),
    ("AL30 过再生报警", [A("AL30")], 2, "再生电阻"),
    ("AL37 伺服初始参数错误", [A("AL37")], 2, "参数"),
    ("AL46 电机过热", [A("AL46")], 1, "三菱电机过热"),
    ("AL50 持续过载报警", [A("AL50")], 2, "持续过载"),
    ("CM03 主轴通信错误", [A("CM03")], 2, "三菱主轴通信"),
    ("查一下 SV0401 和 SV0404 的区别", [A("SV0401"), A("SV0404")], 2, "多码"),
    ("机床报 SV0417 又报 SV0440", [A("SV0417"), A("SV0440")], 2, "多码"),
    ("伺服 V-Ready 信号关闭怎么查", [A("SV0401")], 2, "按名称"),
    ("数字伺服参数非法是什么", [A("SV0417")], 3, "按名称"),
    ("伺服轴过载报警怎么降载", [A("SV0440")], 2, "按名称"),
    ("急停回路的 PMC 报警", [A("EX1086")], 3, "按概念"),
    ("V-Ready 信号异常闭合", [A("SV0404")], 3, "按名称"),
    ("SP0742 主轴过热保护", [A("SP0742")], 2, "按名称"),
    ("定向完成信号超时 SP1202", [A("SP1202")], 2, "码带名"),
    ("SV0420 同步误差大", [A("SV0420")], 3, "按名称"),
    ("AL23 速度跟随跟不上", [A("AL23")], 3, "按现象"),
    ("伺服放大器 V-Ready 掉了", [A("SV0401"), A("SV0404")], 3, "按现象反查码"),
    ("主轴定向报警 SP1202 处理流程", [A("SP1202"), C("主轴系统说明")], 2, "码+章节"),
    ("PS0200 攻丝程序错误排查", [A("PS0200")], 2, "程序错误"),
    ("过载报警 SV0440 常见原因", [A("SV0440")], 2, "过载原因"),
]
ITEMS += [(q, "alarm_code", exp, d, n) for q, exp, d, n in _alarm_q]
assert len([i for i in ITEMS if i[1] == "alarm_code"]) == 40

# ============ symptom 30 条 ============
_symptom_q = [
    ("主轴转起来有异响", [A("SP0740"), C("主轴系统说明")], 2, "主轴异响"),
    ("主轴定位不准", [C("主轴系统说明")], 2, "主轴定位"),
    ("主轴过热冒烟", [A("SP0742")], 2, "主轴过热"),
    ("伺服轴移动时振动大", [A("SV0440"), C("进给轴系统说明")], 2, "伺服振动"),
    ("加工件表面出现振纹", [C("进给轴系统说明"), A("SV0420")], 3, "振纹"),
    ("X 轴超程报警怎么解除", [A("OT0500"), A("OT0700")], 2, "超程"),
    ("开机就报急停", [A("EX1086"), A("EMG")], 1, "急停"),
    ("程序运行时除零报错", [A("PS0112")], 2, "宏程序除零"),
    ("换刀时报移动量溢出", [A("SP1210")], 2, "换刀"),
    ("刚性攻丝位置偏差", [A("SP0740"), A("PS0200")], 2, "攻丝"),
    ("主轴定向转不到位", [A("SP1202")], 2, "定向"),
    ("伺服轴报过载停机", [A("SV0440")], 1, "过载停机"),
    ("编码器信号丢失查哪里", [A("AL20")], 2, "编码器"),
    ("母线电压偏低报警", [A("AL10")], 2, "母线欠压"),
    ("接地漏电报警", [A("AL24")], 2, "接地"),
    ("伺服电机烫手", [A("AL46"), A("SP0742")], 2, "电机过热"),
    ("双驱轴同步偏移", [A("SV0420")], 3, "双驱同步"),
    ("进给轴跟随滞后", [A("AL23"), C("进给轴系统说明")], 3, "跟随滞后"),
    ("主轴箱有啸叫", [C("主轴系统说明"), A("SP0740")], 3, "主轴啸叫"),
    ("切削时主轴转速掉", [A("CM03"), C("主轴系统说明")], 3, "转速掉"),
    ("换刀臂动作异常", [C("主轴系统说明"), A("SP1210")], 3, "换刀臂"),
    ("急停后复位报警", [A("EX1086"), C("主轴系统说明")], 2, "急停复位"),
    ("冷却液流量不足", [C("主轴系统说明")], 3, "冷却液"),
    ("液压站压力波动", [C("主轴系统说明")], 3, "液压"),
    ("主轴定向时震动", [A("SP1202"), C("主轴系统说明")], 3, "定向震动"),
    ("伺服放大器嗡嗡响", [A("SV0401"), A("SV0440")], 3, "放大器异响"),
    ("攻丝时螺纹乱牙", [A("SP0740"), A("PS0200")], 3, "乱牙"),
    ("X 轴走不到位", [A("SV0420"), C("进给轴系统说明")], 3, "走不到位"),
    ("主轴刀柄拉不紧", [C("主轴系统说明"), A("SP1210")], 3, "拉刀"),
    ("机床加工精度突然变差", [C("进给轴系统说明"), A("SV0420")], 3, "精度变差"),
]
ITEMS += [(q, "symptom", exp, d, n) for q, exp, d, n in _symptom_q]
assert len([i for i in ITEMS if i[1] == "symptom"]) == 30

# ============ maintenance 15 条 ============
_maint_q = [
    ("月点检都检查什么内容", [C("日常点检与保养")], 1, "月点检"),
    ("日常保养规范", [C("日常点检与保养")], 1, "保养规范"),
    ("每周点检项目", [C("日常点检与保养")], 2, "周点检"),
    ("开机前需要检查什么", [C("日常点检与保养")], 1, "开机检查"),
    ("液压系统保养周期", [C("日常点检与保养")], 2, "液压保养"),
    ("润滑系统维护", [C("日常点检与保养")], 2, "润滑维护"),
    ("主轴冷却液检查", [C("日常点检与保养")], 2, "冷却液检查"),
    ("伺服电机保养要点", [C("日常点检与保养")], 3, "伺服保养"),
    ("导轨润滑怎么做", [C("日常点检与保养")], 2, "导轨润滑"),
    ("换刀机构保养", [C("日常点检与保养")], 3, "换刀保养"),
    ("点检记录表有哪些项目", [C("日常点检与保养")], 2, "点检记录"),
    ("保养中发现主轴异响怎么办", [C("日常点检与保养"), A("SP0740")], 3, "保养+异常"),
    ("定期保养周期多久一次", [C("日常点检与保养")], 2, "周期"),
    ("气路保养检查", [C("日常点检与保养")], 3, "气路"),
    ("电气柜除尘保养", [C("日常点检与保养")], 3, "电气柜"),
]
ITEMS += [(q, "maintenance", exp, d, n) for q, exp, d, n in _maint_q]
assert len([i for i in ITEMS if i[1] == "maintenance"]) == 15

# ============ device_history 10 条 ============
_dev_q = [
    ("CN-003 最近有没有维修记录", [M("CN-003")], 2, "单机历史"),
    ("3号加工中心报过什么故障", [M("CN-003")], 2, "按名称"),
    ("CN-011 这台机器近 90 天维修情况", [M("CN-011")], 2, "指定机台"),
    ("5号机出现过主轴故障吗", [M("CN-005")], 3, "按名称"),
    ("CN-020 龙门机近期故障统计", [M("CN-020")], 3, "龙门机"),
    ("哪台机器伺服故障最多", [M("CN-001")], 3, "横向统计（代表性）"),
    ("CN-007 报过 SV 系列报警吗", [M("CN-007")], 3, "码+机台"),
    ("车削中心最近换过什么备件", [M("CN-009")], 3, "车削中心"),
    ("CN-015 平均停机多久", [M("CN-015")], 3, "停机时长"),
    ("CN-025 龙门加工中心维护记录", [M("CN-025")], 3, "维护记录"),
]
ITEMS += [(q, "device_history", exp, d, n) for q, exp, d, n in _dev_q]
assert len([i for i in ITEMS if i[1] == "device_history"]) == 10

# ============ multi_turn 5 条 ============
_mt_q = [
    ("刚才报了 SV0401，说要查急停回路，具体检查哪几个点",
     [A("SV0401"), C("主轴系统说明")], 3, "多轮：追问检查点"),
    ("前面说主轴异响，现在又报了 SP0740，是不是同一问题",
     [A("SP0740"), C("主轴系统说明")], 3, "多轮：关联两问题"),
    ("上次换刀报警后修好了，今天又报 SP1210",
     [A("SP1210")], 3, "多轮：复发"),
    ("之前查过伺服过载 SV0440，现在 5 号机又报了",
     [A("SV0440"), M("CN-005")], 3, "多轮：跨机台"),
    ("接着说，编码器无信号 AL20 一般先查什么",
     [A("AL20")], 3, "多轮：接续提问"),
]
ITEMS += [(q, "multi_turn", exp, d, n) for q, exp, d, n in _mt_q]
assert len([i for i in ITEMS if i[1] == "multi_turn"]) == 5

assert len(ITEMS) == 100, f"总数应为 100，实际 {len(ITEMS)}"


def _resolve(cur, spec_type: str, key: str) -> int | None:
    """动态反查 id：alarm→code_norm；chunk→heading_path 子串；machine→asset_no"""
    if spec_type == "alarm":
        cur.execute("SELECT id FROM kb.alarms WHERE code_norm = %s", [key])
    elif spec_type == "chunk":
        # 只锚定 level=2 子块（父块不参与检索，评估只能评可召回的子块）
        cur.execute(
            "SELECT id FROM kb.chunks WHERE level = 2 AND heading_path LIKE %s ORDER BY id LIMIT 1",
            [f"%{key}%"],
        )
    elif spec_type == "machine":
        cur.execute("SELECT id FROM ops.machines WHERE asset_no = %s", [key])
    else:
        return None
    row = cur.fetchone()
    return int(row[0]) if row else None


def main() -> None:
    cfg = get_settings()
    resolved_ok = 0
    unresolved: list[str] = []

    with psycopg.connect(**cfg.db_dsn_kwargs()) as conn, conn.cursor() as cur:
        cur.execute("TRUNCATE log.eval_items")
        for question, q_type, spec, difficulty, note in ITEMS:
            expected: list[dict] = []
            for st, key in spec:
                rid = _resolve(cur, st, key)
                if rid is None:
                    unresolved.append(f"{q_type}: {key}")
                    continue
                expected.append({"type": st, "id": rid})
            if not expected:
                continue
            # expected 是数组：[{"type":"alarm","id":N}, ...]（§3.2 eval_items 契约）
            cur.execute(
                """
                INSERT INTO log.eval_items (question, q_type, expected, difficulty, note)
                VALUES (%s, %s, %s::jsonb, %s, %s)
                """,
                [question, q_type, json.dumps(expected, ensure_ascii=False), difficulty, note],
            )
            resolved_ok += 1
        conn.commit()

        cur.execute("SELECT q_type, count(*) FROM log.eval_items GROUP BY q_type ORDER BY q_type")
        print("[DONE] 评估集入库：")
        for row in cur.fetchall():
            print(f"  {row[0]:<15} {row[1]} 条")
        cur.execute("SELECT count(*) FROM log.eval_items")
        print(f"  总计 {cur.fetchone()[0]} 条")

    if unresolved:
        print("\n[WARN] 未解析的 expected（已跳过该条目）：")
        for u in unresolved:
            print(f"  {u}")
    else:
        print("\n[OK] 所有 expected 引用解析成功")


if __name__ == "__main__":
    main()
