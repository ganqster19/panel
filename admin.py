"""
Admin vardiya paneli — finans, analiz, takvim, puantaj, AI asistan.

Çalıştırma: streamlit run admin.py
"""
import streamlit as st
import pandas as pd
import psycopg2
from psycopg2.extras import RealDictCursor, execute_values
import calendar
import uuid
import os
import re
import json
import difflib
from datetime import datetime, timedelta, date
from dataclasses import dataclass, field
from vardiya.perf import PerfTimer, perf_log
from vardiya.auth import require_auth
from vardiya.db import (
    group_jobs_by_visit, visit_group_label, summarize_personnel,
    format_personnel_badge, personel_listesi_ozet, expand_personnel_by_type,
    build_visit_db_rows, JOB_INSERT_SQL, build_subscription_calendar_meta,
    subscription_labels_merged, subscription_label, sort_visit_groups, visit_has_pro,
    visit_delete_action, split_group_by_session, visit_customer_revenue,
    hesapla_abonelik_yukumluluk, ay_ziyaret_cirosu,
    build_visit_summaries, aggregate_visit_summaries, customer_ranking_from_summaries,
    db_diagnostics, JOB_TAG_ADD_LABELS, job_tag_from_label, job_tag_label,
    job_tag_icon, job_tag_css, job_tag_option_index, is_subscription_tag,
    job_tag_css_block, render_ciro_pie, ciro_by_tag,
    personnel_assign_sql, apply_person_to_row,
)
from vardiya.isakisi import (
    FIYAT, tip_coz, yevmiye, kadro_kur, kadro_say, ziyaret_ucreti,
    abonelik_paket_ucreti, musteri_esle, abonelik_erteleme_plani, fiyat_ozeti,
)
from vardiya.islemler import (
    tarih_coz, is_ekle as is_ekle_core, is_tasi as is_tasi_core,
)

try:
    from vardiya.db import render_name_assignment, DB_MODULE_VERSION
except ImportError:  # sunucuda eski vardiya/db.py varsa panel tamamen çökmesin
    DB_MODULE_VERSION = "eski sürüm — Reboot gerekli"

    def render_name_assignment(*_a, **_k):
        st.warning("Personel atama modülü eski — uygulamayı Reboot edin.")

# --- SAYFA AYARLARI ---
st.set_page_config(page_title="Vardiya (Offline & Hızlı)", page_icon="⚡", layout="wide")
require_auth("admin")
perf_log("admin.py:startup", "script_rerun_start", {"rerun": True}, "C")

# --- İŞ (JOB) SINIFI ---
# Sepete eklenen her "iş", müşterisi, tarihleri, personel listesi ve müşteriden alınan
# ücretiyle birlikte tek bir nesne (class) olarak tutulur. Veritabanına yazılırken bu nesne
# personel sayısı kadar satıra bölünür (şema bunu gerektiriyor) ama ekranda ve sepette
# hep tek bir "iş" olarak görünür ve fiyat/personel bilgisi hep bu sınıfın üzerinde durur.
@dataclass
class Is:
    musteri_id: int
    musteri_adi: str
    job_tag: str                    # 'one_time' | 'subscription'
    tarihler: list                  # tek seferlik: [date, ...] / abonelik: [None]*kota
    musteri_tutari: float           # bu işten alınan ücret (fiyat_modu'na göre yorumlanır)
    fiyat_modu: str                 # 'Günlük' (her ziyarette alınır) | 'Toplam' (bir kere alınır)
    personeller: list = field(default_factory=list)   # [{'tip': 'student'|'pro', 'ucret': float}]
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])

    @property
    def personel_sayisi(self) -> int:
        return len(self.personeller)

    @property
    def ziyaret_sayisi(self) -> int:
        return len(self.tarihler)

    @property
    def gunluk_personel_maliyeti(self) -> float:
        return sum(p['ucret'] for p in self.personeller)

    @property
    def toplam_personel_maliyeti(self) -> float:
        return self.gunluk_personel_maliyeti * self.ziyaret_sayisi

    @property
    def toplam_musteri_geliri(self) -> float:
        return self.musteri_tutari * self.ziyaret_sayisi if self.fiyat_modu == "Günlük" else self.musteri_tutari

    @property
    def net_kar(self) -> float:
        return self.toplam_musteri_geliri - self.toplam_personel_maliyeti

    def db_satirlarina_donustur(self):
        """Bu işi, jobs tablosuna yazılacak (group_id, date, customer_id, job_type, price_worker,
        price_customer, job_tag, is_prepaid) satırlarına böler. Abonelikte müşteri ücreti yalnızca
        ilk kotanın (index 0) ilk personeline yazılır. Tek seferlik işlerde fiyat_modu'na göre dağıtılır."""
        pkg_id = str(uuid.uuid4())[:8]
        satirlar = []
        odendi_mi = False
        for i, d in enumerate(self.tarihler):
            ds = d.strftime("%d.%m.%Y") if d else ""
            gid = f"{pkg_id}_{i}" if is_subscription_tag(self.job_tag) else pkg_id

            if is_subscription_tag(self.job_tag):
                bu_ziyaret_tutari = self.musteri_tutari if i == 0 else 0.0
            else:
                bu_ziyaret_tutari = self.musteri_tutari if (self.fiyat_modu == "Günlük" or not odendi_mi) else 0.0
                if self.fiyat_modu == "Toplam":
                    odendi_mi = True

            ilk_personel_odendi = False
            for p in self.personeller:
                cut = bu_ziyaret_tutari if not ilk_personel_odendi else 0.0
                ilk_personel_odendi = True
                prepaid = 1 if cut > 0 else 0
                satirlar.append((gid, ds, self.musteri_id, p['tip'], p['ucret'], cut, self.job_tag, prepaid))
        return satirlar

# --- CSS ---
st.markdown("""
<style>
""" + job_tag_css_block() + """
    .job-subs, .job-once, .job-hotel, .job-anahtar { padding: 1px 4px; border-radius: 4px; font-size: 10px; display: block; margin-bottom: 2px; font-weight: 700; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .net-profit { color: #008f39; font-weight: bold; font-size: 12px; text-align: right; margin-top: 2px; border-top: 1px solid #eee; }
    .stButton button { width: 100%; border-radius: 5px; }
    .report-box { background-color: #f0f2f6; padding: 10px; border-radius: 5px; margin-bottom: 10px; color: #000; }
    .queue-box { background-color: #ffebee; border: 2px solid #ef5350; color: #c62828; padding: 10px; border-radius: 5px; font-weight: bold; text-align: center; margin-bottom: 10px; }
    .quota-box { background-color: #fff8e1; border: 1px dashed #ffb300; padding: 10px; border-radius: 5px; margin-bottom: 10px; color: #000; }
</style>
""", unsafe_allow_html=True)

# --- SESSION STATES ---
if 'draft_jobs' not in st.session_state: st.session_state.draft_jobs = [] 
if 'pending_actions' not in st.session_state: st.session_state.pending_actions = [] 
if 'sel_date' not in st.session_state: st.session_state.sel_date = datetime.now().strftime("%d.%m.%Y")
if 'db_data' not in st.session_state: st.session_state.db_data = {}

# --- DB BAĞLANTI ---
def get_db_connection():
    s = st.secrets["supabase"]
    password = s["password"]
    port = int(s.get("port", 5432))
    dbname = s["dbname"]
    ssl = {"sslmode": "require", "connect_timeout": 10}

    denemeler = [(s["host"], s["user"])]
    if s.get("pooler_host") and s.get("pooler_user"):
        denemeler.append((s["pooler_host"], s["pooler_user"]))

    son_hata = None
    for host, user in denemeler:
        try:
            return psycopg2.connect(
                host=host,
                database=dbname,
                user=user,
                password=password,
                port=port,
                cursor_factory=RealDictCursor,
                **ssl,
            )
        except Exception as e:
            son_hata = e
            continue

    st.error(f"Veritabanına bağlanılamadı: {son_hata}")
    st.stop()

# --- VERİ ÇEKME ---
def _query_table(cursor, sql, label, errors):
    """Sorgu çalıştır; hata olursa boş liste + mesaj döndür."""
    try:
        cursor.execute(sql)
        return cursor.fetchall()
    except Exception as e:
        errors[label] = str(e)
        return []


def refresh_data():
    with PerfTimer("admin.py:refresh_data", "db_refresh_total", "A") as t:
        conn = get_db_connection()
        errors = {}
        try:
            with conn.cursor() as c:
                data = {"_load_errors": errors}
                q_start = __import__("time").perf_counter()
                data["jobs"] = _query_table(
                    c,
                    "SELECT j.*, c.name FROM jobs j LEFT JOIN customers c ON j.customer_id=c.id",
                    "jobs",
                    errors,
                )
                t.extra["jobs_query_ms"] = round((__import__("time").perf_counter() - q_start) * 1000, 2)
                t.extra["jobs_count"] = len(data["jobs"])

                data["customers"] = _query_table(c, "SELECT * FROM customers ORDER BY name", "customers", errors)
                data["students"] = _query_table(c, "SELECT * FROM students ORDER BY name", "students", errors)
                data["pros"] = _query_table(c, "SELECT * FROM professionals ORDER BY name", "pros", errors)
                data["salaries"] = _query_table(c, "SELECT * FROM salary_payments", "salaries", errors)
                data["trans"] = _query_table(c, "SELECT * FROM transactions", "trans", errors)
                data["attendance"] = _query_table(c, "SELECT * FROM daily_attendance", "attendance", errors)
                data["availability"] = _query_table(c, "SELECT * FROM personnel_availability", "availability", errors)
                data["notes"] = _query_table(c, "SELECT * FROM daily_notes ORDER BY date", "notes", errors)
                data["expenses"] = _query_table(c, "SELECT * FROM expenses ORDER BY date", "expenses", errors)
                data["cash_inflow"] = _query_table(c, "SELECT * FROM cash_inflow", "cash_inflow", errors)

                try:
                    data["_db_diag"] = db_diagnostics(conn)
                except Exception as e:
                    data["_db_diag"] = {"host": "?", "counts": {}, "errors": {"diag": str(e)}}

                st.session_state.db_data = data
                t.extra["table_counts"] = {k: len(v) for k, v in data.items() if isinstance(v, list)}
                return True
        except Exception as e:
            st.error(f"Veri çekme hatası: {e}")
            return False
        finally:
            conn.close()

if not st.session_state.db_data:
    refresh_data()

# --- AYLIK FİNANS HESAPLAMA YARDIMCILARI ---
def ay_arama(sm, sy):
    return f".{sm:02d}.{sy}"

def hesapla_gunluk_giderler(db, sm, sy):
    arama = ay_arama(sm, sy)
    return sum(float(e['amount'] or 0) for e in db.get('expenses', []) if e.get('date') and arama in e['date'])

def hesapla_ay_ozet(db, jobs_list, trans_list, sal_list, sm, sy):
    arama = ay_arama(sm, sy)
    sal_arama = f"{sm:02d}-{sy}"
    exp_jobs = sum(float(j['price_worker'] or 0) for j in jobs_list if j.get('date') and arama in j['date'])
    exp_trans = sum(float(t['amount'] or 0) for t in trans_list if t.get('date') and t.get('type') == 'expense' and arama in t['date'])
    exp_sal = sum(float(s['amount'] or 0) for s in sal_list if s.get('month_year') and sal_arama in s['month_year'])
    exp_daily = hesapla_gunluk_giderler(db, sm, sy)
    inc_jobs = ay_ziyaret_cirosu(jobs_list, sm, sy)
    inc_trans = sum(float(t['amount'] or 0) for t in trans_list if t.get('date') and t.get('type') == 'income' and arama in t['date'])
    return {
        'inc_jobs': inc_jobs, 'inc_trans': inc_trans,
        'exp_jobs': exp_jobs, 'exp_trans': exp_trans, 'exp_sal': exp_sal, 'exp_daily': exp_daily,
        'total_inc': inc_jobs + inc_trans,
        'total_exp': exp_jobs + exp_trans + exp_sal + exp_daily,
    }

def hesapla_analiz_otomatik(db, jobs_list, trans_list, sm, sy):
    arama = ay_arama(sm, sy)
    month_str_puantaj = f"{sm:02d}.{sy}"
    month_jobs = [j for j in jobs_list if j.get('date') and arama in j['date']]
    otomatik_maas = sum(
        p['salary'] + sum(1850 for a in db.get('attendance', [])
            if str(a['person_id']) == str(p['id']) and a.get('person_type') == 'pro'
            and a.get('status') == 'present' and month_str_puantaj in a.get('date', ''))
        for p in db.get('pros', [])
    )
    saha_maliyet = sum(float(j['price_worker'] or 0) for j in month_jobs)
    gunluk_giderler = hesapla_gunluk_giderler(db, sm, sy)
    diger_giderler = sum(float(t['amount'] or 0) for t in trans_list if t.get('date') and t.get('type') == 'expense' and arama in t['date'])
    return {
        'maas': float(otomatik_maas),
        'saha': float(saha_maliyet),
        'gunluk_giderler': float(gunluk_giderler),
        'diger_giderler': float(diger_giderler),
        'month_jobs': month_jobs,
    }

def analiz_param_init(month_key, otomatik):
    """Ay değişince analiz alanlarını doldur: varsayılan 0, yalnızca saha işçi maliyeti otomatik."""
    if st.session_state.get('analiz_aktif_ay') != month_key:
        st.session_state.analiz_aktif_ay = month_key
        st.session_state[f"analiz_maas_{month_key}"] = 0.0
        st.session_state[f"analiz_saha_{month_key}"] = otomatik['saha']
        st.session_state[f"analiz_diger_{month_key}"] = 0.0

# --- KUYRUK FONKSİYONLARI ---
def add_to_queue(desc, query, params, is_bulk=False):
    st.session_state.pending_actions.append({
        'desc': desc, 'query': query, 'params': params, 'is_bulk': is_bulk
    })
    st.toast(f"✅ Eklendi: {desc}")

def commit_queue():
    if not st.session_state.pending_actions: return
    conn = get_db_connection()
    try:
        with conn.cursor() as c:
            for action in st.session_state.pending_actions:
                if action['is_bulk']:
                    execute_values(c, action['query'], action['params'])
                else:
                    c.execute(action['query'], action['params'])
            conn.commit()
        st.session_state.pending_actions = [] 
        refresh_data()
        st.success("Tüm veriler kaydedildi!")
        st.rerun()
    except Exception as e:
        conn.rollback()
        st.error(f"Kayıt Hatası: {e}")
    finally:
        conn.close()

# --- 🤖 AI ARAÇLARI (FONKSİYONLAR) ---
# Asistan panelin kendi giriş mantığını kullanır: iş satırları Is sınıfından üretilir ve
# hiçbir şey doğrudan yazılmaz — her işlem sidebar'daki kuyruğa düşer, kullanıcı onaylar.

def ai_tarih(metin) -> str:
    """Serbest metni GG.AA.YYYY'ye çevirir ('yarın', 'haftaya perşembe', '3 temmuz'...).

    Panel ile API'nin aynı tarih yorumunu kullanması için ortak çekirdeğe devreder.
    """
    return tarih_coz(metin)


def _ai_musteri(ad):
    """Müşteriyi bul: 'emir bey', 'nazlı hanım' gibi hitapları temizler, adayları döner."""
    return musteri_esle(st.session_state.db_data.get('customers', []) or [], ad)


def _ai_kisi(ad):
    """Personeli isimle bul: profesyonel, öğrenci veya servis listesi."""
    q = str(ad or "").strip().casefold()
    if not q:
        return None
    data = st.session_state.db_data
    for kind, key in (("pro", "pros"), ("student", "students"), ("service", "service_personnel")):
        for p in data.get(key, []) or []:
            nm = (p.get('name') or "").casefold()
            if nm and (nm == q or q in nm):
                return {
                    "kind": kind, "id": p.get('id'),
                    "name": p.get('name'), "phone": p.get('phone'),
                }
    return None


def _ai_tip(t) -> str:
    return tip_coz(t)


def _ai_etiket(t) -> str:
    s = str(t or "").strip().casefold()
    if "abone" in s or "kota" in s:
        return "subscription"
    if "otel" in s or "hotel" in s:
        return "hotel"
    if "anahtar" in s or "teslim" in s or "inşaat" in s or "insaat" in s:
        return "turnkey"
    return "one_time"


def _ai_musteri_isleri(cid, tarih):
    return [
        j for j in st.session_state.db_data.get('jobs', [])
        if j.get('customer_id') == cid and (j.get('date') or "") == tarih
    ]


def ai_is_ekle(is_json: str) -> str:
    """Panelin 'İş Ekle' formuyla birebir aynı şekilde yeni iş oluşturur. Tek argüman JSON metnidir.

    Şema (yalnızca bildiğin alanları doldur):
    {
      "musteri": "Ahmet Yılmaz",
      "etiket": "tek seferlik" | "otel" | "anahtar teslim" | "abonelik",
      "tarihler": ["10.09.2026", "12.09.2026"],
      "kota": 4,
      "pro_sayisi": 2, "ogrenci_sayisi": 1,
      "personeller": [{"tip": "pro", "ucret": 2500}, {"tip": "ogrenci", "ucret": 1800}],
      "musteri_tutari": 9600,
      "fiyat_modu": "gunluk" | "toplam",
      "isimler": ["Ali", "Veli"],
      "not": "kapıcıdan anahtar alınacak"
    }
    Kadro: "pro_sayisi"/"ogrenci_sayisi" yeterlidir; yevmiyeler tarifeden gelir
    (profesyonel 2500 ₺, öğrenci 1800 ₺). Farklı yevmiye varsa "personeller" listesini kullan.
    Fiyat: tek seferlik/otel/anahtar teslim işlerde "musteri_tutari" verilmezse tarife uygulanır —
    profesyonel başına 4800 ₺, öğrenci başına 2800 ₺. Kullanıcı "toplam 9600" gibi bir tutar
    söylerse onu "musteri_tutari" olarak gönder.
    ABONELİKTE tutar zorunludur: kullanıcı söylemediyse önce sor, sonra bu aracı çağır.
    Abonelikte tarih verilmez, "kota" ziyaret hakkı sayısıdır (varsayılan 4) ve kotalar havuzda
    bekler; tutarın tamamı ilk kotaya yazılır, kota tüketildiğinde gelir değil yalnızca gider işlenir.
    Diğer etiketlerde en az bir tarih gerekir; her tarih ayrı bir ziyarettir.
    "fiyat_modu" gunluk ise tutar her ziyarette, toplam ise tüm iş için bir kez alınır.
    "isimler" verilirse personel slotlarına sırayla isimle atama yapılır.
    Birden fazla farklı iş varsa bu aracı her iş için ayrı çağır."""
    mesaj, eylemler, onizleme = is_ekle_core(st.session_state.db_data, is_json)
    if not eylemler:
        return mesaj
    for aciklama, sql, params in eylemler:
        add_to_queue(f"🤖 {aciklama}", sql, params)
    for satir in onizleme:
        satir = dict(satir)
        satir["id"] = None
        st.session_state.db_data.setdefault("jobs", []).append(satir)
    return (
        "Kuyruğa eklendi: " + mesaj
        + " Onay için sidebar'daki 'DEĞİŞİKLİKLERİ KAYDET' butonuna basılmalı."
    )


def ai_is_tasi(musteri_adi: str, yeni_tarih: str, eski_tarih: str = "") -> str:
    """Bir rezervasyonu başka güne erteler/taşır. Rezervasyon türünü kendisi belirler.

    Tek seferlik / otel / anahtar teslim işlerde yalnızca o günün tarihi değişir.
    Abonelikte ertelenen kotadan SONRAKİ planlı kotalar da aynı gün sayısı kadar kaydırılır,
    yani abonelik düzeni (haftalık vb. periyot) korunarak yeniden kurulur.
    eski_tarih boş bırakılırsa müşterinin bugüne en yakın planlı işi taşınır."""
    mesaj, eylemler = is_tasi_core(st.session_state.db_data, musteri_adi, yeni_tarih, eski_tarih)
    if not eylemler:
        return mesaj
    for aciklama, sql, params in eylemler:
        add_to_queue(f"🤖 {aciklama}", sql, params)
    return "Kuyruğa eklendi: " + mesaj


def ai_is_iptal(musteri_adi: str, tarih: str) -> str:
    """Bir müşterinin belirtilen günündeki tüm iş kayıtlarını siler."""
    musteri, adaylar = _ai_musteri(musteri_adi)
    if not musteri:
        return f"Hata: '{musteri_adi}' bulunamadı." + (f" Adaylar: {', '.join(adaylar)}" if adaylar else "")
    ds = ai_tarih(tarih)
    if not ds:
        return "Hata: tarih anlaşılmadı, GG.AA.YYYY biçiminde gönder."
    mevcut = _ai_musteri_isleri(musteri['id'], ds)
    if not mevcut:
        return f"Hata: {musteri['name']} için {ds} tarihinde iş yok."
    add_to_queue(
        f"🤖 AI İptal: {musteri['name']}",
        "DELETE FROM jobs WHERE customer_id = %s AND date = %s",
        (musteri['id'], ds),
    )
    return f"Kuyruğa eklendi: {musteri['name']} · {ds} · {len(mevcut)} satır silinecek."


def _musteri_bul(musteri_adi: str):
    musteri, _ = _ai_musteri(musteri_adi)
    return musteri['id'] if musteri else None


def _personel_bul(personel_adi: str):
    for p in st.session_state.db_data.get('pros', []):
        if str(personel_adi or "").casefold() in (p.get('name') or "").casefold():
            return p
    return None


def ai_kota_ekle(musteri_adi: str, eklenecek_kota: int, personel_yevmiyesi: float = 0.0) -> str:
    """Mevcut aboneliğe tarihsiz (havuzda bekleyen) yeni kota ekler. Her kota 1 personeli temsil eder."""
    cid = _musteri_bul(musteri_adi)
    if not cid:
        return f"Hata: '{musteri_adi}' isimli müşteri bulunamadı."

    mevcut_pids = [j['group_id'].split('_')[0] for j in st.session_state.db_data.get('jobs', [])
                   if j.get('customer_id') == cid and j.get('job_tag') == 'subscription' and j.get('group_id')]
    if not mevcut_pids:
        return f"Hata: {musteri_adi} için abonelik paketi yok. Önce ai_is_ekle ile etiket='abonelik' işi oluştur."
    pid = mevcut_pids[0]

    mevcut_seans_no = [int(j['group_id'].split('_')[1]) for j in st.session_state.db_data.get('jobs', [])
                       if j.get('group_id', '').startswith(f"{pid}_") and j['group_id'].split('_')[1].isdigit()]
    baslangic = (max(mevcut_seans_no) + 1) if mevcut_seans_no else 0

    for i in range(int(eklenecek_kota)):
        gid = f"{pid}_{baslangic + i}"
        query = "INSERT INTO jobs (group_id, date, customer_id, job_type, price_worker, price_customer, job_tag, is_prepaid) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
        params = (gid, '', cid, 'pro', float(personel_yevmiyesi), 0.0, 'subscription', 0)
        add_to_queue(f"🤖 Kota Ekle: {musteri_adi}", query, params)

    return f"Kuyruğa eklendi: {musteri_adi} aboneliğine {eklenecek_kota} kota (havuzda bekleyecek)."


def ai_kota_sil(musteri_adi: str, silinecek_kota: int) -> str:
    """Havuzda bekleyen (tarihi olmayan) abonelik kotalarından belirtilen sayıda siler."""
    cid = _musteri_bul(musteri_adi)
    if not cid:
        return f"Hata: '{musteri_adi}' isimli müşteri bulunamadı."

    bekleyenler = [j for j in st.session_state.db_data.get('jobs', [])
                   if j.get('customer_id') == cid and j.get('job_tag') == 'subscription' and not j.get('date')]
    if not bekleyenler:
        return f"Hata: {musteri_adi} için havuzda bekleyen kota yok."

    silinecekler = bekleyenler[:int(silinecek_kota)]
    for j in silinecekler:
        add_to_queue(f"🤖 Kota Sil: {musteri_adi}", "DELETE FROM jobs WHERE id=%s", (j['id'],))

    return f"Kuyruğa eklendi: {musteri_adi} havuzundan {len(silinecekler)} kota silinecek."


def ai_kota_yerlestir(musteri_adi: str, tarih: str, adet: int = 1) -> str:
    """Havuzda bekleyen abonelik kotasını/kotalarını belirli bir güne yerleştirir (takvime yazar)."""
    cid = _musteri_bul(musteri_adi)
    if not cid:
        return f"Hata: '{musteri_adi}' isimli müşteri bulunamadı."
    ds = ai_tarih(tarih)
    if not ds:
        return "Hata: tarih anlaşılmadı, GG.AA.YYYY biçiminde gönder."

    bekleyenler = [j for j in st.session_state.db_data.get('jobs', [])
                   if j.get('customer_id') == cid and j.get('job_tag') == 'subscription' and not (j.get('date') or '').strip()]
    if not bekleyenler:
        return f"Hata: {musteri_adi} için havuzda bekleyen kota yok. ai_kota_ekle ile kota ekleyebilirim."

    secilen = bekleyenler[:max(1, int(adet))]
    for j in secilen:
        add_to_queue(
            f"🤖 Kota Yerleştir: {musteri_adi}",
            "UPDATE jobs SET date=%s WHERE id=%s",
            (ds, j['id']),
        )
        j['date'] = ds
    return f"Kuyruğa eklendi: {musteri_adi} için {len(secilen)} kota {ds} tarihine yerleştirilecek."


def ai_kisi_ekle(musteri_adi: str, tarih: str, personel_tipi: str, yevmiye: float = 0.0) -> str:
    """Zaten planlanmış bir işe ek personel (kişi) ekler. personel_tipi 'ogrenci' veya 'pro'."""
    cid = _musteri_bul(musteri_adi)
    if not cid:
        return f"Hata: '{musteri_adi}' isimli müşteri bulunamadı."
    ds = ai_tarih(tarih)
    mevcut = _ai_musteri_isleri(cid, ds)
    if not mevcut:
        return f"Hata: {musteri_adi} için {ds or tarih} tarihinde iş yok. Yeni iş için ai_is_ekle kullan."

    gid = mevcut[0].get('group_id') or str(uuid.uuid4())[:8]
    tag = mevcut[0].get('job_tag', 'one_time')
    jt = _ai_tip(personel_tipi)

    query = "INSERT INTO jobs (group_id, date, customer_id, job_type, price_worker, price_customer, job_tag, is_prepaid) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
    add_to_queue(f"🤖 Kişi Ekle: {musteri_adi}", query, (gid, ds, cid, jt, float(yevmiye), 0.0, tag, 0))
    return f"Kuyruğa eklendi: {musteri_adi} · {ds} işine 1 {'öğrenci' if jt == 'student' else 'profesyonel'} eklenecek."


def ai_kisi_sil(musteri_adi: str, tarih: str, adet: int = 1) -> str:
    """Planlanmış bir işten kişi sayısını azaltır; önce atanmamış kayıtlar silinir."""
    cid = _musteri_bul(musteri_adi)
    if not cid:
        return f"Hata: '{musteri_adi}' isimli müşteri bulunamadı."
    ds = ai_tarih(tarih)
    mevcut = _ai_musteri_isleri(cid, ds)
    if not mevcut:
        return f"Hata: {musteri_adi} için {ds or tarih} tarihinde iş yok."

    mevcut.sort(key=lambda j: 0 if (not j.get('assigned_student_id') and not j.get('assigned_pro_id')) else 1)
    silinecekler = mevcut[:int(adet)]
    for j in silinecekler:
        add_to_queue(f"🤖 Kişi Sil: {musteri_adi}", "DELETE FROM jobs WHERE id=%s", (j['id'],))

    return f"Kuyruğa eklendi: {musteri_adi} · {ds} işinden {len(silinecekler)} kişi çıkarılacak."


def ai_personel_ata(musteri_adi: str, tarih: str, isimler: list[str]) -> str:
    """Belirli bir gündeki işin personel slotlarına isimle atama yapar (öğrenci/pro/servis listesinden)."""
    cid = _musteri_bul(musteri_adi)
    if not cid:
        return f"Hata: '{musteri_adi}' isimli müşteri bulunamadı."
    ds = ai_tarih(tarih)
    mevcut = _ai_musteri_isleri(cid, ds)
    if not mevcut:
        return f"Hata: {musteri_adi} için {ds or tarih} tarihinde iş yok."

    atanan, bulunamayan, yer_yok = [], [], 0
    for i, ad in enumerate([str(x) for x in (isimler or []) if str(x).strip()]):
        kisi = _ai_kisi(ad)
        if not kisi:
            bulunamayan.append(ad)
            continue
        if i >= len(mevcut):
            yer_yok += 1
            continue
        row = mevcut[i]
        if row.get('id') is None or str(row.get('id')).startswith('tmp_'):
            return "Hata: bu iş henüz kaydedilmedi. Önce kuyruğu kaydedin, sonra atama yapılabilir."
        query, params = personnel_assign_sql(kisi, row['id'])
        add_to_queue(f"🤖 Atama: {kisi['name']}", query, params)
        apply_person_to_row(row, kisi)
        atanan.append(kisi['name'])

    if not atanan and not bulunamayan:
        return "Hata: atanacak isim verilmedi."
    parcalar = []
    if atanan:
        parcalar.append(f"{ds} · {musteri_adi}: " + ", ".join(atanan) + " atandı (kuyrukta)")
    if bulunamayan:
        parcalar.append("bulunamayan isim: " + ", ".join(bulunamayan))
    if yer_yok:
        parcalar.append(f"{yer_yok} isim için boş personel slotu kalmadı (ai_kisi_ekle ile slot eklenebilir)")
    return ". ".join(parcalar) + "."


def ai_etiket_degistir(musteri_adi: str, tarih: str, etiket: str) -> str:
    """Bir günün işinin etiketini değiştirir: tek seferlik / otel / anahtar teslim / abonelik."""
    cid = _musteri_bul(musteri_adi)
    if not cid:
        return f"Hata: '{musteri_adi}' isimli müşteri bulunamadı."
    ds = ai_tarih(tarih)
    mevcut = _ai_musteri_isleri(cid, ds)
    if not mevcut:
        return f"Hata: {musteri_adi} için {ds or tarih} tarihinde iş yok."
    yeni = _ai_etiket(etiket)
    add_to_queue(
        f"🤖 Etiket: {musteri_adi}",
        "UPDATE jobs SET job_tag=%s WHERE customer_id=%s AND date=%s",
        (yeni, cid, ds),
    )
    return f"Kuyruğa eklendi: {musteri_adi} · {ds} işi '{job_tag_label(yeni)}' etiketine geçecek."


def ai_fiyat_guncelle(musteri_adi: str, tarih: str, musteri_tutari: float) -> str:
    """Bir gündeki işin müşteriden alınacak tutarını günceller (tutar ilk satıra yazılır)."""
    cid = _musteri_bul(musteri_adi)
    if not cid:
        return f"Hata: '{musteri_adi}' isimli müşteri bulunamadı."
    ds = ai_tarih(tarih)
    mevcut = _ai_musteri_isleri(cid, ds)
    if not mevcut:
        return f"Hata: {musteri_adi} için {ds or tarih} tarihinde iş yok."
    ilk = mevcut[0]
    add_to_queue(
        f"🤖 Fiyat: {musteri_adi}",
        "UPDATE jobs SET price_customer=0, is_prepaid=0 WHERE customer_id=%s AND date=%s",
        (cid, ds),
    )
    add_to_queue(
        f"🤖 Fiyat: {musteri_adi}",
        "UPDATE jobs SET price_customer=%s, is_prepaid=%s WHERE id=%s",
        (float(musteri_tutari), 1 if float(musteri_tutari) > 0 else 0, ilk['id']),
    )
    return f"Kuyruğa eklendi: {musteri_adi} · {ds} işinin müşteri tutarı {float(musteri_tutari):,.0f} ₺ olacak."


def ai_tahsilat_isaretle(musteri_adi: str, tarih: str) -> str:
    """Bir gündeki işin müşteri tahsilatını 'alındı' olarak işaretler."""
    cid = _musteri_bul(musteri_adi)
    if not cid:
        return f"Hata: '{musteri_adi}' isimli müşteri bulunamadı."
    ds = ai_tarih(tarih)
    mevcut = [j for j in _ai_musteri_isleri(cid, ds) if float(j.get('price_customer') or 0) > 0]
    if not mevcut:
        return f"Hata: {musteri_adi} · {ds or tarih} için tahsil edilecek tutar bulunamadı."
    for j in mevcut:
        add_to_queue(f"🤖 Tahsilat: {musteri_adi}", "UPDATE jobs SET is_collected=1 WHERE id=%s", (j['id'],))
    toplam = sum(float(j.get('price_customer') or 0) for j in mevcut)
    return f"Kuyruğa eklendi: {musteri_adi} · {ds} · {toplam:,.0f} ₺ tahsil edildi olarak işaretlenecek."


def ai_musteri_bul(ad: str) -> str:
    """Müşterinin sistemde kayıtlı olup olmadığını söyler; benzer adları aday olarak listeler."""
    musteri, adaylar = _ai_musteri(ad)
    if musteri:
        tel = (musteri.get('phone') or "").strip() or "telefon yok"
        return f"Kayıtlı: {musteri['name']} ({tel}). Bu müşteriye iş girilebilir."
    if adaylar:
        return (
            f"'{ad}' birebir bulunamadı. Benzer kayıtlar: " + ", ".join(adaylar)
            + ". Hangisi olduğunu kullanıcıya sor; hiçbiri değilse ai_musteri_ekle ile yeni profil aç."
        )
    return f"'{ad}' sistemde yok. ai_musteri_ekle ile yeni müşteri profili açılabilir."


def ai_musteri_ekle(ad: str, telefon: str = "", konum_url: str = "") -> str:
    """Yeni müşteri profili açar. Kayıt anında veritabanına yazılır; hemen ardından iş girilebilir."""
    if not str(ad or "").strip():
        return "Hata: müşteri adı boş."
    mevcut, adaylar = _ai_musteri(ad)
    if mevcut:
        return f"'{mevcut['name']}' zaten kayıtlı, yeni profil açmaya gerek yok."

    temiz_ad = str(ad).strip()
    try:
        conn = get_db_connection()
        with conn.cursor() as c:
            c.execute(
                "INSERT INTO customers (name, phone, location) VALUES (%s, %s, %s) RETURNING id",
                (temiz_ad, str(telefon or "").strip(), str(konum_url or "").strip()),
            )
            yeni_id = c.fetchone()["id"]
        conn.commit()
        conn.close()
    except Exception as e:
        return f"Hata: '{temiz_ad}' müşterisi oluşturulamadı ({e})."

    st.session_state.db_data.setdefault('customers', []).append({
        "id": yeni_id, "name": temiz_ad,
        "phone": str(telefon or "").strip(), "location": str(konum_url or "").strip(),
    })
    ek = f" Benzer kayıtlar da vardı: {', '.join(adaylar)}." if adaylar else ""
    return f"'{temiz_ad}' müşteri profili oluşturuldu ve kaydedildi. Artık iş girebilirim.{ek}"


def ai_personel_ekle(ad: str, tip: str, telefon: str = "", maas: float = 0.0) -> str:
    """Yeni personel kaydı: tip 'pro' (profesyonel) veya 'ogrenci'."""
    if not str(ad or "").strip():
        return "Hata: personel adı boş."
    if _ai_tip(tip) == "student":
        add_to_queue(
            f"🤖 Öğrenci Ekle: {ad}",
            "INSERT INTO students (name, phone) VALUES (%s, %s)",
            (str(ad).strip(), str(telefon or "").strip()),
        )
        return f"Kuyruğa eklendi: '{ad}' öğrenci olarak eklenecek."
    add_to_queue(
        f"🤖 Pro Ekle: {ad}",
        "INSERT INTO professionals (name, phone, salary) VALUES (%s, %s, %s)",
        (str(ad).strip(), str(telefon or "").strip(), float(maas or 0)),
    )
    return f"Kuyruğa eklendi: '{ad}' profesyonel olarak eklenecek."


def ai_gider_ekle(tarih: str, aciklama: str, tutar: float) -> str:
    """Belirli bir güne kira/malzeme/yakıt gibi ekstra gider kaydı ekler."""
    ds = ai_tarih(tarih) or datetime.now().strftime("%d.%m.%Y")
    add_to_queue(
        f"🤖 Gider Ekle: {aciklama}",
        "INSERT INTO expenses (date, description, amount) VALUES (%s, %s, %s)",
        (ds, aciklama, float(tutar)),
    )
    return f"Kuyruğa eklendi: {ds} · '{aciklama}' · {float(tutar):,.0f} ₺ gider."


def ai_not_ekle(tarih: str, not_metni: str) -> str:
    """Belirli bir güne serbest metin not ekler; o günde not varsa altına eklenir."""
    ds = ai_tarih(tarih) or datetime.now().strftime("%d.%m.%Y")
    mevcut_not = next((n.get('note') or '' for n in st.session_state.db_data.get('notes', []) if n.get('date') == ds), '')
    birlesik_not = f"{mevcut_not}\n{not_metni}".strip() if mevcut_not else not_metni

    query = """INSERT INTO daily_notes (date, note) VALUES (%s, %s)
               ON CONFLICT (date) DO UPDATE SET note = EXCLUDED.note"""
    add_to_queue(f"🤖 Not Ekle: {ds}", query, (ds, birlesik_not))
    return f"Kuyruğa eklendi: {ds} notu → '{not_metni}'"


def ai_maas_ode(personel_adi: str, tutar: float, tarih: str = None) -> str:
    """Aylık maaşla çalışan profesyonele maaş ödemesi kaydeder."""
    pro = _personel_bul(personel_adi)
    if not pro:
        return f"Hata: '{personel_adi}' isimli profesyonel bulunamadı."

    ds = ai_tarih(tarih) or datetime.now().strftime("%d.%m.%Y")
    mk = f"{ds.split('.')[1]}-{ds.split('.')[2]}"
    query = "INSERT INTO salary_payments (pro_id,amount,payment_date,month_year,payment_type) VALUES (%s,%s,%s,%s,'monthly')"
    add_to_queue(f"🤖 Maaş Öde: {pro['name']}", query, (pro['id'], float(tutar), ds, mk))
    return f"Kuyruğa eklendi: {pro['name']} · {float(tutar):,.0f} ₺ maaş ({mk})."


def ai_gunluk_ucret_ode(personel_adi: str, tarih: str, tutar: float) -> str:
    """Günlük (yevmiyeli) çalışan personele belirli bir gün için yapılan ödemeyi kaydeder.
    Aylık maaşlı personel için bu aracı DEĞİL, ai_maas_ode aracını kullan."""
    pro = _personel_bul(personel_adi)
    if not pro:
        return f"Hata: '{personel_adi}' isimli personel bulunamadı."

    ds = ai_tarih(tarih) or datetime.now().strftime("%d.%m.%Y")
    query = "INSERT INTO transactions (date, type, category, amount, description, related_id) VALUES (%s, %s, %s, %s, %s, %s)"
    params = (ds, 'expense', 'gunluk_ucret', float(tutar), f"{pro['name']} - günlük ücret", pro['id'])
    add_to_queue(f"🤖 Günlük Ücret: {pro['name']}", query, params)
    return f"Kuyruğa eklendi: {pro['name']} · {ds} · {float(tutar):,.0f} ₺ günlük ücret."


# --- 🔎 AI OKUMA ARAÇLARI (soru cevaplama) ---

def _ai_ozet_meta():
    db_data = st.session_state.db_data
    return (
        build_subscription_calendar_meta(db_data.get('jobs', [])),
        db_data.get('pros', []),
        db_data.get('students', []),
    )


def ai_gun_ozeti(tarih: str) -> str:
    """Belirli bir günün iş listesi: müşteri, etiket, kişi sayısı, kadro isimleri, ciro ve kâr."""
    ds = ai_tarih(tarih)
    if not ds:
        return "Hata: tarih anlaşılmadı."
    meta, pros, students = _ai_ozet_meta()
    gun = datetime.strptime(ds, "%d.%m.%Y").date()
    ozetler = build_visit_summaries(
        st.session_state.db_data.get('jobs', []),
        date_from=gun, date_to=gun, meta=meta, pros=pros, students=students,
    )
    if not ozetler:
        return f"{ds}: planlanmış iş yok."
    satir = [f"{ds} · {len(ozetler)} ziyaret:"]
    for s in ozetler:
        satir.append(
            f"- {s['customer']} · {s['tag_label']}{s['sub_label']} · {s['kisi']} kişi "
            f"({s['kadro_isimleri']}) · ciro {s['ciro']:,.0f} ₺ · kâr {s['kar']:,.0f} ₺"
        )
    top = aggregate_visit_summaries(ozetler)
    satir.append(f"Toplam: ciro {top['ciro']:,.0f} ₺, maliyet {top['maliyet']:,.0f} ₺, kâr {top['kar']:,.0f} ₺.")
    return "\n".join(satir)


def ai_musteri_ozeti(musteri_adi: str, son_kayit: int = 8) -> str:
    """Bir müşterinin geçmiş işleri, toplam ciro/kâr ve bekleyen kotaları."""
    musteri, adaylar = _ai_musteri(musteri_adi)
    if not musteri:
        return f"Hata: '{musteri_adi}' bulunamadı." + (f" Adaylar: {', '.join(adaylar)}" if adaylar else "")
    meta, pros, students = _ai_ozet_meta()
    ozetler = build_visit_summaries(
        st.session_state.db_data.get('jobs', []),
        customer_id=musteri['id'], meta=meta, pros=pros, students=students,
    )
    bekleyen = len({
        (j.get('group_id') or '') for j in st.session_state.db_data.get('jobs', [])
        if j.get('customer_id') == musteri['id'] and j.get('job_tag') == 'subscription'
        and not (j.get('date') or '').strip()
    } - {''})
    if not ozetler:
        return f"{musteri['name']}: tarihli iş kaydı yok. Havuzda bekleyen kota: {bekleyen}."
    top = aggregate_visit_summaries(ozetler)
    satir = [
        f"{musteri['name']} · {top['visits']} ziyaret · ciro {top['ciro']:,.0f} ₺ · "
        f"kâr {top['kar']:,.0f} ₺ · bekleyen kota {bekleyen}. Son işler:"
    ]
    for s in ozetler[:max(1, int(son_kayit))]:
        satir.append(
            f"- {s['date']} · {s['tag_label']} · {s['kisi']} kişi ({s['kadro_isimleri']}) · "
            f"ciro {s['ciro']:,.0f} ₺ · kâr {s['kar']:,.0f} ₺"
        )
    return "\n".join(satir)


def ai_ay_ozeti(ay: int = 0, yil: int = 0) -> str:
    """Bir ayın toplam ciro/maliyet/kâr özeti, etiket dağılımı ve en kârlı müşteriler."""
    bugun = date.today()
    sm = int(ay) if ay else bugun.month
    sy = int(yil) if yil else bugun.year
    meta, pros, students = _ai_ozet_meta()
    ilk = date(sy, sm, 1)
    son = date(sy, sm, calendar.monthrange(sy, sm)[1])
    ozetler = build_visit_summaries(
        st.session_state.db_data.get('jobs', []),
        date_from=ilk, date_to=son, meta=meta, pros=pros, students=students,
    )
    if not ozetler:
        return f"{sm:02d}.{sy}: iş kaydı yok."
    top = aggregate_visit_summaries(ozetler)
    giderler = sum(
        float(e.get('amount') or 0) for e in st.session_state.db_data.get('expenses', [])
        if (e.get('date') or '').endswith(f".{sm:02d}.{sy}")
    )
    satir = [
        f"{sm:02d}.{sy} · {top['visits']} ziyaret · ciro {top['ciro']:,.0f} ₺ · "
        f"personel maliyeti {top['maliyet']:,.0f} ₺ · ek gider {giderler:,.0f} ₺ · "
        f"net {top['kar'] - giderler:,.0f} ₺.",
        "Etiket dağılımı:",
    ]
    for tag, tutar in ciro_by_tag(ozetler).items():
        if tutar > 0:
            satir.append(f"- {job_tag_label(tag)}: {tutar:,.0f} ₺")
    satir.append("En kârlı müşteriler:")
    for r in customer_ranking_from_summaries(ozetler)[:5]:
        satir.append(f"- {r['name']}: {r['visits']} ziyaret · kâr {r['kar']:,.0f} ₺")
    return "\n".join(satir)


def ai_bekleyen_kotalar() -> str:
    """Havuzda bekleyen (tarihi olmayan) tüm abonelik kotalarını müşteri bazında listeler."""
    sayac = {}
    for j in st.session_state.db_data.get('jobs', []):
        if j.get('job_tag') != 'subscription' or (j.get('date') or '').strip():
            continue
        ad = j.get('name') or '—'
        sayac.setdefault(ad, set()).add(j.get('group_id') or f"row{j.get('id')}")
    if not sayac:
        return "Havuzda bekleyen kota yok."
    return "Bekleyen kotalar:\n" + "\n".join(
        f"- {ad}: {len(gids)} kota" for ad, gids in sorted(sayac.items())
    )


def ai_liste(tip: str = "musteri") -> str:
    """Kayıtlı isimleri listeler: tip 'musteri', 'pro', 'ogrenci' veya 'servis'."""
    s = str(tip or "").casefold()
    db_data = st.session_state.db_data
    if s.startswith("mus") or s.startswith("müş"):
        adlar = [c.get('name') for c in db_data.get('customers', [])]
        baslik = "Müşteriler"
    elif s.startswith("ogr") or s.startswith("öğr"):
        adlar = [p.get('name') for p in db_data.get('students', [])]
        baslik = "Öğrenciler"
    elif s.startswith("ser"):
        adlar = [p.get('name') for p in db_data.get('service_personnel', [])]
        baslik = "Servis personeli"
    else:
        adlar = [p.get('name') for p in db_data.get('pros', [])]
        baslik = "Profesyoneller"
    adlar = [a for a in adlar if a]
    if not adlar:
        return f"{baslik}: kayıt yok."
    return f"{baslik} ({len(adlar)}): " + ", ".join(adlar)


AI_ARACLARI = [
    ai_is_ekle, ai_is_tasi, ai_is_iptal,
    ai_kota_ekle, ai_kota_sil, ai_kota_yerlestir,
    ai_kisi_ekle, ai_kisi_sil, ai_personel_ata,
    ai_etiket_degistir, ai_fiyat_guncelle, ai_tahsilat_isaretle,
    ai_musteri_bul, ai_musteri_ekle, ai_personel_ekle,
    ai_gider_ekle, ai_not_ekle,
    ai_maas_ode, ai_gunluk_ucret_ode,
    ai_gun_ozeti, ai_musteri_ozeti, ai_ay_ozeti, ai_bekleyen_kotalar, ai_liste,
]
# ==========================================
# ARAYÜZ SİDEBAR
# ==========================================
db = st.session_state.db_data

with st.sidebar:
    st.title("⚡ Panel")
    
    q_len = len(st.session_state.pending_actions)
    if q_len > 0:
        st.markdown(f'<div class="queue-box">⚠️ {q_len} İŞLEM BEKLİYOR</div>', unsafe_allow_html=True)
        if st.button("💾 DEĞİŞİKLİKLERİ KAYDET", type="primary"):
            with st.spinner("Sunucuya yazılıyor..."):
                commit_queue()
    else:
        st.success("Senkronize.")
    
    st.divider()
    
    now = datetime.now()
    sy = st.selectbox("Yıl", [now.year, now.year+1])
    sm = st.selectbox("Ay", range(1,13), index=now.month-1)
    
    jobs_list = db.get('jobs', [])
    trans_list = db.get('trans', [])
    sal_list = db.get('salaries', [])

    with PerfTimer("admin.py:sidebar", "sidebar_finance_sums", "B", {"jobs_count": len(jobs_list)}):
        ozet = hesapla_ay_ozet(db, jobs_list, trans_list, sal_list, sm, sy)
    
    total_inc = ozet['total_inc']
    total_exp = ozet['total_exp']
    exp_sal = ozet['exp_sal']
    exp_daily = ozet['exp_daily']
    net = total_inc - total_exp
    
    st.markdown(f"""
    <div class="report-box">
        <h4>{calendar.month_name[sm]} {sy}</h4><hr>
        <div style="display:flex;justify-content:space-between;"><span>Ciro:</span><b style="color:green">{total_inc:,.0f}</b></div>
        <div style="display:flex;justify-content:space-between;"><span>Maliyet:</span><b style="color:red">{total_exp:,.0f}</b></div>
        <div style="font-size:11px; color:gray; text-align:right;">(Maaş: {exp_sal:,.0f} | Günlük Gider: {exp_daily:,.0f})</div>
        <div style="border-top:1px solid #ccc; margin-top:5px; padding-top:5px; display:flex;justify-content:space-between;">
            <span>NET:</span><b style="color:{'green' if net>=0 else 'red'}">{net:,.0f}</b>
        </div>
    </div>""", unsafe_allow_html=True)

    st.caption(f"🗄️ DB: {len(jobs_list)} iş · {len(db.get('customers', []))} müşteri")
    st.caption(f"🔖 Kod sürümü: {DB_MODULE_VERSION}")

    load_errors = db.get("_load_errors") or {}
    db_diag = db.get("_db_diag") or {}
    if load_errors:
        with st.expander("⚠️ Veritabanı sorgu hataları", expanded=True):
            for tbl, msg in load_errors.items():
                st.error(f"**{tbl}:** {msg}")
    if len(jobs_list) == 0 and len(db.get("customers", [])) == 0:
        diag_counts = db_diag.get("counts") or {}
        if diag_counts:
            st.warning(
                f"Bağlanılan sunucu: `{db_diag.get('host', '?')}` · "
                f"Supabase ham sayım — jobs: {diag_counts.get('jobs', '?')}, "
                f"customers: {diag_counts.get('customers', '?')}"
            )
            if diag_counts.get("jobs", 0) and diag_counts.get("customers", 0):
                st.info("Tablolarda veri var ama JOIN/ sorgu boş döndü — yukarıdaki hatalara bakın.")
            elif diag_counts.get("jobs") == 0 and diag_counts.get("customers") == 0:
                st.info(
                    "Bağlantı başarılı ama tablolar boş görünüyor. "
                    "Streamlit Secrets’taki **host** değerinin Supabase projenizle aynı olduğunu doğrulayın."
                )
        diag_errors = db_diag.get("errors") or {}
        for tbl, msg in diag_errors.items():
            st.error(f"Teşhis ({tbl}): {msg}")
    
    if st.button("🔄 Verileri Yenile"):
        st.cache_data.clear()
        st.session_state.db_data = {}
        refresh_data()
        st.rerun()

    st.divider()
    if st.button("🗄️ Bilgisayara Yerel Yedek Al", type="secondary"):
        try:
            folder_name = "yedek_veriler"
            os.makedirs(folder_name, exist_ok=True)
            for table_key, table_rows in db.items():
                if table_rows:
                    df_backup = pd.DataFrame(table_rows)
                    df_backup.to_csv(f"{folder_name}/{table_key}.csv", index=False, encoding="utf-8-sig")
            st.sidebar.success(f"✅ Canlı yedek '{folder_name}/' klasörüne güncellendi!")
        except Exception as e:
            st.sidebar.error(f"Yedekleme Hatası: {e}")

st.title("🚀 Vardiya Merkezi")
st.divider()

tabs = st.tabs(["⚡ İş Ekle", "📅 Takvim", "💰 Finans", "📂 Kişiler", "📋 İş Özetleri", "📈 Analiz", "⏱️ Puantaj", "🤖 AI Asistan"])

# TAB 1: İŞ EKLE
with tabs[0]:
    custs = db.get('customers', [])
    c_map = {c['name']: c['id'] for c in custs}
    c1, c2 = st.columns([3, 2])

    with c1:
        st.markdown("#### 1️⃣ Müşteri & Tarih")
        sc = st.selectbox("Müşteri", ["-"] + list(c_map.keys()), key="ib_musteri")
        jt = st.radio("Etiket", list(JOB_TAG_ADD_LABELS), horizontal=True, key="ib_tip")
        tag_sec = job_tag_from_label(jt)

        if is_subscription_tag(tag_sec):
            st.info("Aboneliklerde tarih seçilmez. Kota, ziyaret sayısını belirtir; takvimden istenilen günlere dağıtılır.")
            kota = st.number_input("Kota (Toplam Ziyaret Sayısı)", min_value=1, value=4, step=1, key="ib_kota")
            d1, d2, days = None, None, []
        else:
            dc1, dc2 = st.columns(2)
            d1 = dc1.date_input("Başlangıç", datetime.now(), key="ib_d1")
            d2 = dc2.date_input("Bitiş", datetime.now(), key="ib_d2")
            days = st.multiselect("Günler", ["Pazartesi","Salı","Çarşamba","Perşembe","Cuma","Cumartesi","Pazar"], default=["Pazartesi"], key="ib_days")
            kota = 0

        st.markdown("#### 2️⃣ Ücretlendirme")
        pc1, pc2 = st.columns(2)
        tp = pc1.number_input("Müşteriden Alınacak Tutar (₺)", 0.0, step=500.0, key="ib_tp")
        pm = pc2.radio("Bu tutar...", ["Günlük", "Toplam"], horizontal=True, key="ib_pm",
                        help="Günlük: her ziyarette ayrı alınır. Toplam: tüm iş için bir kez alınır (ilk ziyarette).")

        st.markdown("#### 3️⃣ Personel (tip ve sayı)")
        pp1, pp2 = st.columns(2)
        ib_pro_n = pp1.number_input("Profesyonel sayısı", min_value=0, max_value=20, value=1, key="ib_pro_n")
        ib_stu_n = pp2.number_input("Öğrenci sayısı", min_value=0, max_value=20, value=0, key="ib_stu_n")
        pp3, pp4 = st.columns(2)
        ib_pro_u = pp3.number_input("Prof. yevmiye (₺)", min_value=0.0, step=50.0, key="ib_pro_u")
        ib_stu_u = pp4.number_input("Öğrenci yevmiye (₺)", min_value=0.0, step=50.0, key="ib_stu_u")
        st.caption("Aynı ziyarette birden fazla personel tip/sayı ile girilir.")

        personel_sayisi = int(ib_pro_n) + int(ib_stu_n)
        gunluk_maliyet = int(ib_pro_n) * float(ib_pro_u) + int(ib_stu_n) * float(ib_stu_u)
        if jt == "Tek Seferlik (Tarihli)":
            tr = ["Pazartesi","Salı","Çarşamba","Perşembe","Cuma","Cumartesi","Pazar"]
            onizleme_ziyaret = 0
            curr = d1
            while curr <= d2:
                if tr[curr.weekday()] in days: onizleme_ziyaret += 1
                curr += timedelta(1)
        else:
            onizleme_ziyaret = int(kota)

        st.divider()

        # --- CANLI ÖZET ---
        toplam_maliyet = gunluk_maliyet * onizleme_ziyaret
        toplam_gelir = tp * onizleme_ziyaret if pm == "Günlük" else tp
        net = toplam_gelir - toplam_maliyet

        oc1, oc2, oc3, oc4 = st.columns(4)
        oc1.metric("👥 Personel", personel_sayisi)
        oc2.metric("📅 Ziyaret", onizleme_ziyaret)
        oc3.metric("💰 Toplam Gelir", f"{toplam_gelir:,.0f} ₺")
        oc4.metric("💹 Net Kâr (tahmini)", f"{net:,.0f} ₺")

        if st.button("✅ Sepete Ekle", type="primary", width="stretch"):
            if sc == "-":
                st.warning("Lütfen bir müşteri seçin.")
            elif personel_sayisi < 1:
                st.warning("Lütfen en az 1 personel girin (tip ve sayı).")
            else:
                personeller = expand_personnel_by_type(ib_pro_n, ib_pro_u, ib_stu_n, ib_stu_u)
                if is_subscription_tag(tag_sec):
                    dates = [None] * int(kota)
                    tag = 'subscription'
                else:
                    dates = []
                    tr = ["Pazartesi","Salı","Çarşamba","Perşembe","Cuma","Cumartesi","Pazar"]
                    curr = d1
                    while curr <= d2:
                        if tr[curr.weekday()] in days: dates.append(curr)
                        curr += timedelta(1)
                    tag = tag_sec

                if not dates:
                    st.warning("Seçilen aralıkta uygun bir tarih yok.")
                else:
                    is_obj = Is(
                        musteri_id=c_map[sc], musteri_adi=sc, job_tag=tag, tarihler=dates,
                        musteri_tutari=tp, fiyat_modu=pm,
                        personeller=personeller
                    )
                    st.session_state.draft_jobs.append(is_obj)
                    st.success(f"'{sc}' işi sepete eklendi.")
                    st.rerun()

    with c2:
        st.markdown("#### 🛒 Sepet")
        if not st.session_state.draft_jobs:
            st.caption("Sepette henüz iş yok.")
        else:
            if st.button("💾 KUYRUĞA EKLE (KAYDETMEK İÇİN)", type="primary", width="stretch"):
                rows = []
                for is_obj in st.session_state.draft_jobs:
                    for gid, ds, cid, jtype, worker_price, cust_cut, tag, prepaid in is_obj.db_satirlarina_donustur():
                        rows.append((gid, ds, cid, jtype, worker_price, cust_cut, tag, prepaid))
                        st.session_state.db_data['jobs'].append({
                            'id': f"tmp_{uuid.uuid4().hex[:8]}", 'group_id': gid, 'date': ds, 'customer_id': cid,
                            'job_type': jtype, 'price_worker': worker_price, 'price_customer': cust_cut,
                            'job_tag': tag, 'is_prepaid': prepaid, 'name': is_obj.musteri_adi,
                            'is_collected': 0, 'is_worker_paid': 0, 'assigned_student_id': None, 'assigned_pro_id': None
                        })

                if rows:
                    add_to_queue(f"{len(rows)} İş Girişi",
                                 "INSERT INTO jobs (group_id, date, customer_id, job_type, price_worker, price_customer, job_tag, is_prepaid) VALUES %s",
                                 rows, is_bulk=True)
                    st.session_state.draft_jobs = []
                    st.rerun()

            for i, is_obj in enumerate(st.session_state.draft_jobs):
                with st.container(border=True):
                    baslik = f"{job_tag_icon(is_obj.job_tag)} {job_tag_label(is_obj.job_tag)}"
                    st.markdown(f"**{is_obj.musteri_adi}** &nbsp; {baslik}")
                    mc1, mc2, mc3 = st.columns(3)
                    mc1.metric("👥 Personel", is_obj.personel_sayisi, label_visibility="visible")
                    mc2.metric("📅 Ziyaret", is_obj.ziyaret_sayisi)
                    mc3.metric("💹 Net Kâr", f"{is_obj.net_kar:,.0f} ₺")
                    st.caption(
                        f"Gelir: {is_obj.toplam_musteri_geliri:,.0f} ₺  ·  "
                        f"Personel: {personel_listesi_ozet(is_obj.personeller)}  ·  "
                        f"Maliyet: {is_obj.toplam_personel_maliyeti:,.0f} ₺"
                    )
                    if st.button("🗑️ Sepetten Sil", key=f"del_draft_{i}", width="stretch"):
                        st.session_state.draft_jobs.pop(i)
                        st.rerun()

# TAB 2: TAKVİM
with tabs[1]:
    _cal_start = __import__("time").perf_counter()
    cc, cd = st.columns([2,1])
    ms = f"{sm:02d}.{sy}"
    
    month_jobs = [j for j in jobs_list if j['date'] and ms in j['date']]
    sub_meta = build_subscription_calendar_meta(jobs_list)

    with cc:
        day_map = {}
        for group in group_jobs_by_visit(month_jobs):
            j = visit_group_label(group)
            d = j['date']
            if d not in day_map:
                day_map[d] = {'jobs': {}, 'net': 0, 'toplam_kisi': 0}
            visit_net = sum(
                float(r['price_customer'] or 0) - float(r['price_worker'] or 0) for r in group
            )
            day_map[d]['net'] += visit_net
            day_map[d]['toplam_kisi'] += len(group)
            cust_display = f"{j['name']}{subscription_labels_merged(group, sub_meta)}"
            if cust_display not in day_map[d]['jobs']:
                day_map[d]['jobs'][cust_display] = {
                    'price': 0.0, 'tag': j.get('job_tag', 'one_time'), 'kisi_sayisi': 0,
                }
            day_map[d]['jobs'][cust_display]['price'] += visit_customer_revenue(group)
            day_map[d]['jobs'][cust_display]['kisi_sayisi'] += len(group)
        
        cal = calendar.monthcalendar(sy, sm)
        cols = st.columns(7)
        for d in ["Pt","Sa","Ça","Pe","Cu","Ct","Pz"]: cols[list(["Pt","Sa","Ça","Pe","Cu","Ct","Pz"]).index(d)].write(f"**{d}**")
        for w in cal:
            cols = st.columns(7)
            for i, d in enumerate(w):
                with cols[i]:
                    if d!=0:
                        ds = f"{d:02d}.{ms}"
                        with st.container(border=True):
                            gun_basligi = f"{d} 👥{day_map[ds]['toplam_kisi']}" if ds in day_map else f"{d}"
                            if st.button(gun_basligi, key=f"cal_{d}", width="stretch"): st.session_state.sel_date=ds
                            if ds in day_map:
                                for name, data in list(day_map[ds]['jobs'].items())[:3]:
                                    css = job_tag_css(data['tag'])
                                    st.markdown(f'<span class="{css}">{name} ({data["price"]:.0f}) 👥{data["kisi_sayisi"]}</span>', unsafe_allow_html=True)
                                st.markdown(f'<div class="net-profit">{day_map[ds]["net"]:.0f}</div>', unsafe_allow_html=True)

    with cd:
        sd = st.session_state.sel_date
        st.markdown(f"### 📅 {sd} İşleri")
        djs = [j for j in month_jobs if j['date'] == sd]
        visit_groups = sort_visit_groups(group_jobs_by_visit(djs))

        if visit_groups:
            st.caption(f"📍 **{len(visit_groups)}** ziyaret · 👥 **{len(djs)}** personel")

        if not visit_groups:
            st.info("Bu tarihte planlanmış iş yok.")
        else:
            prev_tier = None
            for gi, group in enumerate(visit_groups):
                tier = 0 if visit_has_pro(group) else 1
                if prev_tier == 0 and tier == 1:
                    st.markdown("---")
                    st.caption("🎓 Öğrenci personelli işler")
                prev_tier = tier

                j = visit_group_label(group)
                counts, ucret, names, phones = summarize_personnel(group)
                badge = format_personnel_badge(counts, ucret)
                curr_tag = j.get('job_tag', 'one_time')
                tag_icon = job_tag_icon(curr_tag)
                sub_label = subscription_labels_merged(group, sub_meta)
                gid = j.get('group_id')
                old_date = j.get('date') or ''
                gkey = gid or j.get('id', gi)
                session_gids = {(r.get('group_id') or '').strip() for r in group if (r.get('group_id') or '').strip()}

                with st.expander(f"{tag_icon} {job_tag_label(curr_tag)} · {j['name']}{sub_label} · 👷 {badge}"):
                    if is_subscription_tag(curr_tag):
                        st.markdown("**Kota işlemleri**")
                        for sgi, (sg_gid, sg) in enumerate(split_group_by_session(group)):
                            sg_rep = visit_group_label(sg)
                            sg_lbl = subscription_label(sg_rep, sub_meta) or f" #{sgi + 1}"
                            sg_counts, _, _, _ = summarize_personnel(sg)
                            sg_badge = format_personnel_badge(sg_counts)
                            c_l, c_r = st.columns([3, 1])
                            c_l.caption(f"{sg_lbl.strip(' []') or 'Kota'} · 👷 {sg_badge}")
                            if c_r.button("🔙 Geri al", key=f"back_{sg_gid}_{gi}_{sgi}", help="Kotayı havuza al"):
                                add_to_queue(
                                    "Kotaya Geri Al",
                                    "UPDATE jobs SET date='' WHERE group_id=%s",
                                    (sg_gid,),
                                )
                                for r in sg:
                                    r['date'] = ''
                                st.rerun()
                        if len(session_gids) > 1:
                            st.caption("Birden fazla kota birleşik — düzenleme için kotayı önce havuza alın veya tek kotayı seçin.")

                    nt = st.selectbox(
                        "Etiket", list(JOB_TAG_ADD_LABELS),
                        index=job_tag_option_index(curr_tag), key=f"t_{gkey}_{gi}",
                    )
                    nv = job_tag_from_label(nt)
                    if nv != curr_tag and gid:
                        add_to_queue(
                            f"Etiket: {j['name']}",
                            "UPDATE jobs SET job_tag=%s WHERE group_id=%s AND COALESCE(date, '')=%s",
                            (nv, gid, old_date),
                        )
                        for r in group:
                            r['job_tag'] = nv
                        st.rerun()

                    st.divider()
                    new_pc = st.number_input(
                        "Müşteri Tutarı (₺)", value=float(j.get('price_customer') or 0),
                        step=50.0, key=f"pc_{gkey}_{gi}",
                    )

                    st.markdown("**Personel (tip ve sayı)**")
                    gc1, gc2 = st.columns(2)
                    g_pro_n = gc1.number_input(
                        "Profesyonel", min_value=0, max_value=20,
                        value=int(counts['pro']), key=f"gpn_{gkey}_{gi}",
                    )
                    g_stu_n = gc2.number_input(
                        "Öğrenci", min_value=0, max_value=20,
                        value=int(counts['student']), key=f"gsn_{gkey}_{gi}",
                    )
                    gc3, gc4 = st.columns(2)
                    g_pro_u = gc3.number_input(
                        "Prof. yevmiye (₺)", min_value=0.0, step=50.0,
                        value=float(ucret['pro']), key=f"gpu_{gkey}_{gi}",
                    )
                    g_stu_u = gc4.number_input(
                        "Öğrenci yevmiye (₺)", min_value=0.0, step=50.0,
                        value=float(ucret['student']), key=f"gsu_{gkey}_{gi}",
                    )

                    counts_differ = (
                        new_pc != float(j.get('price_customer') or 0)
                        or int(g_pro_n) != counts['pro']
                        or int(g_stu_n) != counts['student']
                        or float(g_pro_u) != float(ucret['pro'])
                        or float(g_stu_u) != float(ucret['student'])
                    )
                    if int(g_pro_n) + int(g_stu_n) < 1:
                        st.warning("En az 1 personel olmalı.")
                    elif counts_differ and len(session_gids) <= 1:
                        if st.button("💾 Grubu Güncelle", key=f"grp_upd_{gkey}_{gi}", type="secondary"):
                            existing = {"pro": names["pro"], "student": names["student"], "phones": phones}
                            personeller = expand_personnel_by_type(
                                g_pro_n, g_pro_u, g_stu_n, g_stu_u, existing,
                            )
                            new_rows = build_visit_db_rows(
                                gid, old_date, j['customer_id'], curr_tag, float(new_pc), personeller,
                            )
                            add_to_queue(
                                "Grup sil (yeniden oluştur)",
                                "DELETE FROM jobs WHERE group_id=%s AND COALESCE(date, '')=%s",
                                (gid, old_date),
                            )
                            add_to_queue(
                                f"Grup güncelle: {j['name']}",
                                JOB_INSERT_SQL, new_rows, is_bulk=True,
                            )
                            for r in list(group):
                                if r in jobs_list:
                                    jobs_list.remove(r)
                            for row in new_rows:
                                _gid, ds, cid, jtype, wp, cut, tag, prepaid, *_rest = row
                                jobs_list.append({
                                    'id': f"tmp_{uuid.uuid4().hex[:8]}", 'group_id': _gid, 'date': ds,
                                    'customer_id': cid, 'job_type': jtype, 'price_worker': wp,
                                    'price_customer': cut, 'job_tag': tag, 'is_prepaid': prepaid,
                                    'name': j['name'], 'is_collected': 0, 'is_worker_paid': 0,
                                    'assigned_student_id': None, 'assigned_pro_id': None,
                                })
                            st.rerun()

                    st.divider()
                    render_name_assignment(
                        group, f"adm_{gkey}_{gi}", sd, db, add_to_queue, use_expander=False,
                    )

                    new_job_note = st.text_input(
                        "İşe Özel Not", value=j.get('job_note') or '', key=f"jnote_{gkey}_{gi}",
                    )
                    if new_job_note != (j.get('job_note') or '') and gid:
                        if st.button("📝 Notu Kaydet", key=f"jnote_btn_{gkey}_{gi}"):
                            add_to_queue(
                                f"İş Notu: {j['name']}",
                                "UPDATE jobs SET job_note=%s WHERE group_id=%s AND COALESCE(date, '')=%s",
                                (new_job_note, gid, old_date),
                            )
                            for r in group:
                                r['job_note'] = new_job_note
                            st.rerun()

                    if st.button("🗑️ Ziyareti Sil", key=f"del_{gkey}_{gi}"):
                        del_action = visit_delete_action(group)
                        if del_action:
                            add_to_queue("Silme", del_action[0], del_action[1])
                        else:
                            for row in group:
                                add_to_queue("Silme", "DELETE FROM jobs WHERE id=%s", (row['id'],))
                        for r in list(group):
                            if r in jobs_list:
                                jobs_list.remove(r)
                        st.rerun()

        st.divider()
        st.markdown("### 📥 TARİH BEKLEYEN KOTALAR")
        
        unscheduled = [j for j in jobs_list if not j.get('date') and is_subscription_tag(j.get('job_tag'))]
        pkgs = {}
        for uj in unscheduled:
            pid = uj['group_id'].split('_')[0]
            if pid not in pkgs:
                pkgs[pid] = {'name': uj['name'], 'sessions': set(), 'total_quota': sub_meta.get('pkg_totals', {}).get(pid, 0)}
            pkgs[pid]['sessions'].add(uj['group_id'])
        
        if not pkgs:
            st.caption("Şu an havuzda bekleyen kota yok.")
        else:
            for pid, pdata in pkgs.items():
                rem = len(pdata['sessions'])
                tot = pdata['total_quota']
                with st.container(border=True):
                    st.markdown(f"<div class='quota-box'><b><span style='color:black;'>{pdata['name']}</span></b><br><span style='color:black;'>Kalan Hak: <b>{rem}/{tot}</b></span></div>", unsafe_allow_html=True)
                    if st.button(f"📌 {sd} Tarihine Ata", key=f"ass_{pid}", width="stretch"):
                        def _kota_sira(gid):
                            parca = gid.split('_')
                            return int(parca[1]) if len(parca) > 1 and parca[1].isdigit() else 0
                        sess_to_assign = sorted(pdata['sessions'], key=_kota_sira)[0]
                        add_to_queue(f"Tarih Atama: {pdata['name']}", "UPDATE jobs SET date=%s WHERE group_id=%s", (sd, sess_to_assign))
                        for job_mem in jobs_list:
                            if job_mem.get('group_id') == sess_to_assign:
                                job_mem['date'] = sd
                        st.rerun()

        st.divider()
        st.markdown("### 📝 Gün Notu & Ekstra Gider")
        existing_note = next((n.get('note') for n in db.get('notes', []) if n.get('date') == sd), None)
        day_exps = [e for e in db.get('expenses', []) if e.get('date') == sd]

        if existing_note:
            st.caption(f"📝 {existing_note}")
        if day_exps:
            for e in day_exps:
                st.caption(f"💸 {e.get('description','')}: {float(e.get('amount') or 0):,.0f} ₺")

        with st.expander("➕ Bu güne not / gider ekle"):
            new_note = st.text_input("Not (mevcut nota eklenir)", key=f"note_input_{sd}")
            if st.button("Notu Kaydet", key=f"note_btn_{sd}"):
                if new_note:
                    birlesik = f"{existing_note}\n{new_note}".strip() if existing_note else new_note
                    add_to_queue("Gün Notu Ekle", """INSERT INTO daily_notes (date, note) VALUES (%s, %s)
                        ON CONFLICT (date) DO UPDATE SET note = EXCLUDED.note""", (sd, birlesik))
                    st.rerun()
            exp_desc = st.text_input("Gider Açıklaması", key=f"exp_desc_{sd}")
            exp_amt = st.number_input("Gider Tutarı (₺)", 0.0, step=50.0, key=f"exp_amt_{sd}")
            if st.button("Gideri Kaydet", key=f"exp_btn_{sd}"):
                if exp_desc and exp_amt > 0:
                    add_to_queue("Gün Gideri Ekle", "INSERT INTO expenses (date, description, amount) VALUES (%s, %s, %s)", (sd, exp_desc, exp_amt))
                    st.rerun()

    perf_log("admin.py:tab_calendar", "tab_calendar_render", {
        "elapsed_ms": round((__import__("time").perf_counter() - _cal_start) * 1000, 2),
        "month_jobs_count": len(month_jobs),
    }, "E")

# TAB 3: FİNANS
with tabs[2]:
    t1,t2,t3,t4 = st.tabs(["Alacak","Borç","Maaş","Giderler"])
    with t1:
        ls = [j for j in jobs_list if j['is_collected']==0 and j['price_customer'] > 0]
        for i, l in enumerate(ls[:30]):
            c1,c2,c3 = st.columns([1,2,1])
            c1.write(l['date'] or "[Kota]")
            c2.write(l['name'])
            if c3.button(f"Al: {l['price_customer']}", key=f"col_{l['id']}_{i}"):
                add_to_queue(f"Tahsilat: {l['name']}", "UPDATE jobs SET is_collected=1 WHERE id=%s", (l['id'],))
                st.rerun()
    with t2:
        ls = [j for j in jobs_list if j['is_worker_paid']==0 and j['price_worker'] > 0]
        for i, l in enumerate(ls[:30]):
            c1,c2,c3 = st.columns([1,2,1])
            c1.write(l['date'] or "[Kota]")
            aname = "Atanmadı"
            if l['assigned_student_id']: aname = next((s['name'] for s in db.get('students',[]) if s['id']==l['assigned_student_id']), "Öğrenci")
            elif l['assigned_pro_id']: aname = next((p['name'] for p in db.get('pros',[]) if p['id']==l['assigned_pro_id']), "Pro")
            c2.write(f"{aname} ({l['name']})")
            if c3.button(f"Öde: {l['price_worker']}", key=f"pay_{l['id']}_{i}"):
                add_to_queue(f"Ödeme", "UPDATE jobs SET is_worker_paid=1 WHERE id=%s", (l['id'],))
                st.rerun()
    with t3:
        mk = f"{sm:02d}-{sy}"
        month_str_puantaj = f"{sm:02d}.{sy}"
        
        st.info("💡 Maaş ödemelerinde: Taban Maaşa ek olarak puantajdaki her '✅' için +1850 ₺ eklenir ve 'Avans' girişleri otomatik kesinti olarak yansır. ❌ işaretleri cezaya sebep olmaz.")
        
        for i, p in enumerate(db.get('pros', [])):
            is_paid = any([s for s in sal_list if s['pro_id']==p['id'] and mk in s['month_year']])
            present_days = sum(1 for a in db.get('attendance', []) if str(a['person_id']) == str(p['id']) and a['person_type'] == 'pro' and a['status'] == 'present' and month_str_puantaj in a['date'])
            advances = sum(float(a.get('amount') or 0) for a in db.get('attendance', []) if str(a['person_id']) == str(p['id']) and a['person_type'] == 'pro' and month_str_puantaj in a['date'])
            
            present_earnings = present_days * 1850
            total_cuts = advances
            calculated_salary = p['salary'] + present_earnings - total_cuts
            
            if p['salary'] == 0 and calculated_salary < 0: calculated_salary = 0
            
            c1, c2, c3 = st.columns([2, 2, 1])
            info_parts = []
            if present_days > 0: info_parts.append(f"<span style='color:green;'>+{present_earnings:,.0f} ₺ ({present_days} ✅)</span>")
            if advances > 0: info_parts.append(f"<span style='color:red;'>-{advances:,.0f} ₺ Avans</span>")
            info_html = f"<br><span style='font-size:12px;'>{' | '.join(info_parts)}</span>" if info_parts else ""

            if (present_days > 0 or total_cuts > 0):
                c1.markdown(f"**{p['name']}** <span style='font-size:12px; color:gray;'>(Taban: {p['salary']} ₺)</span>{info_html}", unsafe_allow_html=True)
            else:
                c1.markdown(f"<div style='margin-top: 8px;'>**{p['name']}** <span style='font-size:12px; color:gray;'>(Taban: {p['salary']} ₺)</span></div>", unsafe_allow_html=True)
            
            if is_paid:
                paid_amount = next((s['amount'] for s in sal_list if s['pro_id']==p['id'] and s['month_year'] and mk in s['month_year']), calculated_salary)
                c2.markdown(f"<div style='margin-top: 8px;'><b>{paid_amount:,.0f} ₺</b></div>", unsafe_allow_html=True)
                c3.success("Ödendi")
            else:
                final_pay = c2.number_input("Ödenecek Net Tutar (₺)", value=float(calculated_salary), step=100.0, key=f"edit_sal_{p['id']}_{i}", label_visibility="collapsed")
                if c3.button("Öde", key=f"sal_{p['id']}_{i}"):
                    add_to_queue(f"Maaş: {p['name']}", "INSERT INTO salary_payments (pro_id,amount,payment_date,month_year,payment_type) VALUES (%s,%s,%s,%s,'monthly')", (p['id'], final_pay, f"04.{mk}", mk))
                    st.rerun()

    with t4:
        arama = ay_arama(sm, sy)
        ay_giderleri = [e for e in db.get('expenses', []) if e.get('date') and arama in e['date']]
        ay_giderleri.sort(key=lambda e: e.get('date', ''))
        toplam_gunluk = sum(float(e.get('amount') or 0) for e in ay_giderleri)
        st.info(f"Bu ay takvimden girilen günlük giderler toplamı: **{toplam_gunluk:,.0f} ₺** (sidebar ve analiz hesaplarına otomatik dahil edilir)")
        if not ay_giderleri:
            st.caption("Bu ay için kayıtlı günlük gider yok. Takvim sekmesinden gün bazlı gider ekleyebilirsiniz.")
        else:
            for i, e in enumerate(ay_giderleri):
                gc1, gc2, gc3 = st.columns([1, 3, 1])
                gc1.write(e.get('date', ''))
                gc2.write(e.get('description') or '-')
                gc3.write(f"{float(e.get('amount') or 0):,.0f} ₺")

# TAB 4: KİŞİLER
with tabs[3]:
    tt = st.selectbox("Tip", ["Müşteri","Öğrenci","Profesyonel"])
    with st.form("add_p"):
        nn = st.text_input("Ad"); pp = st.text_input("Tel"); sal = st.number_input("Maaş (Yalnız Pro)",0.0)
        if st.form_submit_button("Ekle"):
            if tt=="Müşteri": add_to_queue("Müşteri Ekle", "INSERT INTO customers (name,phone) VALUES (%s,%s)",(nn,pp))
            elif tt=="Öğrenci": add_to_queue("Öğrenci Ekle", "INSERT INTO students (name,phone) VALUES (%s,%s)",(nn,pp))
            else: add_to_queue("Pro Ekle", "INSERT INTO professionals (name,phone,salary) VALUES (%s,%s,%s)",(nn,pp,sal))
            st.rerun()

# TAB 5: İŞ ÖZETLERİ
with tabs[4]:
    st.markdown("### 📋 İş Özetleri")
    st.caption("Geçmiş ziyaretler müşteri, dönem ve iş tipine göre filtrelenebilir.")

    custs_ozet = sorted(db.get("customers", []), key=lambda c: (c.get("name") or "").casefold())
    cust_opts = {"— Tüm müşteriler —": None}
    cust_opts.update({c["name"]: c["id"] for c in custs_ozet if c.get("name")})

    f1, f2, f3 = st.columns([2, 2, 1])
    with f1:
        sec_musteri = st.selectbox("Müşteri", list(cust_opts.keys()), key="ozet_musteri")
        sec_cid = cust_opts[sec_musteri]
    with f2:
        donem = st.selectbox(
            "Dönem",
            ["Sidebar ayı", "Son 30 gün", "Son 3 ay", "Tüm geçmiş"],
            key="ozet_donem",
        )
    with f3:
        tip_filt = st.selectbox("İş tipi", ["Tümü"] + list(JOB_TAG_ADD_LABELS), key="ozet_tip")

    today = date.today()
    date_from, date_to = None, None
    if donem == "Sidebar ayı":
        date_from = date(sy, sm, 1)
        date_to = date(sy, sm, calendar.monthrange(sy, sm)[1])
    elif donem == "Son 30 gün":
        date_from = today - timedelta(days=30)
        date_to = today
    elif donem == "Son 3 ay":
        date_from = today - timedelta(days=90)
        date_to = today

    tag_filt = None
    if tip_filt != "Tümü":
        tag_filt = job_tag_from_label(tip_filt)

    sub_meta_ozet = build_subscription_calendar_meta(jobs_list)
    pros_ozet = db.get("pros", [])
    students_ozet = db.get("students", [])

    summaries = build_visit_summaries(
        jobs_list,
        customer_id=sec_cid,
        date_from=date_from,
        date_to=date_to,
        job_tag=tag_filt,
        meta=sub_meta_ozet,
        pros=pros_ozet,
        students=students_ozet,
    )
    agg = aggregate_visit_summaries(summaries)

    st.divider()
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Ziyaret", f"{agg['visits']} adet")
    m2.metric("Toplam kişi-gün", f"{agg['kisi']} kişi")
    m3.metric("Ciro", f"{agg['ciro']:,.0f} ₺")
    m4.metric("Maliyet", f"{agg['maliyet']:,.0f} ₺")
    m5.metric("Net kâr", f"{agg['kar']:,.0f} ₺")

    if sec_cid is None and summaries:
        st.markdown("#### 🏆 Müşteri sıralaması (net kâra göre)")
        ranking = customer_ranking_from_summaries(summaries)[:15]
        rank_df = pd.DataFrame([
            {
                "Müşteri": r["name"],
                "Ziyaret": r["visits"],
                "Kişi": r["kisi"],
                "Ciro (₺)": r["ciro"],
                "Maliyet (₺)": r["maliyet"],
                "Net kâr (₺)": r["kar"],
            }
            for r in ranking
        ])
        st.dataframe(
            rank_df,
            width="stretch",
            hide_index=True,
            column_config={
                "Ciro (₺)": st.column_config.NumberColumn(format="%.0f"),
                "Maliyet (₺)": st.column_config.NumberColumn(format="%.0f"),
                "Net kâr (₺)": st.column_config.NumberColumn(format="%.0f"),
            },
        )
        st.caption("Bir müşteriyi seçerek yalnızca o müşterinin geçmiş ziyaretlerini görebilirsiniz.")

    if not summaries:
        st.info("Seçilen filtrelere uygun tamamlanmış ziyaret yok.")
    else:
        st.markdown("#### 📅 Ziyaret geçmişi")
        by_month = {}
        for s in summaries:
            d = s["_sort_date"]
            mk = d.strftime("%Y-%m") if d != date.min else "—"
            by_month.setdefault(mk, []).append(s)

        for mk in sorted(by_month.keys(), reverse=True):
            month_visits = by_month[mk]
            try:
                y, mo = mk.split("-")
                month_title = f"{calendar.month_name[int(mo)]} {y}"
            except ValueError:
                month_title = mk
            month_kar = sum(v["kar"] for v in month_visits)
            with st.expander(
                f"📆 {month_title} · {len(month_visits)} ziyaret · 💹 {month_kar:,.0f} ₺",
                expanded=(sec_cid is not None and len(by_month) <= 3),
            ):
                for s in month_visits:
                    tag_ico = job_tag_icon(s["job_tag"])
                    tahsil_txt = ""
                    if s["tahsil"] is True:
                        tahsil_txt = " · ✅ Tahsil"
                    elif s["tahsil"] is False:
                        tahsil_txt = " · ⏳ Alacak"
                    kar_renk = "#2e7d32" if s["kar"] >= 0 else "#c62828"
                    musteri_etik = f"**{s['customer']}** · " if sec_cid is None else ""
                    st.markdown(
                        f"{tag_ico} {musteri_etik}**{s['date']}**{s['sub_label']} · "
                        f"{s['tag_label']}{tahsil_txt}"
                    )
                    det1, det2, det3 = st.columns(3)
                    det1.markdown(f"👷 **{s['kisi']} kişi** — {s['kadro_badge']}")
                    det2.markdown(f"🧑‍💼 **Kadro:** {s['kadro_isimleri']}")
                    det3.markdown(
                        f"💰 Ciro **{s['ciro']:,.0f} ₺** · Maliyet **{s['maliyet']:,.0f} ₺** · "
                        f"<span style='color:{kar_renk};font-weight:600'>Kâr {s['kar']:,.0f} ₺</span>",
                        unsafe_allow_html=True,
                    )
                    st.divider()

# TAB 6: RAPOR & ANALİZ
with tabs[5]:
    st.markdown(f"### 📈 {calendar.month_name[sm]} {sy} - Aylık Kapsamlı Analiz")
    st.caption("Parametreleri düzenleyebilirsiniz; girdiğiniz değerler ay boyunca korunur. Günlük giderler (takvimden girilen) otomatik hesaplanır.")

    month_key = f"{sm:02d}.{sy}"
    otomatik = hesapla_analiz_otomatik(db, jobs_list, trans_list, sm, sy)
    analiz_param_init(month_key, otomatik)

    maas_key = f"analiz_maas_{month_key}"
    saha_key = f"analiz_saha_{month_key}"
    diger_key = f"analiz_diger_{month_key}"

    month_jobs_analysis = otomatik['month_jobs']
    visit_groups_analysis = group_jobs_by_visit(month_jobs_analysis)
    total_job_count = len(visit_groups_analysis)
    ogrenci_is_sayisi = sum(1 for g in visit_groups_analysis if not visit_has_pro(g))
    pro_is_sayisi = total_job_count - ogrenci_is_sayisi

    ciro_jobs = sum(visit_customer_revenue(g) for g in visit_groups_analysis)
    ciro_trans = sum(float(t['amount'] or 0) for t in trans_list if t.get('date') and t.get('type') == 'income' and ay_arama(sm, sy) in t['date'])
    total_ciro = ciro_jobs + ciro_trans

    gunluk_giderler = otomatik['gunluk_giderler']

    st.divider()

    ac1, ac2 = st.columns([3, 1])
    with ac2:
        if st.button("🔄 Otomatik Değerlere Sıfırla", key=f"analiz_reset_{month_key}", width="stretch"):
            st.session_state[maas_key] = 0.0
            st.session_state[saha_key] = otomatik['saha']
            st.session_state[diger_key] = 0.0
            st.rerun()

    st.markdown("#### ⚙️ Analiz Parametreleri (düzenlenebilir)")
    col1, col2, col3 = st.columns(3)
    with col1:
        st.caption("Varsayılan: 0 ₺")
        sabit_maas_input = st.number_input("👔 Maaşlı Eleman Maliyeti (₺)", min_value=0.0, step=1000.0, key=maas_key,
            help="Profesyonellerin sabit maaşı + puantaj. Varsayılan 0; isterseniz düzenleyin.")
    with col2:
        st.caption(f"Sistem önerisi: {otomatik['saha']:,.0f} ₺")
        saha_maliyet_input = st.number_input("👷 Saha/Günlük İşçi Maliyeti (₺)", min_value=0.0, step=500.0, key=saha_key,
            help="Vardiyalara atanan personel ücretleri toplamı. Ay değişince otomatik dolar.")
    with col3:
        st.caption("Varsayılan: 0 ₺")
        diger_gider_input = st.number_input("🏦 Diğer Giderler (₺)", min_value=0.0, step=500.0, key=diger_key,
            help="Diğer masraflar. Varsayılan 0; isterseniz düzenleyin.")

    st.markdown("#### 📋 Otomatik Giderler (sistemden, her zaman dahil)")
    og1, og2 = st.columns(2)
    og1.metric("🧾 Günlük Giderler (Takvim)", f"{gunluk_giderler:,.0f} ₺",
               help="Takvim sekmesinden girilen kira, yakıt, malzeme vb. giderler. Otomatik güncellenir.")

    toplam_gider = sabit_maas_input + saha_maliyet_input + diger_gider_input + gunluk_giderler
    og2.metric("📉 Toplam Gider (hesaplanan)", f"{toplam_gider:,.0f} ₺",
               f"Günlük: {gunluk_giderler:,.0f} ₺ dahil")

    toplam_kar = total_ciro - toplam_gider
    verimlilik = (toplam_kar / total_ciro * 100) if total_ciro > 0 else 0

    ort_ciro = total_ciro / total_job_count if total_job_count > 0 else 0
    ort_maliyet = toplam_gider / total_job_count if total_job_count > 0 else 0
    ort_kar = toplam_kar / total_job_count if total_job_count > 0 else 0

    st.divider()

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("💰 Toplam Ciro", f"{total_ciro:,.0f} ₺")
    c2.metric("📉 Toplam Gider", f"{toplam_gider:,.0f} ₺")
    c3.metric("💹 Net Kâr", f"{toplam_kar:,.0f} ₺", f"{verimlilik:.1f}% Kâr Marjı")
    c4.metric("📋 Tamamlanan Ziyaret", f"{total_job_count} Adet")
    c5.metric("🎓 Öğrenci Ziyaretleri", f"{ogrenci_is_sayisi} Adet", f"👔 Pro: {pro_is_sayisi}")

    st.markdown("#### 🥧 Ciro nereden geliyor?")
    st.caption("Dış halkada her müşteri bir dilim; dilimler etiketine (otel, anahtar teslim, tek sefer, abonelik) göre gruplanır.")
    render_ciro_pie(
        [
            {
                "job_tag": visit_group_label(g).get("job_tag"),
                "customer": visit_group_label(g).get("name"),
                "ciro": visit_customer_revenue(g),
            }
            for g in visit_groups_analysis
        ],
        title=f"{calendar.month_name[sm]} {sy} ciro dağılımı",
    )

    ay_notlari = sorted(
        [n for n in db.get('notes', []) if n.get('date') and ay_arama(sm, sy) in n['date']],
        key=lambda n: n.get('date', ''),
    )
    if ay_notlari:
        st.markdown("#### 📝 Ay Notları (Takvimden)")
        for n in ay_notlari:
            st.markdown(f"- **{n.get('date', '—')}:** {n.get('note') or '—'}")

    st.markdown("#### 📐 Birim (Ziyaret Başına) Kârlılık Analizi")
    c1, c2, c3 = st.columns(3)
    c1.metric("Ortalama Ciro / Ziyaret", f"{ort_ciro:,.0f} ₺")
    c2.metric("Ortalama Maliyet / Ziyaret", f"{ort_maliyet:,.0f} ₺")
    c3.metric("Ortalama Kâr / Ziyaret", f"{ort_kar:,.0f} ₺")

    st.divider()

    borc = hesapla_abonelik_yukumluluk(jobs_list, sm, sy)
    st.markdown(f"#### ⏳ Abonelik Yükümlülükleri — {borc['sonraki_ay']}")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("📅 Sonraki Ay Planlı Kota", f"{borc['sonraki_ay_kota']} Adet")
    c2.metric("💸 Sonraki Ay Personel Maliyeti", f"{borc['sonraki_ay_maliyet']:,.0f} ₺")
    c3.metric("📥 Havuzda Bekleyen Kota", f"{borc['havuz_kota']} Adet")
    c4.metric("💸 Havuz Tahmini Maliyet", f"{borc['havuz_maliyet']:,.0f} ₺")
    st.caption(
        "Sonraki ay: seçili aydan bir sonraki takvim ayına tarihlenmiş abonelik kotaları. "
        "Havuz: henüz tarihi atanmamış kotalar."
    )

# ==========================================
# TAB 7: PUANTAJ VE MÜSAİTLİK YÖNETİMİ
# ==========================================
with tabs[6]:
    _puan_start = __import__("time").perf_counter()
    t_yoklama, t_avans, t_musaıtlık = st.tabs(["📋 Yoklama Tablosu", "💸 Günlük Avans (₺)", "📅 Personel Müsaitlik Girişi"])
    
    pros = sorted(db.get('pros', []), key=lambda x: x['name'])
    students = sorted(db.get('students', []), key=lambda x: x['name'])
    num_days = calendar.monthrange(sy, sm)[1]
    day_cols = [str(d) for d in range(1, num_days + 1)]
    month_str = f"{sm:02d}.{sy}"
    
    with t_yoklama:
        if pros:
            df_status_data = [{"Personel_ID": p['id'], "Personel": p['name']} for p in pros]
            df_status = pd.DataFrame(df_status_data)
            for d in day_cols: df_status[d] = ""
            for att in db.get('attendance', []):
                if att['person_type'] == 'pro' and month_str in att['date']:
                    try: d = str(int(att['date'].split('.')[0]))
                    except: continue
                    icon = "✅" if att['status']=='present' else "❌" if att['status']=='absent' else "⚠️" if att['status']=='excused' else ""
                    if d in df_status.columns:
                        idx = df_status.index[df_status['Personel_ID'] == att['person_id']].tolist()
                        if idx: df_status.at[idx[0], d] = icon
            
            col_config = {"Personel_ID": None, "Personel": st.column_config.TextColumn("Personel", disabled=True)}
            for d in day_cols: col_config[d] = st.column_config.SelectboxColumn(d, options=["", "✅", "❌", "⚠️"], width="small")
            
            edited_df = st.data_editor(df_status, column_config=col_config, hide_index=True, width="stretch", key="ed_status")
            if st.button("💾 Yoklamaları Kaydet"):
                for idx, row in edited_df.iterrows():
                    pid = row['Personel_ID']
                    for d in day_cols:
                        if df_status.at[idx, d] != row[d]:
                            date_str = f"{int(d):02d}.{month_str}"
                            new_stat = 'present' if row[d]=="✅" else 'absent' if row[d]=="❌" else 'excused' if row[d]=="⚠️" else 'pending'
                            add_to_queue("Yoklama Güncelle", "DELETE FROM daily_attendance WHERE person_id=%s AND person_type='pro' AND date=%s", (pid, date_str))
                            if new_stat != 'pending':
                                add_to_queue("Yoklama Yaz", "INSERT INTO daily_attendance (person_id, person_type, date, status) VALUES (%s, 'pro', %s, %s)", (pid, date_str, new_stat))
                st.rerun()

    with t_avans:
        if pros:
            df_avans_data = [{"Personel_ID": p['id'], "Personel": p['name']} for p in pros]
            df_avans = pd.DataFrame(df_avans_data)
            for d in day_cols: df_avans[d] = 0.0
            for att in db.get('attendance', []):
                if att['person_type'] == 'pro' and month_str in att['date'] and float(att.get('amount') or 0) > 0:
                    try: d = str(int(att['date'].split('.')[0]))
                    except: continue
                    if d in df_avans.columns:
                        idx = df_avans.index[df_avans['Personel_ID'] == att['person_id']].tolist()
                        if idx: df_avans.at[idx[0], d] = float(att['amount'])
            
            col_config_av = {"Personel_ID": None, "Personel": st.column_config.TextColumn("Personel", disabled=True)}
            for d in day_cols: col_config_av[d] = st.column_config.NumberColumn(d, format="%d ₺", width="small")
            
            edited_av = st.data_editor(df_avans, column_config=col_config_av, hide_index=True, width="stretch", key="ed_av")
            if st.button("💾 Avansları Kaydet"):
                for idx, row in edited_av.iterrows():
                    pid = row['Personel_ID']
                    for d in day_cols:
                        if float(df_avans.at[idx, d]) != float(row[d]):
                            date_str = f"{int(d):02d}.{month_str}"
                            add_to_queue("Avans Sil", "DELETE FROM daily_attendance WHERE person_id=%s AND person_type='pro' AND date=%s", (pid, date_str))
                            if float(row[d]) > 0:
                                add_to_queue("Avans Yaz", "INSERT INTO daily_attendance (person_id, person_type, date, amount, status) VALUES (%s, 'pro', %s, %s, 'present')", (pid, date_str, float(row[d])))
                st.rerun()

    with t_musaıtlık:
        all_staff = [{'id': p['id'], 'name': p['name'], 'type': 'pro', 'label': f"👔 {p['name']}"} for p in pros] + \
                    [{'id': s['id'], 'name': s['name'], 'type': 'student', 'label': f"🎓 {s['name']}"} for s in students]
        
        if not all_staff:
            st.info("Kayıtlı personel bulunmuyor.")
        else:
            df_avail_data = [{"ID": x['id'], "Tip": x['type'], "Personel": x['label']} for x in all_staff]
            df_avail = pd.DataFrame(df_avail_data)
            for d in day_cols: df_avail[d] = ""
            
            for av in db.get('availability', []):
                if month_str in av['date']:
                    try: d = str(int(av['date'].split('.')[0]))
                    except: continue
                    icon = "✅ Müsait" if av['status'] == 'available' else "❌ Meşgul" if av['status'] == 'busy' else ""
                    if d in df_avail.columns:
                        idx = df_avail.index[(df_avail['ID'] == av['person_id']) & (df_avail['Tip'] == av['person_type'])].tolist()
                        if idx: df_avail.at[idx[0], d] = icon
            
            col_config_av = {
                "ID": None, "Tip": None, 
                "Personel": st.column_config.TextColumn("Personel Takvimi", disabled=True)
            }
            for d in day_cols: col_config_av[d] = st.column_config.SelectboxColumn(d, options=["", "✅ Müsait", "❌ Meşgul"], width="small")
            
            edited_avail = st.data_editor(df_avail, column_config=col_config_av, hide_index=True, width="stretch", key="ed_avail_grid")
            
            if st.button("💾 Müsaitlik Durumlarını Kaydet"):
                for idx, row in edited_avail.iterrows():
                    pid = row['ID']
                    ptype = row['Tip']
                    pname = row['Personel']
                    for d in day_cols:
                        if df_avail.at[idx, d] != row[d]:
                            date_str = f"{int(d):02d}.{month_str}"
                            new_val = 'available' if row[d] == "✅ Müsait" else 'busy' if row[d] == "❌ Meşgul" else 'pending'
                            
                            add_to_queue("Müsaitlik Temizle", "DELETE FROM personnel_availability WHERE person_id=%s AND person_type=%s AND date=%s", (pid, ptype, date_str))
                            if new_val != 'pending':
                                add_to_queue(f"{pname} Müsaitlik", "INSERT INTO personnel_availability (person_id, person_type, date, status) VALUES (%s, %s, %s, %s)", (pid, ptype, date_str, new_val))
                st.rerun()

    perf_log("admin.py:tab_puantaj", "tab_puantaj_render", {
        "elapsed_ms": round((__import__("time").perf_counter() - _puan_start) * 1000, 2),
        "pro_count": len(pros),
        "student_count": len(students),
    }, "C")

# ==========================================
# TAB 8: 🤖 AI ASİSTAN (doğal dille tam iş girişi + sorgulama)
with tabs[7]:
    _ai_start = __import__("time").perf_counter()
    if "ai_chat" not in st.session_state:
        st.session_state.ai_chat = []

    st.markdown("### 🤖 Asistan")
    st.caption(
        "Ne istediğinizi yazın; asistan panelin kendi giriş mantığıyla işi kurar ve kuyruğa atar. "
        "Hiçbir şey siz onaylamadan veritabanına yazılmaz."
    )
    st.caption(f"Geçerli tarife → {fiyat_ozeti()}")
    with st.expander("💡 Neler yapabilir?"):
        st.markdown(
            "**Tarifeyle iş girişi:** *'Yarına Emir beye 2 profesyonel gidecek, tek seferlik.'* → "
            f"müşteriye {2 * FIYAT['pro_musteri']:,.0f} ₺, personele {FIYAT['pro_yevmiye']:,.0f} ₺ yevmiye "
            "otomatik yazılır; Emir bey kayıtlı değilse önce eşleşenler sorulur, yoksa profil açılır.\n\n"
            "**Erteleme:** *'Nazlı hanımın rezervasyonunu haftaya perşembeye ertele.'* → tek seferlikse "
            "yalnızca tarih değişir, abonelikse sonraki kotalar da kaydırılıp düzen yeniden kurulur.\n\n"
            "**Tutarı siz söylerseniz:** *'İşe toplam 9600 yaz, kişi başı 2500 yevmiye.'*\n\n"
            "**İş girişi (formun tamamı):** *'Ahmet Yılmaz'a yarın 2 profesyonel 1 öğrenci gitsin, "
            "pro yevmiyesi 1500 öğrenci 800, müşteriden 6000 alacağız, otel işi.'*\n\n"
            "**Çok günlü iş:** *'Hilton'a 12, 15 ve 18 eylül günleri 3 pro gitsin, günlük 5000.'*\n\n"
            "**Abonelik:** *'Ayşe hanıma 4 kotalı abonelik aç, ilk ödeme 8000, yevmiye 1200.'*\n\n"
            "**Kota işlemleri:** *'Ayşe'nin bekleyen kotasından birini cumaya koy.'* · "
            "*'2 kota daha ekle.'* · *'Havuzdan 1 kota sil.'*\n\n"
            "**İsimle personel atama:** *'Yarın Hilton'a Ali ve Veli gitsin.'*\n\n"
            "**Düzeltme:** *'Mehmet'in bugünkü işini cumartesiye taşı.'* · *'Fiyatı 7500 yap.'* · "
            "*'Etiketini anahtar teslim yap.'* · *'Tahsil edildi işaretle.'*\n\n"
            "**Soru sorma:** *'Yarın kimler var?'* · *'Bu ay ne kadar kâr ettik?'* · "
            "*'Hilton'la geçmişte neler yaptık?'* · *'Bekleyen kotalar kimde?'*\n\n"
            "**Kayıt açma:** *'Yeni müşteri: Deniz Apartmanı, telefonu 0532...'*"
        )

    if st.session_state.pending_actions:
        with st.expander(f"⏳ Kuyrukta {len(st.session_state.pending_actions)} işlem", expanded=True):
            for _a in st.session_state.pending_actions[-20:]:
                st.caption(f"• {_a['desc']}")
            if st.button("💾 Kuyruğu kaydet", type="primary", key="ai_commit_btn"):
                with st.spinner("Sunucuya yazılıyor..."):
                    commit_queue()

    if "GEMINI_API_KEY" not in st.secrets:
        st.error("⚠️ Streamlit Secrets ayarlarına GEMINI_API_KEY ekleyin.")
    else:
        import google.generativeai as genai
        _model_start = __import__("time").perf_counter()
        genai.configure(api_key=st.secrets["GEMINI_API_KEY"])

        _bugun_dt = datetime.now()
        _mus_adlari = [c.get('name') for c in db.get('customers', []) if c.get('name')]
        _pro_adlari = [p.get('name') for p in db.get('pros', []) if p.get('name')]
        _ogr_adlari = [s.get('name') for s in db.get('students', []) if s.get('name')]
        _srv_adlari = [s.get('name') for s in db.get('service_personnel', []) if s.get('name')]

        _yakin = []
        for _i in range(8):
            _g = (_bugun_dt + timedelta(days=_i)).strftime("%d.%m.%Y")
            _adlar = sorted({j.get('name') or '—' for j in jobs_list if (j.get('date') or '') == _g})
            if _adlar:
                _yakin.append(f"{_g}: " + ", ".join(_adlar))

        _havuz = {}
        for _j in jobs_list:
            if _j.get('job_tag') == 'subscription' and not (_j.get('date') or '').strip():
                _havuz.setdefault(_j.get('name') or '—', set()).add(_j.get('group_id') or str(_j.get('id')))

        system_instruction = f"""
Sen bir temizlik/vardiya firmasının panelinde çalışan operasyon asistanısın. Kullanıcı Türkçe yazar,
sen de Türkçe cevap verirsin. Görevin: niyeti anlamak, doğru aracı çağırmak ve ne yaptığını net söylemek.

BUGÜN: {_bugun_dt.strftime('%d.%m.%Y')} ({_bugun_dt.strftime('%A')}). Tüm tarihler GG.AA.YYYY.
"yarın", "cumaya", "3 temmuz" gibi ifadeleri araçlara olduğu gibi verebilirsin; araçlar çözer.

KAYITLI MÜŞTERİLER ({len(_mus_adlari)}): {", ".join(_mus_adlari) or "—"}
PROFESYONELLER: {", ".join(_pro_adlari) or "—"}
ÖĞRENCİLER: {", ".join(_ogr_adlari) or "—"}
SERVİS PERSONELİ: {", ".join(_srv_adlari) or "—"}
ÖNÜMÜZDEKİ 7 GÜN:
{chr(10).join(_yakin) or "planlı iş yok"}
HAVUZDA BEKLEYEN KOTALAR: {", ".join(f"{k} ({len(v)})" for k, v in _havuz.items()) or "yok"}

FİYAT TARİFESİ (kullanıcı tutar söylemezse bunlar uygulanır, sen hesap yapma; araç hesaplar):
- Tek seferlik işte müşteriden KİŞİ BAŞINA: profesyonel {FIYAT['pro_musteri']:,.0f} ₺, öğrenci {FIYAT['ogrenci_musteri']:,.0f} ₺.
  Örnek: 2 profesyonel → {2 * FIYAT['pro_musteri']:,.0f} ₺.
- Personele ödenen sabit yevmiye: profesyonel {FIYAT['pro_yevmiye']:,.0f} ₺, öğrenci {FIYAT['ogrenci_yevmiye']:,.0f} ₺.
- Abonelikte varsayılan {FIYAT['abonelik_kota']} kota açılır. ABONELİK PAKET ÜCRETİ TARİFEDEN
  HESAPLANMAZ: kullanıcı tutarı söylemediyse "bu abonelik için toplam ne kadar yazayım?" diye SOR.
  Verilen tutarın tamamı ilk kotaya (ilk güne) yazılır; sonraki kotalar tüketilirken müşteriden
  gelir yazılmaz, yalnızca personel yevmiyesi gider olarak işlenir.
- Kullanıcı işe toplam bir ücret söylerse ("toplam 9600") onu musteri_tutari olarak ver; yevmiyeler
  yine tarifeden gelir. Yevmiyeyi de söylerse onu kullan.

İŞ MODELİ:
- Bir "iş" = bir müşteriye bir günde yapılan ziyaret. O ziyarete kaç kişi gidiyorsa veritabanında
  o kadar satır olur; müşteriden alınan tutar yalnızca ilk satıra yazılır (araç otomatik yapar).
- Personel iki tiptir: "pro" (profesyonel) ve "ogrenci". Aynı ziyarette karışık kadro olabilir
  (örn. 2 pro + 1 öğrenci); tip ve sayı işe göre değişir.
- Etiketler: "tek seferlik", "otel", "anahtar teslim", "abonelik". Ay sonu ciro analizi bu etiketlere göre yapılır.
- Abonelikte tarih verilmez: "kota" ziyaret hakkıdır, kotalar havuzda bekler ve sonradan güne yerleştirilir
  (ai_kota_yerlestir). Peşin ödeme ilk kotaya yazılır. Her kota 1 personeli temsil eder.
- Fiyat modu: "gunluk" tutar her ziyarette alınır, "toplam" tüm iş için bir kez alınır.

YENİ İŞ AKIŞI (sırayla uygula):
1. Müşteri adını ai_musteri_bul ile kontrol et. Kayıtlıysa devam et. Benzer adaylar dönerse
   kullanıcıya hangisi olduğunu sor, kendin seçme. Hiç yoksa "yeni müşteri açayım mı?" diye sor;
   onay gelirse (ya da kullanıcı "yeni müşteri" dediyse) ai_musteri_ekle ile profili aç — bu araç
   anında kaydeder, hemen ardından işi girebilirsin.
2. İşin türünü belirle: kadro profesyonel mi öğrenci mi, kaç kişi, etiket ne (tek seferlik / otel /
   anahtar teslim / abonelik).
3. ai_is_ekle'yi JSON ile çağır: pro_sayisi / ogrenci_sayisi (veya personeller listesi), tarihler
   ya da abonelikte kota, varsa musteri_tutari, etiket, isimler, not. Tutar ve yevmiye verilmezse
   tarife otomatik uygulanır.
Örnek: "yarına emir beye 2 profesyonel gidecek tek seferlik" → ai_musteri_bul("emir bey"), gerekirse
ai_musteri_ekle, sonra ai_is_ekle ile {{"musteri": "...", "etiket": "tek seferlik",
"tarihler": ["yarın"], "pro_sayisi": 2}} → tutar {2 * FIYAT['pro_musteri']:,.0f} ₺, yevmiye {FIYAT['pro_yevmiye']:,.0f} ₺ olarak yazılır.

ARAÇ KULLANIMI:
- YENİ İŞ her zaman ai_is_ekle ile ve JSON şemasıyla girilir. Kullanıcının verdiği tüm detayları
  (tarihler, kadro tipleri ve yevmiyeleri, tutar, fiyat modu, etiket, atanacak isimler, not) JSON'a koy.
- Birden fazla farklı iş varsa ai_is_ekle'yi her iş için ayrı ayrı çağır.
- ERTELEME/TAŞIMA her zaman ai_is_tasi ile yapılır; araç rezervasyon türünü kendisi bulur.
  Tek seferlikte sadece tarih değişir; abonelikte sonraki kotalar da kaydırılıp düzen yeniden kurulur.
  Kullanıcı hangi günden taşınacağını söylemezse eski_tarih'i boş bırak, araç en yakın planlı günü alır.
- Var olan işi değiştirmek için: ai_is_tasi (tarih), ai_fiyat_guncelle (tutar), ai_etiket_degistir (etiket),
  ai_kisi_ekle / ai_kisi_sil (kişi sayısı), ai_personel_ata (isimle atama), ai_tahsilat_isaretle (tahsilat),
  ai_is_iptal (o günün işini tamamen sil). Bunlar için ASLA yeni iş ekleme.
- Kota işlemleri: ai_kota_ekle, ai_kota_sil (havuzdakinden), ai_kota_yerlestir (havuzdakini güne koy).
- Kayıt açma: ai_musteri_ekle, ai_personel_ekle. Yeni müşteriye iş girilebilmesi için önce kuyruğun
  kaydedilmesi gerektiğini kullanıcıya söyle.
- SORU sorulduğunda yazma aracı çağırma; ai_gun_ozeti, ai_musteri_ozeti, ai_ay_ozeti, ai_bekleyen_kotalar,
  ai_liste araçlarıyla veriye bak ve cevapla.

DAVRANIŞ KURALLARI:
1. Müşteri adı listede yoksa uydurma; en yakın adayları sun ve hangisi olduğunu sor.
2. Eksik bilgi işi bozacaksa (örn. tarih yok, kadro yok) tek bir net soru sor. Bozmayacaksa varsayılanı
   kullan ve ne varsaydığını söyle: yevmiye 0, tutar 0, kişi sayısı 1, etiket "tek seferlik", fiyat modu "gunluk".
3. Aynı işlemi iki kez çağırma. Araç "Hata:" ile dönerse kullanıcıya sebebini açıkla, sessizce başka araç deneme.
4. Cevabın sonunda ne kuyruğa atıldığını tek satırda özetle ve onay için 'Kuyruğu kaydet' demesi gerektiğini hatırlat.
5. Önceki mesajları dikkate al: "hayır 3 kişi olsun", "onu da cumaya koy" gibi düzeltmeleri bağlamdan çöz.
"""

        try:
            model = genai.GenerativeModel(
                model_name=st.secrets.get("GEMINI_MODEL", "gemini-2.5-flash"),
                tools=AI_ARACLARI,
                system_instruction=system_instruction,
            )
            perf_log("admin.py:tab_ai", "ai_model_init", {
                "elapsed_ms": round((__import__("time").perf_counter() - _model_start) * 1000, 2),
            }, "D")

            if st.session_state.ai_chat:
                if st.button("🧹 Sohbeti temizle", key="ai_clear_btn"):
                    st.session_state.ai_chat = []
                    st.rerun()

            for _m in st.session_state.ai_chat:
                with st.chat_message("user" if _m["role"] == "user" else "assistant"):
                    st.markdown(_m["text"])

            _soru = st.chat_input("Örn: yarına Emir beye 2 profesyonel gidecek, tek seferlik")
            if _soru:
                st.session_state.ai_chat.append({"role": "user", "text": _soru})
                _gecmis = [
                    {"role": "user" if m["role"] == "user" else "model", "parts": [m["text"]]}
                    for m in st.session_state.ai_chat[:-1]
                ][-12:]
                with st.spinner("Asistan çalışıyor..."):
                    try:
                        _chat = model.start_chat(
                            history=_gecmis, enable_automatic_function_calling=True,
                        )
                        _resp = _chat.send_message(_soru)
                        _cevap = (getattr(_resp, "text", "") or "").strip() or "İşlem tamamlandı."
                    except Exception as e:
                        _cevap = f"⚠️ Asistan hatası: {e}"
                st.session_state.ai_chat.append({"role": "assistant", "text": _cevap})
                st.rerun()
        except Exception as e:
            st.error(f"AI başlatma hatası: {e}")

    perf_log("admin.py:tab_ai", "tab_ai_render_total", {
        "elapsed_ms": round((__import__("time").perf_counter() - _ai_start) * 1000, 2),
    }, "D")
