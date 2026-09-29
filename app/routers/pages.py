from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Optional
import json

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from jinja2.utils import markupsafe
from sqlalchemy.orm import Session, joinedload

from app.auth import get_current_user
from app.db import get_db
from app.models import AlkaliTitration, DipLot, Vat, Workshop
from app.services.vat_rules import (
    VatRuleError,
    assert_can_log_titration,
    validate_vat_status_change,
)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _tojson(value):
    return markupsafe.Markup(json.dumps(value, ensure_ascii=False))


templates.env.filters["tojson"] = _tojson

STATUS_LABELS = {
    Vat.STATUS_IDLE: "闲置",
    Vat.STATUS_REDUCING: "还原中",
    Vat.STATUS_READY: "可染色",
}


def render(request: Request, name: str, context: dict, status_code: int = 200):
    ctx = {k: v for k, v in context.items() if k != "request"}
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def _need_login(request: Request, db: Session):
    return get_current_user(request, db)


def _spark_points(lots: list[DipLot], width: int = 72, height: int = 28) -> list[dict]:
    """把 redox 序列压成 sparkline 坐标（无有效读数则空）。"""
    vals = [float(l.redoxMv) for l in lots if l.redoxMv is not None]
    if not vals:
        return []
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    n = len(vals)
    pts = []
    for i, v in enumerate(vals):
        x = 0 if n == 1 else round(i * (width - 1) / (n - 1), 2)
        y = round(height - 1 - ((v - lo) / span) * (height - 1), 2)
        pts.append({"x": x, "y": y})
    return pts


def _vat_payload(vat: Vat) -> dict:
    lots = sorted(vat.lots, key=lambda x: (x.dippedAt, x.id))
    chronological = lots
    latest = lots[-1] if lots else None
    recent = list(reversed(lots[-8:]))  # 展开区展示近几笔
    return {
        "id": vat.id,
        "code": vat.code,
        "dyeType": vat.dyeType,
        "volumeL": float(vat.volumeL),
        "status": vat.status,
        "statusLabel": STATUS_LABELS.get(vat.status, vat.status),
        "workshopId": vat.workshop_id,
        "workshopName": vat.workshop.name if vat.workshop else "",
        "lastRedox": float(latest.redoxMv) if latest and latest.redoxMv is not None else None,
        "lastMeters": float(latest.clothMeters) if latest else None,
        "lastDippedAt": latest.dippedAt.strftime("%Y-%m-%d %H:%M") if latest else None,
        "titrationCount": len(vat.titrations),
        "spark": _spark_points(chronological),
        "recentLots": [
            {
                "id": l.id,
                "dippedAt": l.dippedAt.strftime("%Y-%m-%d %H:%M"),
                "clothMeters": float(l.clothMeters),
                "redoxMv": float(l.redoxMv) if l.redoxMv is not None else None,
            }
            for l in recent
        ],
    }


def _bay_context(
    request: Request,
    db: Session,
    user,
    workshop_id: Optional[int] = None,
    selected_vat: Optional[int] = None,
    error: Optional[str] = None,
):
    # 始终下发全部缸位；工坊仅作前端 chip 筛选，避免切回「全部」时缺数据
    workshops = db.query(Workshop).order_by(Workshop.name).all()
    vats = (
        db.query(Vat)
        .options(
            joinedload(Vat.workshop),
            joinedload(Vat.lots),
            joinedload(Vat.titrations),
        )
        .order_by(Vat.code)
        .all()
    )
    return {
        "request": request,
        "user": user,
        "workshops": [{"id": w.id, "name": w.name, "region": w.region} for w in workshops],
        "vats": [_vat_payload(v) for v in vats],
        "filter_workshop": workshop_id,
        "selected_vat": selected_vat,
        "error": error,
        "status_labels": STATUS_LABELS,
        "active": "bay",
    }


@router.get("/", response_class=HTMLResponse)
async def bay(
    request: Request,
    workshop: Optional[int] = None,
    vat: Optional[int] = None,
    db: Session = Depends(get_db),
):
    user = _need_login(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    return render(request, "bay.html", _bay_context(request, db, user, workshop, vat))


@router.post("/bay/vats/{pk}/status", response_class=HTMLResponse)
async def bay_vat_status(
    pk: int,
    request: Request,
    status: str = Form(...),
    workshop: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need_login(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    item = (
        db.query(Vat)
        .options(
            joinedload(Vat.workshop),
            joinedload(Vat.lots),
            joinedload(Vat.titrations),
        )
        .filter(Vat.id == pk)
        .first()
    )
    ws = int(workshop) if workshop.strip() else None
    if not item:
        return RedirectResponse("/", status_code=303)
    error = None
    try:
        latest = item.latest_lot()
        validate_vat_status_change(item, status, latest, item.titrations)
        item.status = status
        db.commit()
        return RedirectResponse(f"/?vat={pk}" + (f"&workshop={ws}" if ws else ""), status_code=303)
    except VatRuleError as exc:
        error = exc.message
        db.rollback()
    return render(
        request,
        "bay.html",
        _bay_context(request, db, user, ws, pk, error),
        status_code=400,
    )


@router.post("/bay/vats/{pk}/lots", response_class=HTMLResponse)
async def bay_log_lot(
    pk: int,
    request: Request,
    dippedAt: str = Form(...),
    clothMeters: str = Form(...),
    redoxMv: str = Form(""),
    workshop: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need_login(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    item = db.get(Vat, pk)
    ws = int(workshop) if workshop.strip() else None
    if not item:
        return RedirectResponse("/", status_code=303)
    error = None
    try:
        lot = DipLot(
            vat_id=pk,
            dippedAt=datetime.fromisoformat(dippedAt),
            clothMeters=Decimal(clothMeters),
            redoxMv=Decimal(redoxMv) if redoxMv.strip() else None,
        )
        db.add(lot)
        db.commit()
        return RedirectResponse(f"/?vat={pk}" + (f"&workshop={ws}" if ws else ""), status_code=303)
    except (ValueError, InvalidOperation) as exc:
        error = f"浸染记录无效：{exc}"
        db.rollback()
    return render(
        request,
        "bay.html",
        _bay_context(request, db, user, ws, pk, error),
        status_code=400,
    )


# 旧顶栏 CRUD 路径一律回到还原台，避免「换皮表页」残留入口
@router.get("/workshops")
@router.get("/vats")
@router.get("/lots")
@router.get("/home")
async def legacy_redirect():
    return RedirectResponse("/", status_code=303)


# ---------------------------------------------------------------------------
# 碱剂滴定专页
# ---------------------------------------------------------------------------


def _vat_option(vat: Vat) -> dict:
    return {
        "id": vat.id,
        "code": vat.code,
        "workshopName": vat.workshop.name if vat.workshop else "",
        "status": vat.status,
        "statusLabel": STATUS_LABELS.get(vat.status, vat.status),
        "titrationCount": len(vat.titrations),
    }


def _titration_row(t: AlkaliTitration) -> dict:
    return {
        "id": t.id,
        "vatId": t.vat_id,
        "vatCode": t.vat.code if t.vat else "",
        "seq": t.seq,
        "alkalinity": float(t.alkalinity),
        "collectedAt": t.collectedAt.strftime("%Y-%m-%d %H:%M"),
        "operator": t.operator,
    }


def _titrations_context(
    request: Request,
    db: Session,
    user,
    filter_vat: Optional[int] = None,
    error: Optional[str] = None,
    form: Optional[dict] = None,
):
    vats = (
        db.query(Vat)
        .options(joinedload(Vat.workshop), joinedload(Vat.titrations))
        .order_by(Vat.code)
        .all()
    )
    query = (
        db.query(AlkaliTitration)
        .options(joinedload(AlkaliTitration.vat))
        .order_by(AlkaliTitration.vat_id, AlkaliTitration.seq.desc())
    )
    selected = None
    if filter_vat is not None:
        query = query.filter(AlkaliTitration.vat_id == filter_vat)
        selected = next((v for v in vats if v.id == filter_vat), None)
    entries = [_titration_row(t) for t in query.all()]
    return {
        "request": request,
        "user": user,
        "vats": [_vat_option(v) for v in vats],
        "entries": entries,
        "filter_vat": filter_vat,
        "selected_vat": _vat_option(selected) if selected else None,
        "can_write": selected.status == Vat.STATUS_REDUCING if selected else None,
        "error": error,
        "form": form or {},
        "active": "titrations",
    }


@router.get("/titrations", response_class=HTMLResponse)
async def titrations_page(
    request: Request,
    vat: Optional[int] = None,
    db: Session = Depends(get_db),
):
    user = _need_login(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    return render(request, "titrations.html", _titrations_context(request, db, user, vat))


@router.post("/titrations", response_class=HTMLResponse)
async def titrations_create(
    request: Request,
    vat_id: str = Form(...),
    seq: str = Form(...),
    alkalinity: str = Form(...),
    collectedAt: str = Form(...),
    operator: str = Form(...),
    db: Session = Depends(get_db),
):
    user = _need_login(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    form = {
        "vat_id": vat_id,
        "seq": seq,
        "alkalinity": alkalinity,
        "collectedAt": collectedAt,
        "operator": operator,
    }
    item = db.get(Vat, int(vat_id) if vat_id.strip().isdigit() else -1)
    if not item:
        return render(
            request,
            "titrations.html",
            _titrations_context(request, db, user, None, "请选择有效染缸。", form),
            status_code=400,
        )
    try:
        # 建账权限：仅还原中可写（闲置 / 可染色禁止）
        assert_can_log_titration(item)

        try:
            seq_val = int(seq)
        except (TypeError, ValueError):
            raise VatRuleError("滴定序号必须为整数，且从 1 起。")
        if seq_val < 1:
            raise VatRuleError("滴定序号必须从 1 起，不得为 0 或负数。")

        try:
            alk = Decimal(alkalinity)
        except InvalidOperation:
            raise VatRuleError("碱度值必须为正数。")
        if alk <= 0:
            raise VatRuleError("碱度值必须为正数。")

        operator_val = operator.strip()
        if not operator_val:
            raise VatRuleError("当班人不能为空。")
        try:
            collected = datetime.fromisoformat(collectedAt)
        except ValueError:
            raise VatRuleError("采集时间格式无效。")

        exists = (
            db.query(AlkaliTitration)
            .filter(AlkaliTitration.vat_id == item.id, AlkaliTitration.seq == seq_val)
            .first()
        )
        if exists:
            raise VatRuleError(f"该缸已存在第 {seq_val} 条滴定，同缸序号不得重复。")

        db.add(
            AlkaliTitration(
                vat_id=item.id,
                seq=seq_val,
                alkalinity=alk,
                collectedAt=collected,
                operator=operator_val,
            )
        )
        db.commit()
        return RedirectResponse(f"/titrations?vat={item.id}", status_code=303)
    except VatRuleError as exc:
        db.rollback()
        return render(
            request,
            "titrations.html",
            _titrations_context(request, db, user, item.id, exc.message, form),
            status_code=400,
        )
