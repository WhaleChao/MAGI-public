from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "laf_branch_profiles.json"
DEFAULT_LAWYER_NAME = os.environ.get("MAGI_LAF_DEFAULT_LAWYER_NAME", "").strip() or "受任律師"
DEFAULT_OFFICE_NAME = os.environ.get("MAGI_LAW_FIRM_OFFICE_NAME", "").strip() or "事務所名稱"
DEFAULT_OFFICE_ADDRESS = os.environ.get("MAGI_LAW_FIRM_ADDRESS", "").strip() or "事務所地址"
DEFAULT_OFFICE_PHONE = os.environ.get("MAGI_LAW_FIRM_PHONE", "").strip() or "事務所電話"
DEFAULT_OFFICE_FAX = os.environ.get("MAGI_LAW_FIRM_FAX", "").strip() or ""
DEFAULT_OFFICE_MOBILE = os.environ.get("MAGI_LAW_FIRM_MOBILE", "").strip() or ""
PUBLIC_PLACEHOLDERS = {
    "受任律師",
    "事務所名稱",
    "事務所地址",
    "事務所電話",
}


@dataclass(frozen=True)
class LawFirmProfile:
    lawyer_name: str = DEFAULT_LAWYER_NAME
    office_name: str = DEFAULT_OFFICE_NAME
    address_line: str = DEFAULT_OFFICE_ADDRESS
    phone: str = DEFAULT_OFFICE_PHONE
    fax: str = DEFAULT_OFFICE_FAX
    mobile: str = DEFAULT_OFFICE_MOBILE


@dataclass(frozen=True)
class LafBranchProfile:
    branch_label: str
    phone: str = ""
    aliases: tuple[str, ...] = ()
    default_lawyer_name: str = DEFAULT_LAWYER_NAME
    poa_footer_template: str = (
        "本事件經本會　{branch_label}審核准予扶助，爰制作本委任狀如上，"
        "自105年10月1日起，本會不再蓋委任狀章。如欲反應律師辦理狀況，"
        "請逕致電分會({phone})。"
    )
    source: str = ""

    def footer_text(self) -> str:
        return self.poa_footer_template.format(
            branch_label=self.branch_label,
            phone=self.phone or "待確認",
        )


def _text(value: Any) -> str:
    return str(value or "").strip()


def _configured_seed_value(value: Any, default: str) -> str:
    text = _text(value)
    if not text or text in PUBLIC_PLACEHOLDERS:
        return default
    return text


def normalize_branch_label(branch: str) -> str:
    value = _text(branch).removeprefix("法扶").strip()
    if not value:
        return ""
    if "原住民" in value or value in {"原民", "原民中心", "原住民族法律服務"}:
        return "原住民族法律服務中心"
    if value.endswith(("分會", "中心")):
        return value
    return f"{value}分會"


def _load_config() -> dict[str, Any]:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"default_lawyer_name": DEFAULT_LAWYER_NAME, "law_firm_profile": {}, "profiles": []}


def _law_firm_profile_from_seed() -> LawFirmProfile:
    data = _load_config().get("law_firm_profile") or {}
    return LawFirmProfile(
        lawyer_name=_configured_seed_value(data.get("lawyer_name"), DEFAULT_LAWYER_NAME),
        office_name=_configured_seed_value(data.get("office_name"), DEFAULT_OFFICE_NAME),
        address_line=_configured_seed_value(data.get("address_line"), DEFAULT_OFFICE_ADDRESS),
        phone=_configured_seed_value(data.get("phone"), DEFAULT_OFFICE_PHONE),
        fax=_configured_seed_value(data.get("fax"), DEFAULT_OFFICE_FAX),
        mobile=_configured_seed_value(data.get("mobile"), DEFAULT_OFFICE_MOBILE),
    )


def fetch_law_firm_profile_from_db(conn: Any | None = None) -> LawFirmProfile | None:
    close_conn = False
    if conn is None:
        if os.environ.get("MAGI_LAF_BRANCH_PROFILE_DB", "1").strip().lower() not in {"1", "true", "yes", "on"}:
            return None
        try:
            import mysql.connector  # type: ignore

            conn = mysql.connector.connect(**_db_config())
            close_conn = True
        except Exception:
            return None
    try:
        cursor = conn.cursor(dictionary=True)
        try:
            cursor.execute(
                """
                SELECT lawyer_name, office_name, address_line, phone, fax, mobile
                FROM laf_law_firm_profiles
                WHERE profile_key = 'default'
                LIMIT 1
                """
            )
            row = cursor.fetchone()
        finally:
            cursor.close()
    except Exception:
        return None
    finally:
        if close_conn:
            try:
                conn.close()
            except Exception:
                pass
    if not row:
        return None
    return LawFirmProfile(
        lawyer_name=_text(row.get("lawyer_name")) or DEFAULT_LAWYER_NAME,
        office_name=_text(row.get("office_name")) or DEFAULT_OFFICE_NAME,
        address_line=_text(row.get("address_line")) or DEFAULT_OFFICE_ADDRESS,
        phone=_text(row.get("phone")) or DEFAULT_OFFICE_PHONE,
        fax=_text(row.get("fax")) or DEFAULT_OFFICE_FAX,
        mobile=_text(row.get("mobile")) or DEFAULT_OFFICE_MOBILE,
    )


def get_law_firm_profile(conn: Any | None = None) -> LawFirmProfile:
    return fetch_law_firm_profile_from_db(conn=conn) or _law_firm_profile_from_seed()


def seed_branch_profiles() -> list[LafBranchProfile]:
    profiles: list[LafBranchProfile] = []
    for item in _load_config().get("profiles") or []:
        branch_label = normalize_branch_label(_text(item.get("branch_label")))
        if not branch_label:
            continue
        profiles.append(
            LafBranchProfile(
                branch_label=branch_label,
                phone=_text(item.get("phone")),
                aliases=tuple(_text(x) for x in (item.get("aliases") or []) if _text(x)),
                default_lawyer_name=_configured_seed_value(item.get("default_lawyer_name"), DEFAULT_LAWYER_NAME),
                poa_footer_template=_text(item.get("poa_footer_template")) or LafBranchProfile(branch_label).poa_footer_template,
                source=_text(item.get("source")),
            )
        )
    return profiles


def _profile_matches(profile: LafBranchProfile, branch: str) -> bool:
    wanted = normalize_branch_label(branch)
    if not wanted:
        return False
    candidates = {profile.branch_label, profile.branch_label.replace("臺", "台"), profile.branch_label.replace("台", "臺")}
    for alias in profile.aliases:
        normalized_alias = normalize_branch_label(alias)
        candidates.add(normalized_alias)
        candidates.add(normalized_alias.replace("臺", "台"))
        candidates.add(normalized_alias.replace("台", "臺"))
    return wanted in candidates or wanted.replace("臺", "台") in candidates or wanted.replace("台", "臺") in candidates


def resolve_seed_branch_profile(branch: str) -> LafBranchProfile | None:
    for profile in seed_branch_profiles():
        if _profile_matches(profile, branch):
            return profile
    label = normalize_branch_label(branch)
    return LafBranchProfile(branch_label=label) if label else None


def ensure_laf_branch_profile_schema(conn: Any) -> None:
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS laf_branch_profiles (
                id INT AUTO_INCREMENT PRIMARY KEY,
                branch_label VARCHAR(100) NOT NULL,
                aliases_json JSON NULL,
                phone VARCHAR(50) DEFAULT '',
                default_lawyer_name VARCHAR(100) NOT NULL DEFAULT '受任律師',
                poa_footer_template TEXT NULL,
                source VARCHAR(100) DEFAULT '',
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                UNIQUE KEY uq_laf_branch_label (branch_label)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS laf_law_firm_profiles (
                id INT AUTO_INCREMENT PRIMARY KEY,
                profile_key VARCHAR(50) NOT NULL,
                lawyer_name VARCHAR(100) NOT NULL DEFAULT '受任律師',
                office_name VARCHAR(100) DEFAULT '',
                address_line VARCHAR(255) DEFAULT '',
                phone VARCHAR(50) DEFAULT '',
                fax VARCHAR(50) DEFAULT '',
                mobile VARCHAR(50) DEFAULT '',
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                UNIQUE KEY uq_laf_law_firm_profile_key (profile_key)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )
    finally:
        cursor.close()


def seed_laf_branch_profiles_to_db(conn: Any) -> None:
    ensure_laf_branch_profile_schema(conn)
    cursor = conn.cursor()
    try:
        firm = _law_firm_profile_from_seed()
        cursor.execute(
            """
            INSERT INTO laf_law_firm_profiles
                (profile_key, lawyer_name, office_name, address_line, phone, fax, mobile)
            VALUES ('default', %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                lawyer_name = VALUES(lawyer_name),
                office_name = VALUES(office_name),
                address_line = VALUES(address_line),
                phone = VALUES(phone),
                fax = VALUES(fax),
                mobile = VALUES(mobile)
            """,
            (
                firm.lawyer_name,
                firm.office_name,
                firm.address_line,
                firm.phone,
                firm.fax,
                firm.mobile,
            ),
        )
        for profile in seed_branch_profiles():
            cursor.execute(
                """
                INSERT INTO laf_branch_profiles
                    (branch_label, aliases_json, phone, default_lawyer_name, poa_footer_template, source)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    aliases_json = VALUES(aliases_json),
                    phone = VALUES(phone),
                    default_lawyer_name = VALUES(default_lawyer_name),
                    poa_footer_template = VALUES(poa_footer_template),
                    source = VALUES(source)
                """,
                (
                    profile.branch_label,
                    json.dumps(list(profile.aliases), ensure_ascii=False),
                    profile.phone,
                    profile.default_lawyer_name,
                    profile.poa_footer_template,
                    profile.source,
                ),
            )
    finally:
        cursor.close()


def _db_config() -> dict[str, Any]:
    return {
        "host": os.environ.get("OSC_DB_HOST") or os.environ.get("DB_HOST", "127.0.0.1"),
        "port": int(os.environ.get("OSC_DB_PORT") or os.environ.get("DB_PORT", "3306")),
        "user": os.environ.get("OSC_DB_USER") or os.environ.get("DB_USER", ""),
        "password": os.environ.get("OSC_DB_PASSWORD") or os.environ.get("DB_PASSWORD", ""),
        "database": os.environ.get("OSC_DB_NAME") or os.environ.get("DB_NAME", "law_firm_data"),
        "connection_timeout": 2,
        "use_pure": True,
        "charset": "utf8mb4",
    }


def fetch_laf_branch_profile_from_db(branch: str, conn: Any | None = None) -> LafBranchProfile | None:
    label = normalize_branch_label(branch)
    if not label:
        return None
    close_conn = False
    if conn is None:
        if os.environ.get("MAGI_LAF_BRANCH_PROFILE_DB", "1").strip().lower() not in {"1", "true", "yes", "on"}:
            return None
        try:
            import mysql.connector  # type: ignore

            conn = mysql.connector.connect(**_db_config())
            close_conn = True
        except Exception:
            return None
    try:
        cursor = conn.cursor(dictionary=True)
        try:
            cursor.execute(
                """
                SELECT branch_label, aliases_json, phone, default_lawyer_name, poa_footer_template, source
                FROM laf_branch_profiles
                WHERE branch_label = %s
                   OR JSON_CONTAINS(COALESCE(aliases_json, JSON_ARRAY()), JSON_QUOTE(%s))
                   OR JSON_CONTAINS(COALESCE(aliases_json, JSON_ARRAY()), JSON_QUOTE(%s))
                LIMIT 1
                """,
                (label, branch, label),
            )
            row = cursor.fetchone()
        finally:
            cursor.close()
    except Exception:
        return None
    finally:
        if close_conn:
            try:
                conn.close()
            except Exception:
                pass
    if not row:
        return None
    aliases_raw = row.get("aliases_json") or "[]"
    try:
        aliases = tuple(str(x) for x in json.loads(aliases_raw))
    except Exception:
        aliases = ()
    return LafBranchProfile(
        branch_label=normalize_branch_label(row.get("branch_label")),
        aliases=aliases,
        phone=_text(row.get("phone")),
        default_lawyer_name=_text(row.get("default_lawyer_name")) or DEFAULT_LAWYER_NAME,
        poa_footer_template=_text(row.get("poa_footer_template")) or LafBranchProfile(label).poa_footer_template,
        source=_text(row.get("source")) or "db",
    )


def resolve_laf_branch_profile(branch: str, conn: Any | None = None) -> LafBranchProfile | None:
    return fetch_laf_branch_profile_from_db(branch, conn=conn) or resolve_seed_branch_profile(branch)
