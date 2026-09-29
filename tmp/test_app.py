import os, tempfile
db_path = tempfile.mktemp(suffix=".db")
os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
os.environ["SESSION_SECRET"] = "test"

from datetime import datetime, timezone
from decimal import Decimal

from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.main import app
from app.models import AlkalinityTitration, Vat
from app.services.vat_rules import VatRuleError, assert_can_add_titration, validate_vat_status_change

with TestClient(app) as client:
    r = client.post("/login", data={"username": "admin", "password": "123456"}, follow_redirects=False)
    assert r.status_code == 303, r.status_code

    # nav entries present on bay
    home = client.get("/").text
    assert 'href="/titrations"' in home and ">还原台<" in home and ">碱剂滴定<" in home, "nav missing"
    # badge on cards: V-01 has 2 seeded titrations
    assert '"titrationCount": 2' in home, "badge count missing"

    # titration dedicated page exists with list + form
    tp = client.get("/titrations").text
    assert "碱剂滴定本" in tp and 'name="alkalinity"' in tp and 'name="operator"' in tp, "page/form missing"
    # per-vat filter works
    f = client.get("/titrations?vat=99999")  # unknown vat => page still renders, no cards
    assert f.status_code == 200

    db = SessionLocal()
    v1 = db.query(Vat).filter_by(code="V-01").one()  # reducing, redox -520, 2 titrations
    v2 = db.query(Vat).filter_by(code="V-02").one()  # idle
    v3 = db.query(Vat).filter_by(code="V-11").one()  # reducing, redox -480, 0 titrations
    v4 = db.query(Vat).filter_by(code="V-12").one()  # ready
    v1id, v2id, v3id, v4id = v1.id, v2.id, v3.id, v4.id
    db.close()

    def post_status(pk, status):
        return client.post(f"/bay/vats/{pk}/status", data={"status": status}, follow_redirects=False)

    # 1) empty-ledger cannot go ready even though... v3 redox also fails
    r = post_status(v3id, "ready")
    assert r.status_code == 400 and "滴定不足 3 条" in r.text and "电位" in r.text, r.status_code

    # 2) 2 titrations (redox OK) still blocked by titration gate
    r = post_status(v1id, "ready")
    assert r.status_code == 400 and "滴定不足 3 条" in r.text, r.text[:200]

    # 3) creating on idle / ready vats forbidden
    for pk in (v2id, v4id):
        r = client.post("/titrations", data={
            "vat_id": pk, "seq": 1, "alkalinity": "8.0",
            "collectedAt": "2026-09-29T03:00", "operator": "x"})
        assert r.status_code == 400 and "只有还原中" in r.text, pk

    # 4) duplicate seq, non-positive alkalinity rejected
    r = client.post("/titrations", data={
        "vat_id": v1id, "seq": 2, "alkalinity": "9.0",
        "collectedAt": "2026-09-29T03:00+00:00", "operator": "x"})
    assert r.status_code == 400 and "序号 2 已存在" in r.text
    r = client.post("/titrations", data={
        "vat_id": v1id, "seq": 3, "alkalinity": "0",
        "collectedAt": "2026-09-29T03:00+00:00", "operator": "x"})
    assert r.status_code == 400 and "碱度值必须为正" in r.text
    r = client.post("/titrations", data={
        "vat_id": v1id, "seq": 0, "alkalinity": "9.0",
        "collectedAt": "2026-09-29T03:00+00:00", "operator": "x"})
    assert r.status_code == 400 and "从 1 起" in r.text

    now = datetime.now(timezone.utc).isoformat()

    # 5) add seq3 to V-01: adjacent delta 8.7->9.2 = 0.5 > 0.4 => gate fails on delta
    r = client.post("/titrations", data={
        "vat_id": v1id, "seq": 3, "alkalinity": "9.200",
        "collectedAt": now, "operator": "陆阿姐"}, follow_redirects=False)
    assert r.status_code == 303, r.text[:300]
    r = post_status(v1id, "ready")
    assert r.status_code == 400 and "相邻碱度差超过 0.4" in r.text, r.text[:300]

    # remove the bad one, add good seq3 (8.9) directly via db to simulate correction path
    db = SessionLocal()
    db.query(AlkalinityTitration).filter_by(vat_id=v1id, seq=3).delete()
    db.commit(); db.close()
    r = client.post("/titrations", data={
        "vat_id": v1id, "seq": 3, "alkalinity": "8.900",
        "collectedAt": now, "operator": "陆阿姐"}, follow_redirects=False)
    assert r.status_code == 303, r.text[:300]
    r = post_status(v1id, "ready")
    assert r.status_code == 303, r.text[:200]  # both gates pass

    # 6) redox gate still enforced independently: V-03 gets 3 good titrations
    for i, alk in enumerate(["7.000", "7.300", "7.500"], start=1):
        r = client.post("/titrations", data={
            "vat_id": v3id, "seq": i, "alkalinity": alk,
            "collectedAt": now, "operator": "y"}, follow_redirects=False)
        assert r.status_code == 303, (i, r.text[:200])
    r = post_status(v3id, "ready")
    assert r.status_code == 400 and "电位" in r.text and "滴定不足" not in r.text, r.text[:300]

    # 7) timing gate: fresh vat with 3 titrations but latest collected BEFORE latest dip
    db = SessionLocal()
    from app.models import DipLot
    # V-02 is idle with recent dips; switch to reducing (allowed), add stale titrations, then ready
    v2 = db.get(Vat, v2id); v2.status = Vat.STATUS_REDUCING
    db.commit(); db.close()
    old = "2000-01-01T00:00+00:00"
    for i, alk in enumerate(["6.000", "6.200", "6.100"], start=1):
        client.post("/titrations", data={
            "vat_id": v2id, "seq": i, "alkalinity": alk,
            "collectedAt": old, "operator": "z"})
    # v2 latest dip has redox None -> redox fails too; give it a good reading
    db = SessionLocal()
    latest_lot = db.query(DipLot).filter_by(vat_id=v2id).order_by(DipLot.dippedAt.desc()).first()
    latest_lot.redoxMv = Decimal("-510")
    db.commit(); db.close()
    r = post_status(v2id, "ready")
    assert r.status_code == 400 and "最新碱剂采集时间必须晚于" in r.text, r.text[:300]

    # 8) ledger appears on filtered page and badge updated
    pg = client.get(f"/titrations?vat={v1id}").text
    assert "8.900" in pg and "陆阿姐" in pg

print("ALL E2E CHECKS PASSED")
