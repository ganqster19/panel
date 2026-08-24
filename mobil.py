"""
Mobil vardiya paneli — iş ekle/sil, takvim, müşteri, personel, konum & telefon.

Çalıştırma: streamlit run mobil.py
"""
import streamlit as st
import uuid
import calendar
from datetime import datetime, timedelta, date
from dataclasses import dataclass, field
from psycopg2.extras import execute_values

from vardiya.db import (
    get_db_connection, load_panel_data, JOB_INSERT_SQL,
    job_musteri_telefon, job_musteri_konum, render_action_link, min_visible_date,
    group_jobs_by_visit, summarize_personnel, format_personnel_badge, format_personnel_html,
    personel_listesi_ozet, expand_personnel_by_type, build_visit_db_rows, visit_group_label,
    visit_delete_action, subscription_label, subscription_labels_merged,
    sort_visit_groups, visit_has_pro, split_group_by_session,
)
from vardiya.auth import require_auth

st.set_page_config(
    page_title="Vardiya Mobil",
    page_icon="📱",
    layout="centered",
    initial_sidebar_state="collapsed",
)
require_auth("mobil")


@dataclass
class Is:
    musteri_id: int
    musteri_adi: str
    job_tag: str
    tarihler: list
    musteri_tutari: float
    fiyat_modu: str
    personeller: list = field(default_factory=list)
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])

    @property
    def ziyaret_sayisi(self) -> int:
        return len(self.tarihler)

    def db_satirlarina_donustur(self):
        pkg_id = str(uuid.uuid4())[:8]
        satirlar = []
        odendi_mi = False
        for i, d in enumerate(self.tarihler):
            ds = d.strftime("%d.%m.%Y") if d else ""
            gid = f"{pkg_id}_{i}" if self.job_tag == "subscription" else pkg_id
            if self.job_tag == "subscription":
                bu_ziyaret_tutari = self.musteri_tutari if i == 0 else 0.0
            else:
                bu_ziyaret_tutari = self.musteri_tutari if (self.fiyat_modu == "Günlük" or not odendi_mi) else 0.0
                if self.fiyat_modu == "Toplam":
                    odendi_mi = True
            ilk = False
            for p in self.personeller:
                cut = bu_ziyaret_tutari if not ilk else 0.0
                ilk = True
                prepaid = 1 if cut > 0 else 0
                satirlar.append((
                    gid, ds, self.musteri_id, p["tip"], p["ucret"], cut, self.job_tag, prepaid,
                    None, None,
                    p.get("name") or None, p.get("phone") or None,
                ))
        return satirlar


st.markdown("""
<style>
    .block-container { padding-top: 1rem; padding-bottom: 5rem; max-width: 720px; }

    /* Sabit açık renkli kutular — metin rengi her zaman koyu */
    .job-subs, .job-once {
        padding: 8px 10px; border-radius: 8px; font-size: 14px; display: block;
        margin-bottom: 6px; font-weight: 600; line-height: 1.3;
    }
    .job-subs {
        background-color: #fff3e0; border: 1px solid #ffcc80;
        color: #e65100 !important;
    }
    .job-subs * { color: #e65100 !important; }
    .job-once {
        background-color: #e3f2fd; border: 1px solid #90caf9;
        color: #1565c0 !important;
    }
    .job-once * { color: #1565c0 !important; }
    .queue-box {
        background-color: #ffebee; border: 2px solid #ef5350;
        color: #c62828 !important;
        padding: 12px; border-radius: 10px; font-weight: bold; text-align: center;
    }
    .queue-box * { color: #c62828 !important; }
    .quota-box {
        background-color: #fff8e1; border: 1px dashed #ffb300;
        padding: 12px; border-radius: 10px; margin-bottom: 8px;
        color: #4e342e !important;
    }
    .quota-box b, .quota-box * { color: #4e342e !important; }
    .gider-card {
        background: var(--secondary-background-color, #f8f9fa);
        border: 1px solid rgba(128, 128, 128, 0.25);
        border-radius: 10px; padding: 10px 12px; margin-bottom: 8px;
        color: var(--text-color, #1a1a1a) !important;
    }
    .gider-card b { color: var(--text-color, #1a1a1a) !important; }

    /* Tema uyumlu kartlar */
    .info-card {
        background: var(--secondary-background-color, #f8f9fa);
        border: 1px solid rgba(128, 128, 128, 0.25);
        border-radius: 10px; padding: 10px 12px;
        margin: 6px 0; font-size: 14px; line-height: 1.5;
        color: var(--text-color, #1a1a1a) !important;
    }
    .info-card b {
        color: var(--text-color, #1a1a1a) !important;
    }
    .info-card a {
        color: #1e88e5 !important;
        font-weight: 600; text-decoration: none;
    }
    .info-card a:hover { text-decoration: underline; }

    .day-header {
        background: #1565c0; color: #fff !important; padding: 12px 16px;
        border-radius: 10px; text-align: center; font-weight: 700; margin-bottom: 14px;
    }
    .day-header * { color: #fff !important; }
    .servis-card {
        background: var(--secondary-background-color, #f8f9fa);
        border: 1px solid rgba(128, 128, 128, 0.25);
        border-radius: 12px;
        padding: 14px 16px; margin-bottom: 6px;
        box-shadow: 0 1px 3px rgba(0,0,0,.06);
        color: var(--text-color, #1a1a1a);
    }
    .servis-card h3 {
        margin: 0 0 8px 0; font-size: 1.1rem;
        color: var(--text-color, #1a1a1a) !important;
    }
    .servis-meta { font-size: 15px; line-height: 1.6; color: var(--text-color, #333); }
    .servis-meta b { color: var(--text-color, #1a1a1a) !important; }
    .servis-meta a { color: #1e88e5 !important; font-weight: 600; text-decoration: none; }
    .servis-meta a:hover { text-decoration: underline; }

    .action-link {
        display: flex; align-items: center; justify-content: center;
        min-height: 44px; padding: 10px 12px; border-radius: 10px;
        border: 1px solid rgba(128, 128, 128, 0.35);
        background: var(--secondary-background-color, #f0f2f6);
        color: var(--text-color, #31333f) !important;
        font-weight: 600; text-decoration: none; font-size: 15px;
    }
    .action-link:hover { border-color: #1e88e5; color: #1e88e5 !important; }
    .action-link-disabled {
        display: flex; align-items: center; justify-content: center;
        min-height: 44px; padding: 10px 12px; border-radius: 10px;
        border: 1px solid rgba(128, 128, 128, 0.2);
        background: var(--secondary-background-color, #f0f2f6);
        color: var(--text-color, #888) !important;
        font-weight: 600; font-size: 15px; opacity: 0.55;
    }

    .mobil-bar {
        position: sticky; top: 0; z-index: 999;
        background: var(--background-color, #ffffff);
        padding: 10px 0 12px 0;
        border-bottom: 1px solid rgba(128, 128, 128, 0.25);
        margin-bottom: 12px;
    }

    div.stButton > button { min-height: 48px; font-size: 16px; border-radius: 10px; }
    div[data-testid="stTabs"] button { min-height: 44px; font-size: 14px; }
    .nav-day-btn div.stButton > button {
        min-height: 56px; font-size: 17px; font-weight: 700;
    }
    .list-tier-sep {
        display: flex; align-items: center; margin: 18px 0 14px 0;
        color: var(--text-color, #666); font-size: 13px; font-weight: 700;
    }
    .list-tier-sep::before, .list-tier-sep::after {
        content: ""; flex: 1; border-bottom: 2px dashed rgba(128, 128, 128, 0.4);
    }
    .list-tier-sep span { padding: 0 14px; white-space: nowrap; }
    @media (max-width: 640px) {
        .block-container { padding-left: 0.75rem; padding-right: 0.75rem; }
        h1 { font-size: 1.35rem !important; }
        div.stButton > button { min-height: 52px; }
    }
</style>
""", unsafe_allow_html=True)

if "draft_jobs" not in st.session_state:
    st.session_state.draft_jobs = []
if "pending_actions" not in st.session_state:
    st.session_state.pending_actions = []
if "sel_date" not in st.session_state:
    st.session_state.sel_date = datetime.now().strftime("%d.%m.%Y")
if "db_data" not in st.session_state:
    st.session_state.db_data = {}


def format_tr_date(d):
    return d.strftime("%d.%m.%Y")


def parse_tr_date(s):
    try:
        return datetime.strptime(s, "%d.%m.%Y").date()
    except (ValueError, TypeError):
        return date.today()


def refresh_data():
    st.session_state.db_data = load_panel_data(admin=False)


if not st.session_state.db_data:
    refresh_data()
elif "subscription_meta" not in st.session_state.db_data:
    refresh_data()


def add_to_queue(desc, query, params, is_bulk=False):
    st.session_state.pending_actions.append({
        "desc": desc, "query": query, "params": params, "is_bulk": is_bulk,
    })
    st.toast(f"✅ {desc}")


def commit_queue():
    if not st.session_state.pending_actions:
        return
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            for action in st.session_state.pending_actions:
                if action["is_bulk"]:
                    execute_values(c, action["query"], action["params"])
                else:
                    c.execute(action["query"], action["params"])
            conn.commit()
        st.session_state.pending_actions = []
        refresh_data()
        st.success("Kaydedildi!")
        st.rerun()
    except Exception as e:
        conn.rollback()
        st.error(f"Kayıt hatası: {e}")
    finally:
        conn.close()



def tel_link(num):
    if not num or not str(num).strip():
        return None
    digits = "".join(c for c in str(num) if c.isdigit())
    if digits.startswith("0"):
        digits = "90" + digits[1:]
    elif not digits.startswith("90"):
        digits = "90" + digits
    return f"tel:+{digits}"


def maps_link(url):
    u = (url or "").strip()
    if not u:
        return None
    if not u.startswith("http"):
        u = "https://" + u
    return u


def render_tier_separator():
    st.markdown(
        '<div class="list-tier-sep"><span>🎓 Öğrenci personelli işler</span></div>',
        unsafe_allow_html=True,
    )


def render_visit_card(group, sub_meta=None):
    j = visit_group_label(group)
    musteri = j.get("name") or "Müşteri"
    sub_label = subscription_labels_merged(group, sub_meta) if sub_meta is not None else ""
    counts, ucret, names, phones = summarize_personnel(group)
    personel_line = format_personnel_html(counts, ucret, names, phones)
    contact = job_musteri_telefon(j)
    loc = maps_link(job_musteri_konum(j))
    tag = "🔄" if j.get("job_tag") == "subscription" else "🔹"
    meta_lines = [
        f"👷 {personel_line}",
        f"📞 Müşteri: <b>{contact or '—'}</b>",
    ]
    if loc:
        meta_lines.append(f'📍 <a href="{loc}" target="_blank">Konuma git</a>')
    st.markdown(
        f'<div class="servis-card">'
        f'<h3>{tag} {musteri}{sub_label}</h3>'
        f'<div class="servis-meta">' + "<br>".join(meta_lines) + "</div>"
        f"</div>",
        unsafe_allow_html=True,
    )


def render_visit_edit_form(group, i, customers):
    """Aynı ziyarete ait tüm personel satırlarını tip/sayı ile düzenle."""
    j = visit_group_label(group)
    gid = j.get("group_id")
    old_date = j.get("date") or ""
    jid = gid or j.get("id", i)
    counts, ucret, names, phones = summarize_personnel(group)
    existing = {"pro": names["pro"], "student": names["student"], "phones": phones}
    session_gids = {(r.get("group_id") or "").strip() for r in group if (r.get("group_id") or "").strip()}

    if not customers:
        st.caption("Müşteri listesi boş — önce 👤 Müşteri sekmesinden ekleyin.")
        return
    c_map = {c["name"]: c["id"] for c in customers}
    names_list = list(c_map.keys())
    cur_name = j.get("name") or ""
    cust_idx = names_list.index(cur_name) if cur_name in names_list else 0

    tag_opts = ["Tek seferlik", "Abonelik (kota)"]
    cur_tag_i = 1 if j.get("job_tag") == "subscription" else 0

    with st.expander("✏️ İşi düzenle"):
        if len(session_gids) > 1:
            st.info("Bu kartta birden fazla abonelik kotası birleşik. Düzenleme için admin panelini kullanın.")
            return
        st.caption("Telefon ve konum müşteri profilinden gelir → **👤 Müşteri** sekmesi.")

        em = st.selectbox("Müşteri", names_list, index=cust_idx, key=f"ed_cust_{jid}_{i}")
        ed = st.date_input(
            "Tarih",
            value=parse_tr_date(j.get("date")),
            min_value=min_visible_date(),
            format="DD.MM.YYYY",
            key=f"ed_date_{jid}_{i}",
        )
        etag = st.radio("İş tipi", tag_opts, index=cur_tag_i, horizontal=True, key=f"ed_tag_{jid}_{i}")
        epc = st.number_input(
            "Müşteri tutarı (₺)",
            min_value=0.0,
            step=500.0,
            value=float(j.get("price_customer") or 0),
            key=f"ed_pc_{jid}_{i}",
        )

        st.markdown("**Personel (tip ve sayı)**")
        pc1, pc2 = st.columns(2)
        e_pro_n = pc1.number_input(
            "Profesyonel sayısı", min_value=0, max_value=20,
            value=int(counts["pro"]), key=f"ed_pro_n_{jid}_{i}",
        )
        e_stu_n = pc2.number_input(
            "Öğrenci sayısı", min_value=0, max_value=20,
            value=int(counts["student"]), key=f"ed_stu_n_{jid}_{i}",
        )
        pc3, pc4 = st.columns(2)
        e_pro_u = pc3.number_input(
            "Prof. yevmiye (₺)", min_value=0.0, step=50.0,
            value=float(ucret["pro"]), key=f"ed_pro_u_{jid}_{i}",
        )
        e_stu_u = pc4.number_input(
            "Öğrenci yevmiye (₺)", min_value=0.0, step=50.0,
            value=float(ucret["student"]), key=f"ed_stu_u_{jid}_{i}",
        )

        if st.button("Güncelle", key=f"ed_btn_{jid}_{i}", type="primary", use_container_width=True):
            if int(e_pro_n) + int(e_stu_n) < 1:
                st.warning("En az 1 personel girin.")
            else:
                new_cid = c_map[em]
                new_ds = format_tr_date(ed)
                new_tag = "subscription" if "Abonelik" in etag else "one_time"
                personeller = expand_personnel_by_type(
                    e_pro_n, e_pro_u, e_stu_n, e_stu_u, existing,
                )
                new_gid = next(
                    ((r.get("group_id") or "").strip() for r in group if (r.get("group_id") or "").strip()),
                    "",
                ) or str(uuid.uuid4())[:8]
                new_rows = build_visit_db_rows(
                    new_gid, new_ds, new_cid, new_tag, float(epc), personeller,
                )
                del_action = visit_delete_action(group)
                if del_action:
                    add_to_queue(f"İş güncelle: {em}", del_action[0], del_action[1])
                else:
                    for r in group:
                        add_to_queue("Silme", "DELETE FROM jobs WHERE id=%s", (r["id"],))
                add_to_queue(
                    f"İş yeniden oluştur: {em}",
                    JOB_INSERT_SQL,
                    new_rows,
                    is_bulk=True,
                )
                for r in list(group):
                    if r in jobs_list:
                        jobs_list.remove(r)
                profil = next((c for c in customers if c["id"] == new_cid), {})
                for row in new_rows:
                    _gid, ds, cid, jtype, wp, cut, tag, prepaid, _cph, _loc, sname, sph = row
                    jobs_list.append({
                        "id": f"tmp_{uuid.uuid4().hex[:8]}", "group_id": _gid, "date": ds,
                        "customer_id": cid, "job_type": jtype, "price_worker": wp,
                        "price_customer": cut, "job_tag": tag, "is_prepaid": prepaid,
                        "staff_name": sname, "staff_phone": sph,
                        "name": em,
                        "customer_phone": profil.get("phone"),
                        "customer_location": profil.get("location"),
                        "is_collected": 0, "is_worker_paid": 0,
                    })
                st.rerun()


def sync_month_from_date(ds):
    """Seçili tarihe göre ay/yıl session değerlerini güncelle."""
    parca = ds.split(".")
    if len(parca) == 3:
        st.session_state.basit_sm = int(parca[1])
        st.session_state.basit_sy = int(parca[2])


def apply_date_navigation(now):
    """Tüm tarih/ay değişimleri widget'lardan önce — tek kaynak: sel_date."""
    min_d = min_visible_date()

    day_nav = st.session_state.pop("_day_nav", None)
    if day_nav == "prev":
        d = parse_tr_date(st.session_state.sel_date) - timedelta(days=1)
        if d >= min_d:
            st.session_state.sel_date = format_tr_date(d)
    elif day_nav == "next":
        d = parse_tr_date(st.session_state.sel_date) + timedelta(days=1)
        st.session_state.sel_date = format_tr_date(d)
    elif day_nav == "today":
        st.session_state.sel_date = format_tr_date(date.today())

    if parse_tr_date(st.session_state.sel_date) < min_d:
        st.session_state.sel_date = format_tr_date(min_d)

    sync_month_from_date(st.session_state.sel_date)

    if "basit_sm" not in st.session_state:
        st.session_state.basit_sm = now.month
    if "basit_sy" not in st.session_state:
        st.session_state.basit_sy = now.year

    month_nav = st.session_state.pop("_month_nav", None)
    if month_nav == "prev":
        sm_v, sy_v = st.session_state.basit_sm, st.session_state.basit_sy
        st.session_state.basit_sm = 12 if sm_v == 1 else sm_v - 1
        if sm_v == 1:
            st.session_state.basit_sy = sy_v - 1
    elif month_nav == "next":
        sm_v, sy_v = st.session_state.basit_sm, st.session_state.basit_sy
        st.session_state.basit_sm = 1 if sm_v == 12 else sm_v + 1
        if sm_v == 12:
            st.session_state.basit_sy = sy_v + 1
    elif month_nav == "today":
        st.session_state.sel_date = format_tr_date(date.today())
        sync_month_from_date(st.session_state.sel_date)

    st.session_state.pop("basit_gun_picker", None)


db = st.session_state.db_data
jobs_list = db.get("jobs", [])
personnel = db.get("service_personnel", [])
expenses_list = db.get("expenses", [])
now = datetime.now()
q_len = len(st.session_state.pending_actions)

apply_date_navigation(now)
sm = st.session_state.basit_sm
sy = st.session_state.basit_sy
sd_global = st.session_state.sel_date

# --- ÜST BAR ---
st.markdown('<div class="mobil-bar">', unsafe_allow_html=True)
b1, b2, b3 = st.columns([1, 2, 1])
with b1:
    if st.button("🔄", help="Yenile", use_container_width=True):
        refresh_data()
        st.rerun()
with b2:
    if q_len:
        st.markdown(f'<div class="queue-box">⚠️ {q_len} bekliyor</div>', unsafe_allow_html=True)
    else:
        st.success("✓ Senkron", icon="✅")
with b3:
    if st.button("💾 KAYDET", type="primary", disabled=q_len == 0, use_container_width=True):
        commit_queue()
st.markdown("</div>", unsafe_allow_html=True)

st.caption(f"📊 {len(jobs_list)} iş · {len(personnel)} personel · 📅 {sd_global}")

st.title("📱 Vardiya")
tab_ekle, tab_takvim, tab_musteri, tab_personel, tab_gider = st.tabs(
    ["➕ İş", "📅 Günler", "👤 Müşteri", "👷 Personel", "💸 Gider"]
)

# ========== İŞ EKLE ==========
with tab_ekle:
    custs = db.get("customers", [])
    c_map = {c["name"]: c["id"] for c in custs}
    cust_by_id = {c["id"]: c for c in custs}

    sc = st.selectbox("Müşteri", ["— Seçin —"] + list(c_map.keys()), key="basit_musteri")

    jt = st.radio("İş tipi", ["Tek seferlik", "Abonelik (kota)"], key="basit_tip")
    min_d = min_visible_date()
    if jt == "Tek seferlik":
        d1 = st.date_input("Başlangıç", max(datetime.now().date(), min_d), min_value=min_d, key="basit_d1")
        d2 = st.date_input("Bitiş", max(datetime.now().date(), min_d), min_value=min_d, key="basit_d2")
        days = st.multiselect(
            "Hangi günler?",
            ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"],
            default=["Pazartesi"], key="basit_days",
        )
        kota = 0
    else:
        kota = st.number_input("Kota sayısı", min_value=1, value=4, step=1, key="basit_kota")
        d1, d2, days = None, None, []

    tp = st.number_input("Müşteri tutarı (₺)", 0.0, step=500.0, key="basit_tp")
    pm = st.radio("Tutar türü", ["Günlük", "Toplam"], horizontal=True, key="basit_pm")

    st.markdown("**Gidecek personel (tip ve sayı)**")
    pc1, pc2 = st.columns(2)
    pro_sayi = pc1.number_input("Profesyonel sayısı", min_value=0, max_value=20, value=1, key="basit_pro_n")
    ogrenci_sayi = pc2.number_input("Öğrenci sayısı", min_value=0, max_value=20, value=0, key="basit_stu_n")
    pc3, pc4 = st.columns(2)
    pro_ucret = pc3.number_input("Prof. yevmiye (₺)", min_value=0.0, step=50.0, key="basit_pro_u")
    ogrenci_ucret = pc4.number_input("Öğrenci yevmiye (₺)", min_value=0.0, step=50.0, key="basit_stu_u")
    st.caption("Örn: 2 Profesyonel + 1 Öğrenci — aynı ziyarette gruplanır.")

    if st.button("✅ Sepete ekle", type="primary", use_container_width=True):
        if sc == "— Seçin —":
            st.warning("Müşteri seçin.")
        elif int(pro_sayi) + int(ogrenci_sayi) < 1:
            st.warning("En az 1 personel girin (tip ve sayı).")
        else:
            personeller = expand_personnel_by_type(
                pro_sayi, pro_ucret, ogrenci_sayi, ogrenci_ucret,
            )
            if jt == "Tek seferlik":
                tr = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]
                dates, curr = [], d1
                while curr <= d2:
                    if tr[curr.weekday()] in days:
                        dates.append(curr)
                    curr += timedelta(1)
                tag = "one_time"
            else:
                dates = [None] * int(kota)
                tag = "subscription"

            if not dates:
                st.warning("Tarih/kota yok.")
            else:
                st.session_state.draft_jobs.append(Is(
                    musteri_id=c_map[sc], musteri_adi=sc, job_tag=tag, tarihler=dates,
                    musteri_tutari=tp, fiyat_modu=pm,
                    personeller=personeller,
                ))
                st.success("Sepete eklendi.")
                st.rerun()

    st.divider()
    st.markdown("#### 🛒 Sepet")
    if not st.session_state.draft_jobs:
        st.caption("Sepet boş.")
    else:
        for i, is_obj in enumerate(st.session_state.draft_jobs):
            tip = "🔄 Abonelik" if is_obj.job_tag == "subscription" else "🔹 Tek sefer"
            p_ozet = personel_listesi_ozet(is_obj.personeller)
            profil = cust_by_id.get(is_obj.musteri_id, {})
            tel = profil.get("phone") or "—"
            st.info(
                f"**{is_obj.musteri_adi}** — {tip}\n"
                f"{is_obj.ziyaret_sayisi} gün/kota · 👷 {p_ozet}\n"
                f"📞 {tel} (müşteri profili)"
            )
            if st.button("Sepetten kaldır", key=f"basit_draft_del_{i}", use_container_width=True):
                st.session_state.draft_jobs.pop(i)
                st.rerun()
        if st.button("💾 Kuyruğa ekle", type="primary", use_container_width=True, key="basit_kuyruk"):
            rows = []
            for is_obj in st.session_state.draft_jobs:
                for row in is_obj.db_satirlarina_donustur():
                    rows.append(row)
                    gid, ds, cid, jtype, wp, cut, tag, prepaid, _cph, _loc, sname, sph = row
                    profil = cust_by_id.get(cid, {})
                    st.session_state.db_data["jobs"].append({
                        "id": f"tmp_{uuid.uuid4().hex[:8]}", "group_id": gid, "date": ds,
                        "customer_id": cid, "job_type": jtype, "price_worker": wp,
                        "price_customer": cut, "job_tag": tag, "is_prepaid": prepaid,
                        "staff_name": sname, "staff_phone": sph,
                        "name": is_obj.musteri_adi,
                        "customer_phone": profil.get("phone"),
                        "customer_location": profil.get("location"),
                        "is_collected": 0, "is_worker_paid": 0,
                    })
            if rows:
                add_to_queue(f"{len(rows)} iş", JOB_INSERT_SQL, rows, is_bulk=True)
                st.session_state.draft_jobs = []
                st.rerun()

# ========== TAKVİM ==========
with tab_takvim:
    custs_edit = db.get("customers", [])
    sub_meta = db.get("subscription_meta", {})
    ms = f"{sm:02d}.{sy}"
    month_jobs = [j for j in jobs_list if j.get("date") and ms in j["date"]]

    def get_sub_label(group):
        return subscription_labels_merged(group, sub_meta)

    day_map = {}
    for group in group_jobs_by_visit(month_jobs):
        j = visit_group_label(group)
        d = j["date"]
        day_map.setdefault(d, {"jobs": {}, "kisi": 0})
        day_map[d]["kisi"] += len(group)
        label = f"{j['name']}{get_sub_label(group)}"
        day_map[d]["jobs"].setdefault(label, {"tag": j.get("job_tag", "one_time"), "kisi": 0})
        day_map[d]["jobs"][label]["kisi"] += len(group)

    sd = st.session_state.sel_date
    min_d = min_visible_date()
    cur = parse_tr_date(sd)

    st.markdown('<div class="nav-day-btn">', unsafe_allow_html=True)
    nav1, nav2, nav3 = st.columns(3)
    with nav1:
        if st.button("◀ Dün", key="basit_dun", use_container_width=True):
            st.session_state._day_nav = "prev"
            st.rerun()
    with nav2:
        if st.button("📍 Bugün", key="basit_bugun_nav", use_container_width=True):
            st.session_state._day_nav = "today"
            st.rerun()
    with nav3:
        if st.button("Yarın ▶", key="basit_yarin", use_container_width=True):
            st.session_state._day_nav = "next"
            st.rerun()
    st.markdown("</div>", unsafe_allow_html=True)

    gun = st.date_input(
        "Gün seç",
        value=cur,
        min_value=min_d,
        format="DD.MM.YYYY",
    )
    picked = format_tr_date(gun)
    if picked != sd:
        st.session_state.sel_date = picked
        st.rerun()

    sd = st.session_state.sel_date
    st.markdown(f'<div class="day-header">📅 {sd}</div>', unsafe_allow_html=True)

    djs = [j for j in jobs_list if j.get("date") == sd]
    visit_groups = sort_visit_groups(group_jobs_by_visit(djs))

    if not visit_groups:
        st.info("Bu gün için planlanmış iş yok.")
    else:
        st.caption(f"{len(visit_groups)} ziyaret · {len(djs)} personel")
        prev_tier = None
        for i, group in enumerate(visit_groups):
            tier = 0 if visit_has_pro(group) else 1
            if prev_tier == 0 and tier == 1:
                render_tier_separator()
            prev_tier = tier

            j = visit_group_label(group)
            jid = j.get("group_id") or j.get("id", i)
            render_visit_card(group, sub_meta)

            if j.get("job_tag") == "subscription":
                for sgi, (sg_gid, sg) in enumerate(split_group_by_session(group)):
                    sg_rep = visit_group_label(sg)
                    sg_lbl = subscription_label(sg_rep, sub_meta) or f"Kota {sgi + 1}"
                    sg_counts, _, _, _ = summarize_personnel(sg)
                    sg_badge = format_personnel_badge(sg_counts)
                    bc1, bc2 = st.columns([3, 1])
                    bc1.caption(f"{sg_lbl.strip(' []')} · 👷 {sg_badge}")
                    if bc2.button("🔙 Havuz", key=f"basit_back_{sg_gid}_{i}_{sgi}", help="Kotayı havuza al"):
                        add_to_queue(
                            "Kotaya Geri Al",
                            "UPDATE jobs SET date='' WHERE group_id=%s",
                            (sg_gid,),
                        )
                        for r in sg:
                            r["date"] = ""
                        st.rerun()

            act1, act2, act3 = st.columns(3)
            contact = job_musteri_telefon(j)
            loc = maps_link(job_musteri_konum(j))
            ct = tel_link(contact)
            with act1:
                render_action_link("📞 Ara", ct)
            with act2:
                render_action_link("🗺️ Harita", loc, new_tab=True)
            with act3:
                if st.button("🗑️", key=f"basit_del_{jid}_{i}", use_container_width=True, help="Sil"):
                    del_action = visit_delete_action(group)
                    if del_action:
                        add_to_queue("Silme", del_action[0], del_action[1])
                    else:
                        for r in group:
                            add_to_queue("Silme", "DELETE FROM jobs WHERE id=%s", (r["id"],))
                    for r in list(group):
                        if r in jobs_list:
                            jobs_list.remove(r)
                    st.rerun()

            render_visit_edit_form(group, i, custs_edit)
            st.divider()

    # --- Alt: ay takvimi & bekleyen kotalar ---
    with st.expander("📅 Ay takvimi", expanded=False):
        nav_a, nav_b, nav_c = st.columns(3)
        if nav_a.button("◀ Önceki ay", key="basit_prev_m", use_container_width=True):
            st.session_state._month_nav = "prev"
            st.rerun()
        if nav_b.button("📍 Bugün", key="basit_bugun_cal", use_container_width=True):
            st.session_state._month_nav = "today"
            st.rerun()
        if nav_c.button("Sonraki ay ▶", key="basit_next_m", use_container_width=True):
            st.session_state._month_nav = "next"
            st.rerun()

        st.markdown(f"**{calendar.month_name[sm]} {sy}**")
        num_days = calendar.monthrange(sy, sm)[1]
        for row_start in range(1, num_days + 1, 3):
            cols = st.columns(3)
            for col_i, day in enumerate(range(row_start, min(row_start + 3, num_days + 1))):
                ds_btn = f"{day:02d}.{ms}"
                with cols[col_i]:
                    lbl = f"{day} · 👥{day_map[ds_btn]['kisi']}" if ds_btn in day_map else str(day)
                    btn_type = "primary" if ds_btn == sd else "secondary"
                    if st.button(lbl, key=f"basit_day_{day}", type=btn_type, use_container_width=True):
                        st.session_state.sel_date = ds_btn
                        st.rerun()

    with st.expander("📥 Bekleyen kotalar", expanded=False):
        unscheduled = [j for j in jobs_list if not j.get("date") and j.get("job_tag") == "subscription"]
        pkgs = {}
        for uj in unscheduled:
            pid = uj["group_id"].split("_")[0]
            if pid not in pkgs:
                pkgs[pid] = {
                    "name": uj["name"],
                    "sessions": {},
                    "total": sub_meta.get("pkg_totals", {}).get(pid, 0),
                }
            gid = uj.get("group_id")
            pkgs[pid]["sessions"].setdefault(gid, []).append(uj)

        if not pkgs:
            st.caption("Bekleyen kota yok.")
        else:
            for pid, pdata in pkgs.items():
                pending = len(pdata["sessions"])
                st.markdown(
                    f"<div class='quota-box'><b>{pdata['name']}</b><br>"
                    f"Kalan: {pending}/{pdata['total']}</div>",
                    unsafe_allow_html=True,
                )
                for gid, rows in sorted(pdata["sessions"].items(), key=lambda x: x[0]):
                    counts, ucret, names, phones = summarize_personnel(rows)
                    badge = format_personnel_badge(counts, ucret)
                    st.caption(f"👷 {badge}")
                if st.button(f"📌 {sd} gününe ata", key=f"basit_ass_{pid}", use_container_width=True):
                    def _kota_sira(gid):
                        parca = gid.split("_")
                        return int(parca[1]) if len(parca) > 1 and parca[1].isdigit() else 0
                    sess = sorted(pdata["sessions"].keys(), key=_kota_sira)[0]
                    add_to_queue("Tarih atama", "UPDATE jobs SET date=%s WHERE group_id=%s", (sd, sess))
                    for job_mem in jobs_list:
                        if job_mem.get("group_id") == sess:
                            job_mem["date"] = sd
                    st.rerun()

# ========== MÜŞTERİ ==========
with tab_musteri:
    with st.form("basit_musteri_form"):
        ad = st.text_input("Ad Soyad", placeholder="Örn. Ayşe Yılmaz")
        tel = st.text_input("Telefon", placeholder="05xx xxx xx xx")
        konum = st.text_input("Konum linki (Google Maps)", placeholder="https://maps.google.com/...")
        if st.form_submit_button("Kuyruğa ekle", type="primary", use_container_width=True):
            if not ad.strip():
                st.warning("Ad girin.")
            else:
                add_to_queue(
                    f"Müşteri: {ad.strip()}",
                    "INSERT INTO customers (name, phone, location) VALUES (%s, %s, %s)",
                    (ad.strip(), tel.strip(), konum.strip()),
                )
                st.rerun()

    st.divider()
    arama = st.text_input("🔍 Müşteri ara", key="basit_musteri_ara")
    musteriler = db.get("customers", [])
    if arama.strip():
        q = arama.strip().lower()
        musteriler = [m for m in musteriler if q in (m.get("name") or "").lower()]

    if not musteriler:
        st.info("Müşteri bulunamadı.")
    else:
        st.caption(f"{len(musteriler)} müşteri")
        for i, m in enumerate(musteriler[:50]):
            mid = m["id"]
            loc = (m.get("location") or "").strip()
            loc_goster = f" · [📍 Konum]({loc})" if loc.startswith("http") else (f" · 📍 {loc[:40]}..." if loc else "")
            with st.container(border=True):
                st.markdown(f"**{m['name']}**")
                st.caption(f"📞 {m.get('phone') or '—'}{loc_goster}")
                with st.expander("✏️ Profili düzenle"):
                    en = st.text_input("Ad Soyad", value=m.get("name") or "", key=f"m_ad_{mid}_{i}")
                    et = st.text_input("Telefon", value=m.get("phone") or "", key=f"m_tel_{mid}_{i}")
                    ek = st.text_input(
                        "Konum linki (Google Maps)",
                        value=m.get("location") or "",
                        key=f"m_loc_{mid}_{i}",
                        placeholder="https://maps.google.com/...",
                    )
                    if st.button("Güncelle", key=f"m_btn_{mid}_{i}", use_container_width=True):
                        if not en.strip():
                            st.warning("Ad boş olamaz.")
                        else:
                            add_to_queue(
                                f"Müşteri güncelle: {en.strip()}",
                                "UPDATE customers SET name=%s, phone=%s, location=%s WHERE id=%s",
                                (en.strip(), et.strip(), ek.strip(), mid),
                            )
                            m.update(name=en.strip(), phone=et.strip(), location=ek.strip())
                            st.rerun()

# ========== PERSONEL ==========
with tab_personel:
    st.caption("Servis ekibi referans listesi. İş eklerken personel tipi ve sayısı girilir.")
    with st.form("basit_personel_form"):
        pad = st.text_input("Ad Soyad", placeholder="Örn. Ahmet Yılmaz")
        ptel = st.text_input("Telefon", placeholder="05xx xxx xx xx")
        if st.form_submit_button("Personel ekle (kuyruk)", type="primary", use_container_width=True):
            if not pad.strip():
                st.warning("Ad girin.")
            else:
                add_to_queue(
                    f"Personel: {pad.strip()}",
                    "INSERT INTO service_personnel (name, phone) VALUES (%s, %s)",
                    (pad.strip(), ptel.strip()),
                )
                st.rerun()

    st.divider()
    parca = st.text_input("🔍 Personel ara", key="basit_personel_ara")
    plist = personnel
    if parca.strip():
        q = parca.strip().lower()
        plist = [p for p in plist if q in (p.get("name") or "").lower()]

    if not plist:
        st.info("Henüz personel yok.")
    else:
        for i, p in enumerate(plist):
            with st.container(border=True):
                st.markdown(f"### 👷 {p['name']}")
                st.caption(f"📞 {p.get('phone') or '—'}")
                if st.button("🗑️ Personeli sil", key=f"del_staff_{p['id']}_{i}", use_container_width=True):
                    add_to_queue("Personel sil", "DELETE FROM service_personnel WHERE id=%s", (p["id"],))
                    st.rerun()

# ========== GİDER ==========
with tab_gider:
    st.caption("Gider kaydı girin. Ana panel analizine otomatik yansır.")
    g1, g2, g3 = st.columns([1, 2, 1])
    with g1:
        if st.button("◀ Ay", key="gider_prev_m", use_container_width=True):
            st.session_state._month_nav = "prev"
            st.rerun()
    with g2:
        st.markdown(f"**{calendar.month_name[sm]} {sy}**")
    with g3:
        if st.button("Ay ▶", key="gider_next_m", use_container_width=True):
            st.session_state._month_nav = "next"
            st.rerun()
    ms_gider = f"{sm:02d}.{sy}"

    with st.form("basit_gider_form"):
        gisim = st.text_input("İsim", placeholder="Örn. Yakıt, Kira, Malzeme")
        gaciklama = st.text_input("Açıklama", placeholder="Detay (isteğe bağlı)")
        gtarih = st.date_input("Tarih", max(date.today(), min_visible_date()), min_value=min_visible_date())
        gtutar = st.number_input("Miktar (₺)", min_value=0.0, step=50.0)
        if st.form_submit_button("Kuyruğa ekle", type="primary", use_container_width=True):
            if not gisim.strip():
                st.warning("İsim girin.")
            elif gtutar <= 0:
                st.warning("Miktar 0'dan büyük olmalı.")
            else:
                ds_g = gtarih.strftime("%d.%m.%Y")
                add_to_queue(
                    f"Gider: {gisim.strip()}",
                    "INSERT INTO expenses (date, name, description, amount) VALUES (%s, %s, %s, %s)",
                    (ds_g, gisim.strip(), gaciklama.strip(), float(gtutar)),
                )
                st.rerun()

    st.divider()
    ay_giderleri = [
        e for e in expenses_list
        if e.get("date") and ms_gider in e["date"]
    ]
    ay_giderleri.sort(key=lambda e: e.get("date", ""), reverse=True)
    toplam = sum(float(e.get("amount") or 0) for e in ay_giderleri)

    st.markdown(f"### {calendar.month_name[sm]} {sy}")
    st.metric("Ay toplam gider", f"{toplam:,.0f} ₺")

    if not ay_giderleri:
        st.info("Bu ay gider kaydı yok.")
    else:
        for i, e in enumerate(ay_giderleri):
            eid = e.get("id", i)
            tutar = float(e.get("amount") or 0)
            baslik = (e.get("name") or "").strip() or (e.get("description") or "—")
            detay = (e.get("description") or "").strip()
            if e.get("name") and detay and detay != baslik:
                alt = f"<br><span style='opacity:0.85'>{detay}</span>"
            else:
                alt = ""
            with st.container(border=True):
                st.markdown(
                    f'<div class="gider-card">'
                    f'<b>{e.get("date", "—")}</b> · {baslik}{alt}<br>'
                    f'<b>{tutar:,.0f} ₺</b>'
                    f'</div>',
                    unsafe_allow_html=True,
                )
                with st.expander("✏️ Düzenle"):
                    ed_isim = st.text_input("İsim", value=(e.get("name") or "").strip(), key=f"g_n_{eid}_{i}")
                    ed_aciklama = st.text_input(
                        "Açıklama", value=(e.get("description") or "").strip(), key=f"g_a_{eid}_{i}",
                    )
                    ed_tarih = st.date_input(
                        "Tarih",
                        max(parse_tr_date(e.get("date")), min_visible_date()),
                        min_value=min_visible_date(),
                        key=f"g_d_{eid}_{i}",
                    )
                    ed_tutar = st.number_input(
                        "Miktar (₺)", min_value=0.0, step=50.0,
                        value=float(e.get("amount") or 0), key=f"g_t_{eid}_{i}",
                    )
                    if st.button("Güncelle", key=f"g_btn_{eid}_{i}", use_container_width=True):
                        if not ed_isim.strip():
                            st.warning("İsim girin.")
                        elif ed_tutar <= 0:
                            st.warning("Miktar 0'dan büyük olmalı.")
                        else:
                            yeni_tarih = ed_tarih.strftime("%d.%m.%Y")
                            add_to_queue(
                                f"Gider güncelle: {ed_isim.strip()}",
                                """UPDATE expenses SET date=%s, name=%s, description=%s, amount=%s
                                   WHERE id=%s""",
                                (
                                    yeni_tarih, ed_isim.strip(), ed_aciklama.strip(),
                                    float(ed_tutar), eid,
                                ),
                            )
                            e.update(
                                date=yeni_tarih, name=ed_isim.strip(),
                                description=ed_aciklama.strip(), amount=float(ed_tutar),
                            )
                            st.rerun()
                if st.button("🗑️ Sil", key=f"del_gider_{eid}_{i}", use_container_width=True):
                    add_to_queue("Gider sil", "DELETE FROM expenses WHERE id=%s", (eid,))
                    expenses_list.remove(e)
                    st.rerun()

with st.sidebar:
    st.caption("Servis görünümü için:")
    st.code("streamlit run servis.py", language="bash")
