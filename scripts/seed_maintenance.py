"""
scripts.seed_maintenance —— 造 200 条仿真维修工单 → ops.maintenance_logs

W2.5 入口。`is_demo=true` 标记。

设计：
- 从 ops.machines 拉所有机器（包含 demo 与非 demo，但只对 demo 造工单）
- 200 条按机器数大致均匀分布
- 故障类型分布：机械 35% / 电气 30% / 液压 15% / 气动 10% / 软件 10%
- 报警码：60% 命中（按机器品牌从 kb.alarms 取），40% 无
- 时间：过去 365 天内随机
- 中文症状 / 根因 / 处置：每个故障类型一个小型词池 → 拼出 30~80 字描述
- downtime_min：5~480 随机
- parts_used：~30% 概率附带 1~3 个零件名

向量化不在此脚本范围，跑完后用：
    python scripts/vectorize_maintenance.py

用法：
    python scripts/seed_maintenance.py                  # 默认 200 条
    python scripts/seed_maintenance.py --count 50 --clear
"""

from __future__ import annotations

import argparse
import random
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import psycopg  # noqa: E402
from dotenv import dotenv_values  # noqa: E402
from psycopg.types.json import Jsonb  # noqa: E402

DEFAULTS = {
    "PG_HOST": "127.0.0.1",
    "PG_PORT": "5432",
    "PG_SUPERUSER": "postgres",
    "PG_SUPERPASSWORD": "",
    "PG_DB": "cnc_kb",
}

DEFAULT_SEED = 20260830

# ===== 文本池 =====

FAULT_TYPES = ["机械", "电气", "液压", "气动", "软件"]
FAULT_TYPE_WEIGHTS = [0.35, 0.30, 0.15, 0.10, 0.10]

SYMPTOM_BY_TYPE: dict[str, list[str]] = {
    "机械": [
        "主轴转动时有异响", "X 轴移动出现抖动", "Z 轴回零后位置偏差",
        "加工件表面出现振纹", "主轴锥孔有损伤", "丝杠背隙过大",
        "导轨润滑不良", "换刀机械手卡死", "工作台移动有顿挫",
    ],
    "电气": [
        "伺服放大器报警", "编码器信号丢失", "急停回路异常",
        "主轴电机温度过高", "电池欠压报警", "I/O 模块通信中断",
        "光栅尺反馈异常", "继电器触点粘连",
    ],
    "液压": [
        "油缸推力不足", "油温过高", "液压站压力不稳",
        "油管接头渗油", "油过滤器堵塞",
    ],
    "气动": [
        "气源压力偏低", "气缸动作缓慢", "气路接头漏气",
        "吹气阀不动作",
    ],
    "软件": [
        "加工程序无法执行", "参数被误修改", "宏程序调用异常",
        "M 代码功能失效", "G 代码指令报警",
    ],
}

ROOT_CAUSE_BY_TYPE: dict[str, list[str]] = {
    "机械": [
        "主轴轴承磨损", "丝杠润滑不足", "导轨压板松动",
        "联轴器松动", "机械手凸轮磨损", "锥孔内有切屑",
    ],
    "电气": [
        "伺服参数被复位", "编码器线缆受干扰", "急停按钮机械卡死",
        "电池电压低于 2.8V", "I/O 模块触点氧化",
    ],
    "液压": [
        "油泵效率下降", "冷却器堵塞", "溢流阀调压漂移",
        "密封圈老化",
    ],
    "气动": [
        "气源三联件堵塞", "气管老化开裂", "气缸密封圈磨损",
        "电磁阀线圈烧毁",
    ],
    "软件": [
        "DNC 传输中断", "参数被覆盖", "宏变量未初始化",
        "PLC 梯形图逻辑错误",
    ],
}

ACTION_BY_TYPE: dict[str, list[str]] = {
    "机械": [
        "更换主轴轴承并重新调试动平衡", "补充润滑脂并检查油路",
        "重新调整导轨压板间隙", "紧固联轴器并对中",
        "清理锥孔内切屑", "更换机械手凸轮",
    ],
    "电气": [
        "从备份恢复参数并更换电池", "检查编码器线缆屏蔽接地",
        "检查急停回路并复位", "更换 I/O 模块",
        "清理触点并紧固接线",
    ],
    "液压": [
        "更换油泵", "清洗冷却器", "重新调整溢流阀压力",
        "更换密封圈",
    ],
    "气动": [
        "清洗三联件", "更换气管", "更换气缸密封件",
        "更换电磁阀",
    ],
    "软件": [
        "重新传输加工程序", "恢复出厂参数", "修正宏程序逻辑",
        "修复 PLC 梯形图",
    ],
}

PARTS_POOL = [
    "主轴轴承 7014", "丝杠轴承", "导轨润滑脂",
    "伺服电机风扇", "编码器线缆 5m", "电池 A98L-0031-0025",
    "I/O 模块 A02B-0303-C001", "液压油 L-HM46", "气缸密封件",
    "电磁阀 DC24V", "继电器 MY2N", "熔断器 5A",
]

ENGINEERS = ["张工", "李工", "王工", "赵工", "陈工", "刘工", "杨工", "黄工"]


def get_db_cfg() -> dict:
    if not (PROJECT_ROOT / ".env").exists():
        print("[ERROR] .env 不存在", file=sys.stderr)
        sys.exit(2)
    env = {k: v for k, v in dotenv_values(PROJECT_ROOT / ".env").items() if v is not None}
    merged = {**DEFAULTS, **env}
    if not merged.get("PG_SUPERPASSWORD"):
        print("[ERROR] PG_SUPERPASSWORD 未配置", file=sys.stderr)
        sys.exit(2)
    return merged


def _fetch_machines(conn) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, asset_no, brand, model, controller "
            "FROM ops.machines WHERE is_demo=true ORDER BY id"
        )
        cols = [d.name for d in cur.description or []]
        return [dict(zip(cols, r, strict=False)) for r in cur.fetchall()]


def _fetch_alarm_codes(conn) -> dict[str, list[str]]:
    """返回 brand -> list[code]"""
    with conn.cursor() as cur:
        cur.execute("SELECT brand, code FROM kb.alarms")
        out: dict[str, list[str]] = {}
        for brand, code in cur.fetchall():
            out.setdefault(brand, []).append(code)
        return out


def _make_one(
    rng: random.Random,
    machine: dict,
    alarm_pool_by_brand: dict[str, list[str]],
    seq: int,
    now: datetime,
    days_back: int,
) -> dict:
    """造 1 条 maintenance_log 字典。"""
    fault_type = rng.choices(FAULT_TYPES, weights=FAULT_TYPE_WEIGHTS, k=1)[0]
    symptom = rng.choice(SYMPTOM_BY_TYPE[fault_type])
    root_cause = rng.choice(ROOT_CAUSE_BY_TYPE[fault_type])
    action = rng.choice(ACTION_BY_TYPE[fault_type])

    # 60% 命中报警码：按机器品牌筛
    alarm_code = None
    if rng.random() < 0.6:
        codes = alarm_pool_by_brand.get(machine["brand"], [])
        if codes:
            alarm_code = rng.choice(codes)

    # 30% 概率附带 parts_used
    parts_used: list[dict] = []
    if rng.random() < 0.3:
        n = rng.randint(1, 3)
        chosen = rng.sample(PARTS_POOL, k=min(n, len(PARTS_POOL)))
        parts_used = [{"name": p, "qty": rng.randint(1, 2)} for p in chosen]

    # 时间：过去 N 天内
    delta = timedelta(
        days=rng.randint(0, days_back),
        hours=rng.randint(0, 23),
        minutes=rng.randint(0, 59),
    )
    started_at = now - delta
    downtime = rng.randint(5, 480)
    finished_at = started_at + timedelta(minutes=downtime)

    order_no = f"WO-{started_at.strftime('%Y%m')}-{seq:05d}"
    return {
        "machine_id": machine["id"],
        "order_no": order_no,
        "alarm_code": alarm_code,
        "fault_type": fault_type,
        "symptom": symptom,
        "root_cause": root_cause,
        "action_taken": action,
        "parts_used": parts_used,
        "engineer": rng.choice(ENGINEERS),
        "downtime_min": downtime,
        "started_at": started_at,
        "finished_at": finished_at,
        "is_demo": True,
    }


_INSERT_SQL = """
INSERT INTO ops.maintenance_logs (
    machine_id, order_no, alarm_code, fault_type,
    symptom, root_cause, action_taken, parts_used,
    engineer, downtime_min, started_at, finished_at, is_demo
) VALUES (
    %(machine_id)s, %(order_no)s, %(alarm_code)s, %(fault_type)s,
    %(symptom)s, %(root_cause)s, %(action_taken)s, %(parts_used)s::jsonb,
    %(engineer)s, %(downtime_min)s, %(started_at)s, %(finished_at)s, %(is_demo)s
)
ON CONFLICT (order_no) DO NOTHING
"""


def _clear_demo(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM ops.maintenance_logs WHERE is_demo=true")
        return cur.rowcount


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="W2.5: 造 N 条仿真维修工单 → ops.maintenance_logs")
    p.add_argument("--count", type=int, default=200, help="工单数量（默认 200）")
    p.add_argument("--days", type=int, default=365, help="时间分布天数（默认 365）")
    p.add_argument("--seed", type=int, default=DEFAULT_SEED, help="随机种子")
    p.add_argument("--clear", action="store_true", help="先清掉所有 is_demo=true 的工单")
    args = p.parse_args(argv)

    if args.count <= 0 or args.days <= 0:
        print("[ERROR] --count / --days 必须 > 0", file=sys.stderr)
        return 2

    db = get_db_cfg()
    print(f"[INFO] 连库: {db['PG_HOST']}:{db['PG_PORT']}, db={db['PG_DB']}")
    print(f"[INFO] 生成 {args.count} 条工单；days={args.days}；seed={args.seed}")

    rng = random.Random(args.seed)

    with psycopg.connect(
        host=db["PG_HOST"], port=int(db["PG_PORT"]),
        user=db["PG_SUPERUSER"], password=db["PG_SUPERPASSWORD"],
        dbname=db["PG_DB"],
    ) as conn:
        machines = _fetch_machines(conn)
        if not machines:
            print("[ERROR] ops.machines 中无 demo 机器，请先跑 seed_machines.py", file=sys.stderr)
            return 2

        alarm_pool = _fetch_alarm_codes(conn)
        print(f"[INFO] 找到机器 {len(machines)} 台；报警码品牌分布: "
              f"{', '.join(f'{b}={len(c)}' for b, c in alarm_pool.items())}")

        if args.clear:
            n = _clear_demo(conn)
            conn.commit()
            print(f"[INFO] 清掉旧 demo 工单 {n} 条")

        now = datetime.now(UTC).replace(microsecond=0)
        rows = [
            _make_one(rng, m, alarm_pool, i + 1, now, args.days)
            for i, m in enumerate(machines * ((args.count // len(machines)) + 1))
        ]
        rows = rows[: args.count]

        try:
            with conn.cursor() as cur:
                for row in rows:
                    payload = dict(row)
                    payload["parts_used"] = Jsonb(row["parts_used"])  # JSONB wrapper
                    cur.execute(_INSERT_SQL, payload)
        except Exception as e:  # noqa: BLE001
            conn.rollback()
            print(f"[ERROR] 入库失败: {e}", file=sys.stderr)
            return 1
        conn.commit()

        # 汇总
        with conn.cursor() as cur:
            cur.execute(
                "SELECT fault_type, count(*) FROM ops.maintenance_logs "
                "WHERE is_demo=true GROUP BY fault_type ORDER BY fault_type"
            )
            print("[DONE] 各故障类型分布:")
            for ft, n in cur.fetchall():
                print(f"  {ft}: {n}")
            cur.execute(
                "SELECT count(*) FILTER (WHERE alarm_code IS NOT NULL) AS with_code, "
                "       count(*) FILTER (WHERE alarm_code IS NULL) AS without_code "
                "FROM ops.maintenance_logs WHERE is_demo=true"
            )
            with_code, without_code = cur.fetchone()
            print(f"[DONE] 报警码: 有 {with_code} / 无 {without_code}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
