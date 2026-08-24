"""Mobil ve servis panelleri için ortak DB bağlantısı."""
import calendar
import streamlit as st
import psycopg2
from datetime import datetime, timedelta, date
from typing import Optional
from psycopg2.extras import RealDictCursor

RETENTION_DAYS = 1  # dün ve sonrası görünür; daha eski kayıtlar gizlenir

JOB_INSERT_SQL = """
    INSERT INTO jobs (
        group_id, date, customer_id, job_type, price_worker, price_customer,
        job_tag, is_prepaid, contact_phone, location_url, staff_name, staff_phone
    ) VALUES %s
"""


def job_musteri_telefon(job):
    """Telefon: müşteri profili öncelikli."""
    return (job.get("customer_phone") or job.get("contact_phone") or "").strip()


def job_musteri_konum(job):
    """Konum linki: müşteri profili öncelikli."""
    return (job.get("customer_location") or job.get("location_url") or "").strip()


def render_action_link(label, url=None, new_tab=False):
    """link_button eski Streamlit sürümlerinde key desteklemez; HTML link kullan."""
    if url:
        tgt = ' target="_blank" rel="noopener"' if new_tab else ""
        st.markdown(
            f'<a class="action-link" href="{url}"{tgt}>{label}</a>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            f'<span class="action-link-disabled">{label}</span>',
            unsafe_allow_html=True,
        )


TIP_LABELS = {"pro": "Profesyonel", "student": "Öğrenci"}


def subscription_session_no(group_id) -> Optional[int]:
    """Abonelik oturum sırası: abc123_2 → 3 (group_id son eki + 1)."""
    gid = (group_id or "").strip()
    if not gid:
        return None
    parts = gid.rsplit("_", 1)
    if len(parts) == 2 and parts[1].isdigit():
        return int(parts[1]) + 1
    return None


def subscription_session_key(group_id):
    """Oturum anahtarı: (paket_id, oturum_indeksi)."""
    gid = (group_id or "").strip()
    if not gid:
        return None
    parts = gid.rsplit("_", 1)
    if len(parts) == 2 and parts[1].isdigit():
        return (parts[0], parts[1])
    return (gid, "")


def build_subscription_calendar_meta(rows):
    """Takvim ile aynı kota mantığı: toplam paket + tarih sırasına göre adım."""
    pkg_totals = {}
    pkg_sessions = {}
    for row in rows:
        if isinstance(row, dict):
            gid = (row.get("group_id") or "").strip()
            d = (row.get("date") or "").strip()
        else:
            gid = (row[0] or "").strip()
            d = (row[1] or "").strip() if len(row) > 1 else ""
        if not gid:
            continue
        pid = gid.split("_")[0]
        pkg_totals.setdefault(pid, set()).add(gid)
        if d:
            pkg_sessions.setdefault(pid, {})[gid] = d

    session_steps = {}
    for sessions_dict in pkg_sessions.values():
        sorted_sessions = sorted(
            sessions_dict.items(),
            key=lambda x: (
                datetime.strptime(x[1], "%d.%m.%Y") if x[1] else datetime.min,
                x[0],
            ),
        )
        for step, (gid, _) in enumerate(sorted_sessions, 1):
            session_steps[gid] = step

    return {
        "pkg_totals": {pid: len(sessions) for pid, sessions in pkg_totals.items()},
        "session_steps": session_steps,
    }


def subscription_label(job, meta=None) -> str:
    """Kota etiketi: [3/4] — admin takvimi ile aynı."""
    if job.get("job_tag") != "subscription":
        return ""
    gid = (job.get("group_id") or "").strip()
    if not gid:
        return ""
    meta = meta or {}
    pid = gid.split("_")[0]
    total = meta.get("pkg_totals", {}).get(pid, 0)
    step = meta.get("session_steps", {}).get(gid)
    if step and total:
        return f" [{step}/{total}]"
    return ""


def job_visit_key(job):
    """Aynı ziyarete ait satırları grupla.

    - Abonelik: aynı müşteri + gün + paket → tek kart (2/4, 3/4 birlikte)
    - Tek seferlik: aynı müşteri + gün → tek kart
    """
    date = job.get("date") or ""
    gid = (job.get("group_id") or "").strip()
    cid = job.get("customer_id")
    tag = job.get("job_tag") or "one_time"

    if tag == "subscription":
        pid = gid.split("_")[0] if gid else ""
        if date and cid is not None and pid:
            return (date, "subpkg", str(cid), pid)
        sess = subscription_session_key(gid)
        if date and cid is not None and sess and sess[1]:
            return (date, "sub", str(cid), sess[1])
        if sess and sess[1]:
            return (date, "sub", sess[0], sess[1])
        if gid:
            return (date, "sub", gid)
        if cid is not None:
            return (date, "sub", f"c{cid}")
        return (date, "sub", str(job.get("id")))

    if cid is not None and date:
        return (date, "once", str(cid))
    if gid:
        return (date, "once", gid)
    return (date, "once", str(job.get("id")))


def visit_delete_action(group):
    """Ziyaret grubunun tamamını silmek için SQL."""
    j = visit_group_label(group)
    tag = j.get("job_tag") or "one_time"
    gid = (j.get("group_id") or "").strip()
    date = j.get("date") or ""
    cid = j.get("customer_id")

    if tag == "subscription":
        gids = {(r.get("group_id") or "").strip() for r in group if (r.get("group_id") or "").strip()}
        if len(gids) == 1:
            return ("DELETE FROM jobs WHERE group_id=%s", (next(iter(gids)),))
        if len(gids) > 1:
            return ("DELETE FROM jobs WHERE group_id = ANY(%s)", (list(gids),))
        ids = [r["id"] for r in group if r.get("id")]
        if ids:
            return ("DELETE FROM jobs WHERE id = ANY(%s)", (ids,))
    if tag == "one_time" and date and cid is not None:
        return (
            "DELETE FROM jobs WHERE customer_id=%s AND COALESCE(date, '')=%s AND job_tag=%s",
            (cid, date, "one_time"),
        )
    if gid:
        return (
            "DELETE FROM jobs WHERE group_id=%s AND COALESCE(date, '')=%s",
            (gid, date),
        )
    return None


def group_jobs_by_visit(jobs):
    """İş satırlarını ziyaret gruplarına ayır."""
    groups, order = {}, []
    for j in jobs:
        k = job_visit_key(j)
        if k not in groups:
            groups[k] = []
            order.append(k)
        groups[k].append(j)
    return [groups[k] for k in order]


def summarize_personnel(rows):
    """Gruptaki personeli tip ve sayıya göre özetle."""
    counts = {"pro": 0, "student": 0}
    ucret = {"pro": 0.0, "student": 0.0}
    names = {"pro": [], "student": []}
    phones = {"pro": [], "student": []}
    for r in rows:
        tip = r.get("job_type") or "pro"
        if tip not in counts:
            tip = "pro"
        counts[tip] += 1
        ucret[tip] = float(r.get("price_worker") or 0)
        if r.get("staff_name"):
            names[tip].append(r.get("staff_name"))
        if r.get("staff_phone") is not None:
            phones[tip].append(r.get("staff_phone") or "")
    return counts, ucret, names, phones


def format_personnel_badge(counts, ucret=None):
    parts = []
    for tip in ("pro", "student"):
        n = counts.get(tip, 0)
        if n > 0:
            lbl = TIP_LABELS[tip]
            u = ""
            if ucret is not None and ucret.get(tip):
                u = f" · {ucret[tip]:,.0f} ₺/kişi"
            parts.append(f"**{n}** {lbl}{u}")
    return " · ".join(parts) if parts else "Personel yok"


def format_personnel_html(counts, ucret=None, names=None, phones=None):
    parts = []
    for tip in ("pro", "student"):
        n = counts.get(tip, 0)
        if n > 0:
            lbl = TIP_LABELS[tip]
            u = f" · {ucret[tip]:,.0f} ₺/kişi" if ucret and ucret.get(tip) else ""
            parts.append(f"<b>{n}</b> {lbl}{u}")
    line = " · ".join(parts) if parts else "Personel yok"
    if names:
        det = []
        for tip in ("pro", "student"):
            for nm, ph in zip(names.get(tip, []), phones.get(tip, [])):
                det.append(f"{nm}" + (f" ({ph})" if ph else ""))
        if det:
            line += "<br><span style='opacity:0.85'>" + ", ".join(det) + "</span>"
    return line


def personel_listesi_ozet(personeller):
    counts = {"pro": 0, "student": 0}
    ucret = {"pro": 0.0, "student": 0.0}
    for p in personeller:
        tip = p.get("tip") or "pro"
        counts[tip] = counts.get(tip, 0) + 1
        ucret[tip] = float(p.get("ucret") or 0)
    return format_personnel_badge(counts, ucret)


def expand_personnel_by_type(pro_n, pro_ucret, student_n, student_ucret, existing=None):
    """Tip ve sayıdan personel listesi oluştur."""
    existing = existing or {"pro": [], "student": [], "phones": {"pro": [], "student": []}}
    result = []
    for i in range(int(pro_n)):
        name = existing["pro"][i] if i < len(existing["pro"]) else f"Profesyonel {i + 1}"
        phone = existing["phones"]["pro"][i] if i < len(existing["phones"]["pro"]) else ""
        result.append({"tip": "pro", "ucret": float(pro_ucret), "name": name, "phone": phone})
    for i in range(int(student_n)):
        name = existing["student"][i] if i < len(existing["student"]) else f"Öğrenci {i + 1}"
        phone = existing["phones"]["student"][i] if i < len(existing["phones"]["student"]) else ""
        result.append({"tip": "student", "ucret": float(student_ucret), "name": name, "phone": phone})
    return result


def build_visit_db_rows(group_id, date_str, customer_id, job_tag, price_customer, personeller):
    """Tek ziyaret için DB insert satırları."""
    rows = []
    ilk = True
    for p in personeller:
        cut = float(price_customer) if ilk else 0.0
        ilk = False
        prepaid = 1 if cut > 0 else 0
        rows.append((
            group_id, date_str, customer_id, p["tip"], p["ucret"], cut, job_tag, prepaid,
            None, None, p.get("name"), p.get("phone") or None,
        ))
    return rows


def visit_group_label(group):
    """Grup temsil satırı."""
    return group[0] if group else {}


def subscription_labels_merged(group, meta=None) -> str:
    """Birleşik kart için kota etiketleri: [2/4, 3/4]."""
    meta = meta or {}
    j = visit_group_label(group)
    if j.get("job_tag") != "subscription":
        return ""
    gid_steps = []
    seen = set()
    for row in group:
        gid = (row.get("group_id") or "").strip()
        if not gid or gid in seen:
            continue
        seen.add(gid)
        step = meta.get("session_steps", {}).get(gid)
        if not step:
            continue
        pid = gid.split("_")[0]
        total = meta.get("pkg_totals", {}).get(pid, 0)
        if total:
            gid_steps.append((step, f"{step}/{total}"))
    if not gid_steps:
        return subscription_label(j, meta)
    gid_steps.sort(key=lambda x: x[0])
    unique = []
    for _, label in gid_steps:
        if label not in unique:
            unique.append(label)
    return " [" + ", ".join(unique) + "]"


def visit_has_pro(group) -> bool:
    counts, _, _, _ = summarize_personnel(group)
    return counts.get("pro", 0) > 0


def visit_customer_name(group) -> str:
    return (visit_group_label(group).get("name") or "").casefold()


def sort_visit_groups(groups):
    """Önce profesyonelli işler (A-Z), sonra yalnızca öğrencili işler (A-Z)."""
    return sorted(groups, key=lambda g: (0 if visit_has_pro(g) else 1, visit_customer_name(g)))


def split_group_by_session(group):
    """Abonelik kartını kota (group_id) bazında alt gruplara ayır."""
    j = visit_group_label(group)
    if j.get("job_tag") != "subscription":
        return [(j.get("group_id") or "visit", group)]
    by_gid, order = {}, []
    for r in group:
        gid = (r.get("group_id") or "").strip()
        if not gid:
            continue
        if gid not in by_gid:
            by_gid[gid] = []
            order.append(gid)
        by_gid[gid].append(r)
    if not order:
        return [("visit", group)]
    return [(gid, by_gid[gid]) for gid in order]


def count_subscription_sessions(jobs):
    """Benzersiz abonelik kota (group_id) sayısı."""
    return len({(j.get("group_id") or "").strip() for j in jobs if (j.get("group_id") or "").strip()})


def visit_customer_revenue(group) -> float:
    """Ziyarette müşteriden alınan toplam (gruptaki tüm satırlar)."""
    return sum(float(r.get("price_customer") or 0) for r in group)


def ay_ziyaret_cirosu(jobs_list, sm: int, sy: int) -> float:
    """Ay içindeki ziyaret gruplarından müşteri cirosu (çift sayım önlenir)."""
    arama = f".{sm:02d}.{sy}"
    month_jobs = [j for j in jobs_list if j.get("date") and arama in j["date"]]
    return sum(visit_customer_revenue(g) for g in group_jobs_by_visit(month_jobs))


def visit_worker_cost(group) -> float:
    """Ziyarette personel maliyeti toplamı."""
    return sum(float(r.get("price_worker") or 0) for r in group)


def resolve_row_staff_name(row, pros_by_id=None, students_by_id=None):
    """Satır için görüntülenecek personel adı."""
    pros_by_id = pros_by_id or {}
    students_by_id = students_by_id or {}
    if row.get("assigned_pro_id") is not None:
        p = pros_by_id.get(row["assigned_pro_id"])
        if p:
            return p.get("name") or "Profesyonel", "pro"
    if row.get("assigned_student_id") is not None:
        s = students_by_id.get(row["assigned_student_id"])
        if s:
            return s.get("name") or "Öğrenci", "student"
    if row.get("staff_name"):
        return row["staff_name"], row.get("job_type") or "pro"
    tip = row.get("job_type") or "pro"
    return TIP_LABELS.get(tip, tip), tip


def format_kadro_isimleri(kadro) -> str:
    """Kadrodaki isimleri okunabilir metne çevir."""
    if not kadro:
        return "—"
    parts = []
    for k in kadro:
        ico = "👔" if k.get("tip") == "pro" else "🎓"
        parts.append(f"{ico} {k.get('name', '—')}")
    return ", ".join(parts)


def visit_summary(group, meta=None, pros=None, students=None):
    """Tek ziyaret için özet sözlük."""
    j = visit_group_label(group)
    counts, ucret, names, phones = summarize_personnel(group)
    pros_by_id = {p["id"]: p for p in (pros or [])}
    students_by_id = {s["id"]: s for s in (students or [])}

    kadro = []
    for r in group:
        nm, tip = resolve_row_staff_name(r, pros_by_id, students_by_id)
        kadro.append({
            "name": nm,
            "tip": tip,
            "ucret": float(r.get("price_worker") or 0),
        })

    revenue = visit_customer_revenue(group)
    cost = visit_worker_cost(group)
    tag = j.get("job_tag") or "one_time"
    has_charge = any(float(r.get("price_customer") or 0) > 0 for r in group)
    if has_charge:
        tahsil = all(
            bool(r.get("is_collected"))
            for r in group
            if float(r.get("price_customer") or 0) > 0
        )
    else:
        tahsil = None

    return {
        "date": j.get("date") or "",
        "customer": j.get("name") or "—",
        "customer_id": j.get("customer_id"),
        "job_tag": tag,
        "tag_label": "Abonelik" if tag == "subscription" else "Tek sefer",
        "sub_label": subscription_labels_merged(group, meta) if tag == "subscription" else "",
        "kisi": len(group),
        "counts": counts,
        "kadro_badge": format_personnel_badge(counts, ucret),
        "kadro_isimleri": format_kadro_isimleri(kadro),
        "kadro": kadro,
        "ciro": revenue,
        "maliyet": cost,
        "kar": revenue - cost,
        "tahsil": tahsil,
        "has_pro": visit_has_pro(group),
        "_sort_date": parse_tr_date_str(j.get("date")) or date.min,
    }


def build_visit_summaries(
    jobs_list,
    customer_id=None,
    date_from=None,
    date_to=None,
    job_tag=None,
    meta=None,
    pros=None,
    students=None,
):
    """Filtrelenmiş ziyaret özetleri (yeniden eskiye)."""
    dated = [j for j in jobs_list if (j.get("date") or "").strip()]
    if customer_id is not None:
        dated = [j for j in dated if j.get("customer_id") == customer_id]

    summaries = []
    for group in group_jobs_by_visit(dated):
        s = visit_summary(group, meta, pros, students)
        d = s["_sort_date"]
        if date_from and d < date_from:
            continue
        if date_to and d > date_to:
            continue
        if job_tag and s["job_tag"] != job_tag:
            continue
        summaries.append(s)

    summaries.sort(key=lambda x: (x["_sort_date"], x["customer"]), reverse=True)
    return summaries


def aggregate_visit_summaries(summaries):
    """Özet listesinden toplam metrikler."""
    if not summaries:
        return {"visits": 0, "ciro": 0.0, "maliyet": 0.0, "kar": 0.0, "kisi": 0}
    return {
        "visits": len(summaries),
        "ciro": sum(s["ciro"] for s in summaries),
        "maliyet": sum(s["maliyet"] for s in summaries),
        "kar": sum(s["kar"] for s in summaries),
        "kisi": sum(s["kisi"] for s in summaries),
    }


def customer_ranking_from_summaries(summaries):
    """Müşteri bazında ziyaret/kâr sıralaması."""
    by_cust = {}
    for s in summaries:
        key = s.get("customer_id") if s.get("customer_id") is not None else s["customer"]
        if key not in by_cust:
            by_cust[key] = {
                "customer_id": s.get("customer_id"),
                "name": s["customer"],
                "visits": 0,
                "ciro": 0.0,
                "maliyet": 0.0,
                "kar": 0.0,
                "kisi": 0,
            }
        row = by_cust[key]
        row["visits"] += 1
        row["ciro"] += s["ciro"]
        row["maliyet"] += s["maliyet"]
        row["kar"] += s["kar"]
        row["kisi"] += s["kisi"]
    return sorted(by_cust.values(), key=lambda x: (-x["kar"], -x["visits"], x["name"]))


def sonraki_ay(sm: int, sy: int):
    if sm >= 12:
        return 1, sy + 1
    return sm + 1, sy


def hesapla_abonelik_yukumluluk(jobs_list, sm: int, sy: int):
    """Sonraki ay planlı abonelik yükü + havuzdaki bekleyen kotalar."""
    nsm, nsy = sonraki_ay(sm, sy)
    next_arama = f".{nsm:02d}.{nsy}"

    next_jobs = [
        j for j in jobs_list
        if j.get("job_tag") == "subscription" and j.get("date") and next_arama in j["date"]
    ]
    unscheduled = [
        j for j in jobs_list
        if j.get("job_tag") == "subscription" and not (j.get("date") or "").strip()
    ]

    next_gids = {(j.get("group_id") or "").strip() for j in next_jobs if j.get("group_id")}
    pool_gids = {(j.get("group_id") or "").strip() for j in unscheduled if j.get("group_id")}

    return {
        "sonraki_ay": f"{calendar.month_name[nsm]} {nsy}",
        "sonraki_ay_kota": len(next_gids),
        "sonraki_ay_maliyet": sum(float(j.get("price_worker") or 0) for j in next_jobs),
        "havuz_kota": len(pool_gids),
        "havuz_maliyet": sum(float(j.get("price_worker") or 0) for j in unscheduled),
    }


def min_visible_date() -> date:
    """Kısıtlı panellerde görülebilir en eski gün (dün)."""
    return date.today() - timedelta(days=RETENTION_DAYS)


def parse_tr_date_str(ds) -> Optional[date]:
    if not ds or not str(ds).strip():
        return None
    try:
        return datetime.strptime(str(ds).strip(), "%d.%m.%Y").date()
    except ValueError:
        return None


def is_date_visible(ds, admin: bool = False) -> bool:
    """Tarihli kayıt görünür mü? Tarihsiz işler (bekleyen kota) her zaman görünür."""
    if admin:
        return True
    d = parse_tr_date_str(ds)
    if d is None:
        return True
    return d >= min_visible_date()


def _job_date_filter_sql(admin: bool) -> str:
    if admin:
        return ""
    return """
        AND (
            j.date IS NULL OR TRIM(j.date) = ''
            OR (
                j.date ~ '^[0-9]{2}\\.[0-9]{2}\\.[0-9]{4}$'
                AND TO_DATE(j.date, 'DD.MM.YYYY') >= CURRENT_DATE - INTERVAL '1 day'
            )
        )
    """


def _expense_date_filter_sql(admin: bool) -> str:
    if admin:
        return ""
    return """
        AND e.date ~ '^[0-9]{2}\\.[0-9]{2}\\.[0-9]{4}$'
        AND TO_DATE(e.date, 'DD.MM.YYYY') >= CURRENT_DATE - INTERVAL '1 day'
    """


def get_db_connection():
    s = st.secrets["supabase"]
    denemeler = [(s["host"], s["user"])]
    if s.get("pooler_host") and s.get("pooler_user"):
        denemeler.append((s["pooler_host"], s["pooler_user"]))
    son_hata = None
    for host, user in denemeler:
        try:
            return psycopg2.connect(
                host=host, database=s["dbname"], user=user, password=s["password"],
                port=int(s.get("port", 5432)), cursor_factory=RealDictCursor,
                sslmode="require", connect_timeout=10,
            )
        except Exception as e:
            son_hata = e
    st.error(f"Bağlantı hatası: {son_hata}")
    st.stop()


def ensure_schema(conn):
    with conn.cursor() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS service_personnel (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                phone TEXT DEFAULT ''
            )
        """)
        for col, typ in [
            ("contact_phone", "TEXT"),
            ("location_url", "TEXT"),
            ("staff_name", "TEXT"),
            ("staff_phone", "TEXT"),
        ]:
            c.execute(f"""
                ALTER TABLE jobs ADD COLUMN IF NOT EXISTS {col} {typ}
            """)
        c.execute("""
            ALTER TABLE customers ADD COLUMN IF NOT EXISTS location TEXT DEFAULT ''
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS expenses (
                id SERIAL PRIMARY KEY,
                date TEXT NOT NULL,
                name TEXT DEFAULT '',
                description TEXT DEFAULT '',
                amount NUMERIC DEFAULT 0
            )
        """)
        c.execute("""
            ALTER TABLE expenses ADD COLUMN IF NOT EXISTS name TEXT DEFAULT ''
        """)
    conn.commit()


def load_panel_data(admin: bool = False):
    """admin=True → tam veri (admin.py). admin=False → son 1 gün + tarihsiz işler."""
    conn = get_db_connection()
    try:
        ensure_schema(conn)
        job_filter = _job_date_filter_sql(admin)
        exp_filter = _expense_date_filter_sql(admin)
        with conn.cursor() as c:
            data = {"admin_mode": admin}
            c.execute(f"""
                SELECT j.*, c.name, c.phone AS customer_phone, c.location AS customer_location
                FROM jobs j JOIN customers c ON j.customer_id = c.id
                WHERE 1=1 {job_filter}
            """)
            data["jobs"] = c.fetchall()
            c.execute("SELECT * FROM customers ORDER BY name")
            data["customers"] = c.fetchall()
            c.execute("SELECT * FROM service_personnel ORDER BY name")
            data["service_personnel"] = c.fetchall()
            try:
                c.execute(f"""
                    SELECT * FROM expenses e
                    WHERE 1=1 {exp_filter}
                    ORDER BY date DESC, id DESC
                """)
                data["expenses"] = c.fetchall()
            except Exception:
                data["expenses"] = []
            c.execute("""
                SELECT group_id, date FROM jobs
                WHERE job_tag = 'subscription'
                  AND group_id IS NOT NULL AND TRIM(group_id) <> ''
            """)
            data["subscription_meta"] = build_subscription_calendar_meta(c.fetchall())
            return data
    finally:
        conn.close()


# Geriye dönük uyumluluk (eski import adları)
build_subscription_meta = build_subscription_calendar_meta
