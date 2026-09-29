import hashlib
import hmac
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models import AlkaliTitration, DipLot, User, Vat, Workshop

_PWD_SALT = os.environ.get("PWD_SALT", "indigovat-dev-salt").encode("utf-8")


def hash_password(password: str) -> str:
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), _PWD_SALT, 120000
    )
    return digest.hex()


def verify_password(plain: str, hashed: str) -> bool:
    return hmac.compare_digest(hash_password(plain), hashed)


def ensure_seed_data(db: Session) -> None:
    """幂等种子：账号 + 蓝靛湾/清水江样例缸位与电位序列。"""
    if not db.query(User).filter_by(username="admin").first():
        db.add(
            User(
                username="admin",
                password_hash=hash_password("123456"),
                is_superuser=True,
            )
        )
    if not db.query(User).filter_by(username="worker").first():
        db.add(
            User(
                username="worker",
                password_hash=hash_password("123456"),
                is_superuser=False,
            )
        )
    db.commit()

    if db.query(Workshop).first():
        return

    w1 = Workshop(name="蓝靛湾一号坊", region="黔东南", notes="晨露还原较快")
    w2 = Workshop(name="清水江二号坊", region="黔南", notes="缸体较深，保温好")
    db.add_all([w1, w2])
    db.flush()

    v1 = Vat(
        workshop_id=w1.id,
        code="V-01",
        dyeType="土靛",
        volumeL=Decimal("800.00"),
        status=Vat.STATUS_REDUCING,
    )
    v2 = Vat(
        workshop_id=w1.id,
        code="V-02",
        dyeType="合成靛",
        volumeL=Decimal("600.00"),
        status=Vat.STATUS_IDLE,
    )
    v3 = Vat(
        workshop_id=w2.id,
        code="V-11",
        dyeType="土靛",
        volumeL=Decimal("900.00"),
        status=Vat.STATUS_REDUCING,
    )
    v4 = Vat(
        workshop_id=w2.id,
        code="V-12",
        dyeType="板蓝根靛",
        volumeL=Decimal("750.00"),
        status=Vat.STATUS_READY,
    )
    db.add_all([v1, v2, v3, v4])
    db.flush()

    now = datetime.now(timezone.utc)

    def lots(vat_id: int, series):
        """series: (hours_ago, meters, redox or None)"""
        rows = []
        for hours, meters, redox in series:
            rows.append(
                DipLot(
                    vat_id=vat_id,
                    dippedAt=now - timedelta(hours=hours),
                    clothMeters=Decimal(meters),
                    redoxMv=Decimal(redox) if redox is not None else None,
                )
            )
        return rows

    db.add_all(
        lots(
            v1.id,
            [
                (36, "18.00", "-410.00"),
                (28, "22.50", "-455.00"),
                (20, "30.00", "-490.00"),
                (12, "40.00", "-510.00"),
                (8, "45.00", "-520.00"),
            ],
        )
    )
    db.add_all(
        lots(
            v2.id,
            [
                (6, "8.00", None),
                (1, "12.00", None),
            ],
        )
    )
    db.add_all(
        lots(
            v3.id,
            [
                (40, "25.00", "-390.00"),
                (30, "35.00", "-430.00"),
                (22, "48.00", "-460.00"),
                (14, "60.00", "-480.00"),
            ],
        )
    )
    db.add_all(
        lots(
            v4.id,
            [
                (48, "20.00", "-420.00"),
                (32, "28.00", "-470.00"),
                (20, "33.00", "-505.00"),
                (10, "38.50", "-530.00"),
            ],
        )
    )

    def titrations(vat_id: int, series):
        """series: (seq, hours_ago, alkalinity, operator)；采集时间须晚于最近浸染。"""
        rows = []
        for seq, hours, alkalinity, operator in series:
            rows.append(
                AlkaliTitration(
                    vat_id=vat_id,
                    seq=seq,
                    alkalinity=Decimal(alkalinity),
                    collectedAt=now - timedelta(hours=hours),
                    operator=operator,
                )
            )
        return rows

    # V-01：只有 2 条滴定——电位虽达标（-520 mV），账不齐仍不能标可染色
    db.add_all(
        titrations(
            v1.id,
            [
                (1, 6, "8.80", "吴阿莲"),
                (2, 3, "9.05", "吴阿莲"),
            ],
        )
    )
    # V-11：3 条连续合格滴定，但电位仅 -480 mV——电位门槛仍拦住可染色
    db.add_all(
        titrations(
            v3.id,
            [
                (1, 9, "9.00", "龙秋生"),
                (2, 6, "9.25", "龙秋生"),
                (3, 3, "9.10", "龙秋生"),
            ],
        )
    )
    # V-12：已是可染色缸，保留一本完整的 3 条滴定账
    db.add_all(
        titrations(
            v4.id,
            [
                (1, 8, "9.10", "潘阿花"),
                (2, 5, "9.30", "潘阿花"),
                (3, 2, "9.20", "潘阿花"),
            ],
        )
    )
    db.commit()
