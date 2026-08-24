"""
Servis ekibi paneli — günlük iş listesi (telefon, personel, konum).
Salt okunur; veri mobil panelden gelir.

Çalıştırma: streamlit run servis.py
"""
import streamlit as st
import calendar
from datetime import datetime, date

from vardiya.db import (
    load_panel_data, job_musteri_telefon, job_musteri_konum, render_action_link,
    min_visible_date, group_jobs_by_visit, summarize_personnel, format_personnel_html,
    visit_group_label, subscription_labels_merged,
    sort_visit_groups, visit_has_pro,
)
from vardiya.auth import require_auth

st.set_page_config(
    page_title="Servis Listesi",
    page_icon="🚐",
    layout="centered",
    initial_sidebar_state="collapsed",
)
require_auth("servis")

st.markdown("""
<style>
    .block-container { padding-top: 1rem; padding-bottom: 3rem; max-width: 720px; }
    .servis-card {
        background: var(--secondary-background-color, #f8f9fa);
        border: 1px solid rgba(128, 128, 128, 0.25);
        border-radius: 12px;
        padding: 14px 16px; margin-bottom: 10px;
        box-shadow: 0 1px 3px rgba(0,0,0,.06);
        color: var(--text-color, #1a1a1a);
    }
    .servis-card h3 {
        margin: 0 0 8px 0; font-size: 1.1rem;
        color: var(--text-color, #1a1a1a) !important;
    }
    .servis-meta { font-size: 15px; line-height: 1.6; color: var(--text-color, #333); }
    .servis-meta a { color: #1565c0; font-weight: 600; text-decoration: none; }
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
    div.stButton > button { min-height: 44px; border-radius: 10px; }
    .day-header {
        background: #1565c0; color: #fff; padding: 12px 16px;
        border-radius: 10px; text-align: center; font-weight: 700; margin-bottom: 14px;
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
    }
</style>
""", unsafe_allow_html=True)


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


if "servis_data" not in st.session_state:
    st.session_state.servis_data = {}

if "servis_date" not in st.session_state:
    st.session_state.servis_date = date.today().strftime("%d.%m.%Y")


def refresh():
    st.session_state.servis_data = load_panel_data(admin=False)


if not st.session_state.servis_data:
    refresh()

min_d = min_visible_date()
if datetime.strptime(st.session_state.servis_date, "%d.%m.%Y").date() < min_d:
    st.session_state.servis_date = min_d.strftime("%d.%m.%Y")

now = datetime.now()
db = st.session_state.servis_data
jobs = db.get("jobs", [])
sub_meta = db.get("subscription_meta", {})

# --- Üst bar ---
c1, c2, c3 = st.columns([1, 2, 1])
with c1:
    if st.button("🔄", help="Yenile", use_container_width=True):
        refresh()
        st.rerun()
with c2:
    st.title("🚐 Servis")
with c3:
    st.caption(f"{len(jobs)} iş")

# Tarih seçimi
col_d, col_m, col_y = st.columns(3)
with col_d:
    cur = datetime.strptime(st.session_state.servis_date, "%d.%m.%Y").date()
    gun = st.date_input(
        "Gün", max(cur, min_d), min_value=min_d, label_visibility="collapsed",
    )
    st.session_state.servis_date = gun.strftime("%d.%m.%Y")
with col_m:
    if st.button("◀ Dün", use_container_width=True):
        from datetime import timedelta
        d = datetime.strptime(st.session_state.servis_date, "%d.%m.%Y").date() - timedelta(days=1)
        if d >= min_d:
            st.session_state.servis_date = d.strftime("%d.%m.%Y")
        st.rerun()
with col_y:
    if st.button("Yarın ▶", use_container_width=True):
        from datetime import timedelta
        d = datetime.strptime(st.session_state.servis_date, "%d.%m.%Y").date() + timedelta(days=1)
        st.session_state.servis_date = d.strftime("%d.%m.%Y")
        st.rerun()

sel = st.session_state.servis_date
st.markdown(f'<div class="day-header">📅 {sel}</div>', unsafe_allow_html=True)

day_jobs = [j for j in jobs if j.get("date") == sel]
visit_groups = sort_visit_groups(group_jobs_by_visit(day_jobs))

if not visit_groups:
    st.info("Bu gün için planlanmış iş yok.")
else:
    st.caption(f"{len(visit_groups)} ziyaret · {len(day_jobs)} personel")
    prev_tier = None
    for idx, group in enumerate(visit_groups):
        tier = 0 if visit_has_pro(group) else 1
        if prev_tier == 0 and tier == 1:
            st.markdown(
                '<div class="list-tier-sep"><span>🎓 Öğrenci personelli işler</span></div>',
                unsafe_allow_html=True,
            )
        prev_tier = tier

        j = visit_group_label(group)
        musteri = j.get("name") or "Müşteri"
        counts, ucret, names, phones = summarize_personnel(group)
        personel_line = format_personnel_html(counts, ucret, names, phones)
        contact = job_musteri_telefon(j)
        loc = maps_link(job_musteri_konum(j))
        tag = "🔄" if j.get("job_tag") == "subscription" else "🔹"
        sub = subscription_labels_merged(group, sub_meta)
        meta_lines = [
            f"👷 {personel_line}",
            f"📞 Müşteri: <b>{contact or '—'}</b>",
        ]
        if loc:
            meta_lines.append(f'📍 <a href="{loc}" target="_blank">Konuma git</a>')

        st.markdown(
            f'<div class="servis-card">'
            f'<h3>{tag} {musteri}{sub}</h3>'
            f'<div class="servis-meta">' + "<br>".join(meta_lines) + "</div>"
            f"</div>",
            unsafe_allow_html=True,
        )

        btn_cols = st.columns(2)
        ct = tel_link(contact)
        with btn_cols[0]:
            render_action_link("📞 Müşteriyi ara", ct)
        with btn_cols[1]:
            render_action_link("🗺️ Harita", loc, new_tab=True)

st.divider()
st.caption("Bu panel salt okunurdur. Düzenleme için mobil vardiya panelini kullanın.")
