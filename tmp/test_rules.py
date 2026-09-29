"""离线验证 app.services.vat_rules 的判定逻辑（环境无 sqlalchemy，用桩导入真实模块）。"""
import sys, types
from datetime import datetime, timedelta, timezone
from decimal import Decimal

# --- 桩 sqlalchemy ---
sa = types.ModuleType("sqlalchemy")
orm = types.ModuleType("sqlalchemy.orm")

def _make(name):
    def f(*a, **k):
        return None
    f.__name__ = name
    return f

for n in ["Boolean", "DateTime", "ForeignKey", "Numeric", "String", "Text", "UniqueConstraint"]:
    setattr(sa, n, _make(n))

class _Mapped:
    def __class_getitem__(cls, item):
        return None
orm.Mapped = _Mapped
orm.mapped_column = lambda *a, **k: None
orm.relationship = lambda *a, **k: None
sys.modules["sqlalchemy"] = sa
sys.modules["sqlalchemy.orm"] = orm

dbmod = types.ModuleType("app.db")
class Base: ...
dbmod.Base = Base
sys.modules["app.db"] = dbmod
app_pkg = types.ModuleType("app"); app_pkg.__path__ = ["/workspace/app"]
sys.modules["app"] = app_pkg

sys.path.insert(0, "/workspace")
from app.services.vat_rules import (  # noqa: E402
    VatRuleError,
    assert_can_log_titration,
    validate_vat_status_change,
)
from app.models import Vat, DipLot, AlkaliTitration  # noqa: E402

now = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)

def lot(at, mv):
    l = DipLot.__new__(DipLot)
    l.dippedAt = at; l.redoxMv = None if mv is None else Decimal(mv); return l

def tit(seq, alk, at):
    t = AlkaliTitration.__new__(AlkaliTitration)
    t.id = seq; t.seq = seq; t.alkalinity = Decimal(alk); t.collectedAt = at; return t

def vat(status):
    v = Vat.__new__(Vat); v.status = status; return v

GOOD_LOT = lot(now - timedelta(hours=4), "-520")
BAD_LOT = lot(now - timedelta(hours=4), "-480")
NONE_LOT = lot(now - timedelta(hours=4), None)

def good_tits(adjust=None, n=3):
    rows = [
        tit(1, "9.00", now - timedelta(hours=9)),
        tit(2, "9.30", now - timedelta(hours=6)),
        tit(3, "9.10", now - timedelta(hours=3)),
    ]
    if adjust: adjust(rows)
    return rows[:n]

results = []
def check(name, fn, expect_error):
    try:
        fn()
        got = None
    except VatRuleError as e:
        got = e.message
    ok = (got is not None) == expect_error
    results.append((ok, name, got))

# 1 空账 + 电位合格 -> 拒
check("空账即使电位合格也不能 ready",
      lambda: validate_vat_status_change(vat("reducing"), "ready", GOOD_LOT, []), True)
# 2 仅 2 条 -> 拒
check("只有2条滴定不能 ready",
      lambda: validate_vat_status_change(vat("reducing"), "ready", GOOD_LOT, good_tits(n=2)), True)
# 3 合格 -> 放行
check("电位+3条连续滴定齐全 -> ready",
      lambda: validate_vat_status_change(vat("reducing"), "ready", GOOD_LOT, good_tits()), False)
# 4 电位 -480 -> 拒（电位门槛仍生效）
check("电位-480即使滴定齐全也拒",
      lambda: validate_vat_status_change(vat("reducing"), "ready", BAD_LOT, good_tits()), True)
# 5 电位空 -> 拒
check("电位为空拒",
      lambda: validate_vat_status_change(vat("reducing"), "ready", NONE_LOT, good_tits()), True)
# 6 无浸染批次 -> 拒（两套检查都拦）
check("无浸染批次拒",
      lambda: validate_vat_status_change(vat("reducing"), "ready", None, good_tits()), True)
# 7 序号断号 1,2,4 -> 拒
def gap(rows): rows[2].seq = 4
check("序号断号拒",
      lambda: validate_vat_status_change(vat("reducing"), "ready", GOOD_LOT, good_tits(gap)), True)
# 8 相邻差 0.5 -> 拒
def bigstep(rows): rows[1].alkalinity = Decimal("9.50")
check("相邻碱度差0.5拒",
      lambda: validate_vat_status_change(vat("reducing"), "ready", GOOD_LOT, good_tits(bigstep)), True)
# 9 相邻差恰好 0.4 -> 放行
def edge(rows): rows[1].alkalinity = Decimal("9.40")
check("相邻碱度差恰好0.4放行",
      lambda: validate_vat_status_change(vat("reducing"), "ready", GOOD_LOT, good_tits(edge)), False)
# 10 最新滴定早于最近浸染 -> 拒
def early(rows): rows[2].collectedAt = now - timedelta(hours=5)
check("最新采集早于浸染拒",
      lambda: validate_vat_status_change(vat("reducing"), "ready", GOOD_LOT, good_tits(early)), True)
# 11 乱序传入也按 seq 校验
def unsorted(rows): rows[0], rows[2] = rows[2], rows[0]
check("乱序传入仍按序号判定放行",
      lambda: validate_vat_status_change(vat("reducing"), "ready", GOOD_LOT, good_tits(unsorted)), False)
# 12 建账权限
check("还原中可建账", lambda: assert_can_log_titration(vat("reducing")), False)
check("闲置缸禁止建账", lambda: assert_can_log_titration(vat("idle")), True)
check("可染色缸禁止建账", lambda: assert_can_log_titration(vat("ready")), True)
# 13 改到 idle/reducing 不触发门槛
check("改回闲置不检查门槛",
      lambda: validate_vat_status_change(vat("reducing"), "idle", None, []), False)

fails = 0
for ok, name, msg in results:
    print(("PASS" if ok else "FAIL"), "-", name, ("| 拒因: " + msg if not ok and msg else ""))
    if not ok: fails += 1
print(f"\n{len(results)-fails}/{len(results)} passed")
sys.exit(1 if fails else 0)
