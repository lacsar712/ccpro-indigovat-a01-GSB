"""染缸状态业务规则。

改状态（挂在状态入口的唯一判定函数 validate_vat_status_change）在目标为
``ready`` 时并行执行两套门槛：

1. 氧化还原电位门槛（原有）：最新浸染批次 redoxMv 已填且 <= -500 mV；
2. 碱剂滴定门槛：滴定序号自 1 连续、不少于 3 条、相邻碱度差绝对值
   <= 0.4，且最新一条采集时间晚于该缸最近一笔浸染。
"""

from decimal import Decimal
from typing import Optional, Sequence

from app.models import AlkaliTitration, DipLot, Vat

# 相邻滴定碱度允许的最大波动
MAX_ALKALINITY_STEP = Decimal("0.4")
# 可染色要求的最少滴定条数
MIN_TITRATIONS = 3


class VatRuleError(Exception):
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


def assert_can_log_titration(vat: Vat) -> None:
    """碱剂滴定专页建账：仅还原中染缸可写，闲置缸与可染色缸禁止。"""
    if vat.status != Vat.STATUS_REDUCING:
        raise VatRuleError("只有还原中的染缸才能登记碱剂滴定，闲置缸与可染色缸禁止建账。")


def assert_can_mark_ready(latest: Optional[DipLot]) -> None:
    """电位门槛：不能将染缸标为 ready，除非最新浸染批次 redoxMv 已填且 <= -500。"""
    if latest is None or latest.redoxMv is None or Decimal(latest.redoxMv) > Decimal("-500"):
        raise VatRuleError(
            "无法设为可染色：最新浸染批次的氧化还原电位为空或高于 -500 mV。"
        )


def assert_titrations_ready(
    latest: Optional[DipLot],
    titrations: Sequence[AlkaliTitration],
) -> None:
    """碱剂滴定门槛：条数、序号连续性、碱度波动与采集时序。"""
    rows = sorted(titrations, key=lambda t: t.seq)
    if not rows:
        raise VatRuleError("无法设为可染色：碱剂滴定本为空，账不齐不准标可染色。")
    if len(rows) < MIN_TITRATIONS:
        raise VatRuleError(
            f"无法设为可染色：碱剂滴定仅 {len(rows)} 条，至少需要 {MIN_TITRATIONS} 条。"
        )

    seqs = [t.seq for t in rows]
    expected = list(range(1, len(rows) + 1))
    if seqs != expected:
        raise VatRuleError(
            "无法设为可染色：滴定序号必须从 1 起连续且不重复，当前序号为 "
            + ", ".join(str(s) for s in seqs)
            + "。"
        )

    for prev, cur in zip(rows, rows[1:]):
        if abs(Decimal(cur.alkalinity) - Decimal(prev.alkalinity)) > MAX_ALKALINITY_STEP:
            raise VatRuleError(
                f"无法设为可染色：第 {prev.seq}、{cur.seq} 条碱度差绝对值超过 "
                f"{MAX_ALKALINITY_STEP}，碱度尚未走稳。"
            )

    newest = max(rows, key=lambda t: (t.collectedAt, t.id))
    if latest is None or newest.collectedAt <= latest.dippedAt:
        raise VatRuleError(
            "无法设为可染色：最新滴定采集时间必须晚于该缸最近一笔浸染。"
        )


def validate_vat_status_change(
    vat: Vat,
    new_status: str,
    latest: Optional[DipLot],
    titrations: Optional[Sequence[AlkaliTitration]] = None,
) -> None:
    """改状态入口的唯一判定：转 ready 时电位与碱度两套门槛并行检查。"""
    if new_status == Vat.STATUS_READY:
        assert_can_mark_ready(latest)
        assert_titrations_ready(latest, titrations or [])
