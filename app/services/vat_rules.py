"""染缸状态业务规则。

改缸状态与碱剂滴定建账共用本模块判定，`validate_vat_status_change`
是改状态入口挂的同一判定函数。可染色（ready）须同时通过两套门槛：

1. 氧化还原电位门槛（原有）：最新浸染批次读数存在且 <= -500 mV；
2. 碱剂滴定门槛：同缸滴定序号从 1 起连续且不少于 3 条，相邻碱度差
   绝对值不超过 0.4，最新一笔采集时间晚于该缸最近一笔浸染。
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable, Optional

from app.models import AlkalinityTitration, DipLot, Vat

REDOX_THRESHOLD_MV = Decimal("-500")
MIN_TITRATIONS = 3
MAX_ADJACENT_ALKALINITY_DELTA = Decimal("0.4")


def _as_utc(dt: datetime) -> datetime:
    """表单提交的本地朴素时间按 UTC 处理，避免与带时区的浸染时间比较报错。"""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


class VatRuleError(Exception):
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


def assert_can_add_titration(
    vat: Vat,
    seq: int,
    alkalinity: Decimal,
    existing_seqs: Iterable[int],
) -> None:
    """只有还原中染缸能写滴定本；序号从 1 起、同缸不重复、碱度为正。"""
    if vat.status != Vat.STATUS_REDUCING:
        raise VatRuleError("只有还原中的染缸才能补碱剂滴定本（闲置/可染色缸禁止建账）。")
    if seq < 1:
        raise VatRuleError("滴定序号必须从 1 起。")
    if Decimal(alkalinity) <= 0:
        raise VatRuleError("碱度值必须为正。")
    if seq in set(existing_seqs):
        raise VatRuleError(f"同缸内滴定序号 {seq} 已存在，序号不得重复。")


def assert_can_mark_ready(
    latest: Optional[DipLot],
    titrations: list[AlkalinityTitration],
) -> None:
    """可染色须同时满足电位门槛与碱剂滴定门槛；缺一笔滴定账即不放行。"""
    problems: list[str] = []

    # 门槛一：最新浸染批次 redoxMv 已填且 <= -500 mV
    if (
        latest is None
        or latest.redoxMv is None
        or Decimal(latest.redoxMv) > REDOX_THRESHOLD_MV
    ):
        problems.append("最新浸染批次的氧化还原电位为空或高于 -500 mV")

    # 门槛二：碱剂滴定本
    ordered = sorted(titrations, key=lambda t: t.seq)
    if len(ordered) < MIN_TITRATIONS:
        problems.append(f"碱剂滴定不足 {MIN_TITRATIONS} 条（空账或条数不够均不得放行）")
    else:
        seqs = [t.seq for t in ordered]
        if seqs != list(range(1, len(seqs) + 1)):
            problems.append("碱剂滴定序号未从 1 起连续")
        else:
            bad_pairs = []
            for prev, cur in zip(ordered, ordered[1:]):
                if abs(Decimal(cur.alkalinity) - Decimal(prev.alkalinity)) > MAX_ADJACENT_ALKALINITY_DELTA:
                    bad_pairs.append(f"{prev.seq}→{cur.seq}")
            if bad_pairs:
                problems.append(
                    "相邻碱度差超过 0.4（序号对：" + "、".join(bad_pairs) + "）"
                )
            latest_t = max(ordered, key=lambda t: (_as_utc(t.collectedAt), t.id))
            if latest is None or _as_utc(latest_t.collectedAt) <= _as_utc(latest.dippedAt):
                problems.append("最新碱剂采集时间必须晚于该缸最近一笔浸染")

    if problems:
        raise VatRuleError("无法设为可染色：" + "；".join(problems) + "。")


def validate_vat_status_change(
    vat: Vat,
    new_status: str,
    latest: Optional[DipLot],
    titrations: Optional[list[AlkalinityTitration]] = None,
) -> None:
    """改状态入口的唯一判定函数：转 ready 时电位与碱剂两套门槛并行检查。"""
    if new_status == Vat.STATUS_READY:
        assert_can_mark_ready(latest, titrations or [])
