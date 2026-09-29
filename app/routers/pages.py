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
from app.models import AlkalinityTitration, DipLot, Vat, Workshop
from app.services.vat_rules import (
    VatRuleError,
    assert_can_add_titration,
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


def _titrations_context(
    request: Request,
    db: Session,
    user,
    vat_id: Optional[int] = None,
    form: Optional[dict] = None,
    error: Optional[str] = None,
):
    vats = (
        db.query(Vat)
        .options(
            joinedload(Vat.workshop),
            joinedload(Vat.titrations),
            joinedload(Vat.lots),
        )
        .order_by(Vat.code)
        .all()
    )
    vat_cards = []
    for v in vats:
        titrations = sorted(v.titrations, key=lambda t: (t.seq, t.id))
        latest_lot = v.latest_lot()
        vat_cards.append(
            {
                "id": v.id,
                "code": v.code,
                "dyeType": v.dyeType,
                "workshopName": v.workshop.name if v.workshop else "",
                "status": v.status,
                "statusLabel": STATUS_LABELS.get(v.status, v.status),
                "canWrite": v.status == Vat.STATUS_REDUCING,
                "nextSeq": (max((t.seq for t in titrations), default=0) + 1),
                "lastDippedAt": latest_lot.dippedAt.strftime("%Y-%m-%d %H:%M")
                if latest_lot
                else None,
                "titrations": [
                    {
                        "id": t.id,
                        "seq": t.seq,
                        "alkalinity": f"{Decimal(t.alkalinity):.3f}",
                        "collectedAt": t.collectedAt.strftime("%Y-%m-%d %H:%M"),
                        "operator": t.operator,
                    }
                    for t in titrations
                ],
            }
        )
    reducing_vats = [
        {"id": c["id"], "code": c["code"], "label": f'{c["code"]}（{c["workshopName"]}）'}
        for c in vat_cards
        if c["canWrite"]
    ]
    return {
        "request": request,
        "user": user,
        "vat_cards": vat_cards,
        "reducing_vats": reducing_vats,
        "filter_vat": vat_id,
        "form": form or {},
        "error": error,
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
async def create_titration(
    request: Request,
    vat_id: str = Form(...),
    seq: str = Form(...),
    alkalinity: str = Form(...),
    collectedAt: str = Form(...),
    operator: str = Form(...),
    filter_vat: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need_login(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    fv = int(filter_vat) if filter_vat.strip() else None
    form = {
        "vat_id": vat_id,
        "seq": seq,
        "alkalinity": alkalinity,
        "collectedAt": collectedAt,
        "operator": operator,
    }
    item = None
    try:
        item = (
            db.query(Vat)
            .options(joinedload(Vat.titrations))
            .filter(Vat.id == int(vat_id))
            .first()
        )
    except ValueError:
        pass
    if not item:
        return render(
            request,
            "titrations.html",
            _titrations_context(request, db, user, fv, form, "染缸不存在。"),
            status_code=400,
        )
    try:
        seq_val = int(seq)
        alk_val = Decimal(alkalinity)
        collected = datetime.fromisoformat(collectedAt)
        operator_val = operator.strip()
        if not operator_val:
            raise ValueError("当班人不能为空")
        assert_can_add_titration(
            item,
            seq_val,
            alk_val,
            [t.seq for t in item.titrations],
        )
        db.add(
            AlkalinityTitration(
                vat_id=item.id,
                seq=seq_val,
                alkalinity=alk_val,
                collectedAt=collected,
                operator=operator_val,
            )
        )
        db.commit()
        return RedirectResponse(
            f"/titrations?vat={item.id}", status_code=303
        )
    except (ValueError, InvalidOperation) as exc:
        error = f"滴定记录无效：{exc}"
        db.rollback()
    except VatRuleError as exc:
        error = exc.message
        db.rollback()
    return render(
        request,
        "titrations.html",
        _titrations_context(request, db, user, fv, form, error),
        status_code=400,
    )


# 旧顶栏 CRUD 路径一律回到还原台，避免「换皮表页」残留入口
@router.get("/workshops")
@router.get("/vats")
@router.get("/lots")
@router.get("/home")
async def legacy_redirect():
    return RedirectResponse("/", status_code=303)
