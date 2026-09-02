"""
scripts.seed_machines —— 造 30 台仿真 CNC 设备台账 → ops.machines

W2.5 入口。`is_demo=true` 标记。

设计：
- 品牌 / 机型分布按"贴近真实工厂"拍脑袋（可改）：
    FANUC        12 台（VMC850 ×4 + DMC650 ×4 + TC500 ×4）
    MITSUBISHI   10 台（VMC850 ×4 + M70-车 ×3 + M80-铣 ×3）
    SIEMENS       5 台（DMC650 ×3 + VMC850 ×2）
    HEIDENHAIN    3 台（VMC850 ×2 + TC500 ×1）
- 3 个车间 / 6 条产线（轮询分配）
- asset_no = CN-001..CN-030（保证唯一）
- install_date 随机在 2018..2024
- 随机种子固定 → 每次跑出来结果一致

用法：
    python scripts/seed_machines.py                  # 默认 30 台
    python scripts/seed_machines.py --count 50       # 改数量（按比例扩）
    python scripts/seed_machines.py --clear          # 先 TRUNCATE 再插
"""

from __future__ import annotations

import argparse
import random
import sys
from datetime import date, timedelta
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

# 品牌 + 机型分布（总 30 台）
BRAND_MODEL_PLAN: list[tuple[str, str, str]] = [
    # (brand, controller, count)
    ("FANUC", "0i-MF", 4),
    ("FANUC", "0i-MF", 4),
    ("FANUC", "0i-TF", 4),
    ("MITSUBISHI", "M70", 4),
    ("MITSUBISHI", "M70", 3),
    ("MITSUBISHI", "M80", 3),
    ("SIEMENS", "828D", 3),
    ("SIEMENS", "828D", 2),
    ("HEIDENHAIN", "TNC640", 2),
    ("HEIDENHAIN", "TNC640", 1),
]

WORKSHOPS = ["一车间", "二车间", "三车间"]
LINES = ["1线", "2线", "3线", "4线", "5线", "6线"]

MODELS = ["VMC850", "DMC650", "TC500", "VMC1060", "DMC850"]


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


def _build_machines(count: int, seed: int) -> list[dict]:
    """生成 N 台机器；保持品牌/机型分布按比例缩放。"""
    rng = random.Random(seed)
    # 把 (brand, controller) 摊平成行
    slots: list[tuple[str, str, str]] = []
    plan_total = sum(c for _, _, c in BRAND_MODEL_PLAN)
    for brand, controller, _n in BRAND_MODEL_PLAN:
        slots.append((brand, controller, _pick_model(brand, controller)))

    # 按 count 等比缩放
    rows: list[dict] = []
    for i in range(count):
        # 按 plan 比例挑一个
        idx = int(i * plan_total / count) % plan_total
        brand_idx = 0
        running = 0
        for j, (_, _, n) in enumerate(BRAND_MODEL_PLAN):
            running += n
            if idx < running:
                brand_idx = j
                break
        brand, controller, model = slots[brand_idx]

        # name: 立式加工中心-03 / 龙门-02 等
        name = f"{_name_for(brand, model)}-{i + 1:02d}"
        asset_no = f"CN-{i + 1:03d}"
        install_date = date(2018, 1, 1) + timedelta(days=rng.randint(0, 365 * 6))
        workshop = WORKSHOPS[i % len(WORKSHOPS)]
        line_no = LINES[i % len(LINES)]
        spec = {
            "max_rpm": rng.choice([8000, 10000, 12000, 15000]),
            "travel_x_mm": rng.choice([800, 1000, 1200]),
            "travel_y_mm": rng.choice([500, 600, 800]),
            "travel_z_mm": rng.choice([500, 600, 800]),
        }
        rows.append({
            "asset_no": asset_no,
            "name": name,
            "brand": brand,
            "model": model,
            "controller": controller,
            "workshop": workshop,
            "line_no": line_no,
            "install_date": install_date,
            "status": "running",
            "spec": spec,
            "is_demo": True,
        })
    return rows


def _pick_model(brand: str, controller: str) -> str:
    """按品牌挑个看起来合理的型号；后续真实数据可改。"""
    if brand == "FANUC":
        return "VMC850" if controller == "0i-MF" else "TC500"
    if brand == "MITSUBISHI":
        return "VMC850" if controller == "M70" else "DMC650"
    if brand == "SIEMENS":
        return "DMC650"
    return "VMC850"


def _name_for(brand: str, model: str) -> str:
    if model.startswith("VMC"):
        return "立式加工中心"
    if model.startswith("DMC"):
        return "龙门加工中心"
    if model.startswith("TC"):
        return "车削中心"
    return "加工中心"


_INSERT_SQL = """
INSERT INTO ops.machines (
    asset_no, name, brand, model, controller,
    workshop, line_no, install_date, status, spec, is_demo
) VALUES (
    %(asset_no)s, %(name)s, %(brand)s, %(model)s, %(controller)s,
    %(workshop)s, %(line_no)s, %(install_date)s, %(status)s, %(spec)s::jsonb, %(is_demo)s
)
ON CONFLICT (asset_no) DO UPDATE SET
    name = EXCLUDED.name,
    brand = EXCLUDED.brand,
    model = EXCLUDED.model,
    controller = EXCLUDED.controller,
    workshop = EXCLUDED.workshop,
    line_no = EXCLUDED.line_no,
    install_date = EXCLUDED.install_date,
    spec = EXCLUDED.spec
"""


def _clear_demo(conn) -> int:
    """清掉所有 is_demo=true 的机器（maintenance_logs ON DELETE CASCADE 自动清）。"""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM ops.machines WHERE is_demo = true")
        return cur.rowcount


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="W2.5: 造 N 台仿真 CNC 设备台账 → ops.machines")
    p.add_argument("--count", type=int, default=30, help="机器数量（默认 30）")
    p.add_argument("--seed", type=int, default=DEFAULT_SEED, help="随机种子（默认 20260830）")
    p.add_argument("--clear", action="store_true", help="先清掉所有 is_demo=true 的机器")
    args = p.parse_args(argv)

    if args.count <= 0:
        print("[ERROR] --count 必须 > 0", file=sys.stderr)
        return 2

    db = get_db_cfg()
    print(f"[INFO] 连库: {db['PG_HOST']}:{db['PG_PORT']}, db={db['PG_DB']}")
    print(f"[INFO] 生成 {args.count} 台机器；seed={args.seed}；clear={args.clear}")

    machines = _build_machines(args.count, args.seed)

    with psycopg.connect(
        host=db["PG_HOST"], port=int(db["PG_PORT"]),
        user=db["PG_SUPERUSER"], password=db["PG_SUPERPASSWORD"],
        dbname=db["PG_DB"],
    ) as conn:
        try:
            if args.clear:
                n = _clear_demo(conn)
                print(f"[INFO] 清掉旧 demo 机器 {n} 台")
            with conn.cursor() as cur:
                for m in machines:
                    row = dict(m)
                    row["spec"] = Jsonb(m["spec"])  # psycopg3 需要 Jsonb wrapper
                    cur.execute(_INSERT_SQL, row)
        except Exception as e:  # noqa: BLE001
            conn.rollback()
            print(f"[ERROR] 入库失败: {e}", file=sys.stderr)
            return 1
        conn.commit()

        # 汇总
        with conn.cursor() as cur:
            cur.execute(
                "SELECT brand, count(*) FROM ops.machines "
                "WHERE is_demo=true GROUP BY brand ORDER BY brand"
            )
            print("[DONE] 各品牌机器数:")
            for brand, n in cur.fetchall():
                print(f"  {brand}: {n}")
            cur.execute("SELECT count(*) FROM ops.machines WHERE is_demo=true")
            (total,) = cur.fetchone()
            print(f"  总计: {total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
