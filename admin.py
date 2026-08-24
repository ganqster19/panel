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
    db_diagnostics,
)

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
            gid = f"{pkg_id}_{i}" if self.job_tag == 'subscription' else pkg_id

            if self.job_tag == 'subscription':
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
    .job-subs { background-color: #fff3e0; border: 1px solid #ffcc80; color: #e65100; padding: 1px 4px; border-radius: 4px; font-size: 10px; display: block; margin-bottom: 2px; font-weight: 700; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .job-once { background-color: #e3f2fd; border: 1px solid #90caf9; color: #1565c0; padding: 1px 4px; border-radius: 4px; font-size: 10px; display: block; margin-bottom: 2px; font-weight: 700; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
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

def ai_is_ekle(musteri_adi: str, prof_sayisi: int, personel_yevmiyesi: float, musteri_tutari: float, abonelik_mi: bool, is_tarihi: str, atanacak_personeller: list[str]) -> str:
    """Yapay zekanın çıkardığı verilerle sisteme yeni iş (vardiya) ekler.
    Abonelikte, o gün için alınan ilk ödeme peşin sayılır (is_prepaid=1 olarak işaretlenir)."""
    cid = None
    for c in st.session_state.db_data.get('customers', []):
        if c['name'].lower() == musteri_adi.lower():
            cid = c['id']
            break
            
    if not cid: return f"Hata: '{musteri_adi}' isimli bir müşteri bulunamadı."

    pkg_id = str(uuid.uuid4())[:8]
    tag = 'subscription' if abonelik_mi else 'one_time'
    
    pro_ids = []
    for p_name in atanacak_personeller:
        for p in st.session_state.db_data.get('pros', []):
            if p_name.lower() in p['name'].lower():
                pro_ids.append(p['id'])
                break
                
    gercek_kisi_sayisi = int(max(prof_sayisi, len(pro_ids), 1))
    
    for i in range(gercek_kisi_sayisi):
        cut = float(musteri_tutari) if i == 0 else 0.0 
        prepaid = 1 if (i == 0 and cut > 0) else 0
        assigned_id = pro_ids[i] if i < len(pro_ids) else None
        query = "INSERT INTO jobs (group_id, date, customer_id, job_type, price_worker, price_customer, job_tag, assigned_pro_id, is_prepaid) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
        params = (pkg_id, is_tarihi, cid, 'pro', float(personel_yevmiyesi), cut, tag, assigned_id, prepaid)
        add_to_queue(f"🤖 AI Ekleme: {musteri_adi}", query, params)
        
    return f"Başarılı! {musteri_adi} için {is_tarihi} tarihine {gercek_kisi_sayisi} personellik iş oluşturuldu."

def ai_is_tasi(musteri_adi: str, eski_tarih: str, yeni_tarih: str) -> str:
    """Bir müşterinin eski tarihteki işlerini/aboneliğini yeni tarihe taşır."""
    cid = None
    for c in st.session_state.db_data.get('customers', []):
        if musteri_adi.lower() in c['name'].lower():
            cid = c['id']
            break
            
    if not cid: return f"Hata: '{musteri_adi}' isimli bir müşteri bulunamadı."
    
    query = "UPDATE jobs SET date = %s WHERE customer_id = %s AND date = %s"
    params = (yeni_tarih, cid, eski_tarih)
    add_to_queue(f"🤖 AI Taşıma: {musteri_adi}", query, params)
    return f"Başarılı! {musteri_adi} müşterisinin {eski_tarih} tarihindeki işleri {yeni_tarih} tarihine taşındı."

def ai_is_iptal(musteri_adi: str, tarih: str) -> str:
    """Bir müşterinin belirtilen tarihteki tüm işlerini sistemden tamamen siler."""
    cid = None
    for c in st.session_state.db_data.get('customers', []):
        if musteri_adi.lower() in c['name'].lower():
            cid = c['id']
            break
            
    if not cid: return f"Hata: '{musteri_adi}' isimli müşteri bulunamadı."
    
    query = "DELETE FROM jobs WHERE customer_id = %s AND date = %s"
    params = (cid, tarih)
    add_to_queue(f"🤖 AI İptal: {musteri_adi}", query, params)
    return f"Başarılı! {musteri_adi} müşterisinin {tarih} tarihindeki kayıtları iptal edildi ve silindi."

def _musteri_bul(musteri_adi: str):
    for c in st.session_state.db_data.get('customers', []):
        if musteri_adi.lower() in c['name'].lower():
            return c['id']
    return None

def _personel_bul(personel_adi: str):
    for p in st.session_state.db_data.get('pros', []):
        if personel_adi.lower() in p['name'].lower():
            return p
    return None

def ai_kota_ekle(musteri_adi: str, eklenecek_kota: int, personel_yevmiyesi: float = 0.0) -> str:
    """Bir müşterinin mevcut aboneliğine, tarihsiz (havuzda bekleyen) yeni kota/hak ekler.
    Her kota 1 personeli (1 iş satırını) temsil eder ve müşteriden ayrıca ücret alınmaz (is_prepaid=0),
    çünkü bu sistem sadece iş takibi amaçlıdır. Kullanıcı 'X'e 2 kota daha ekle' derse bu araç kullanılır."""
    cid = _musteri_bul(musteri_adi)
    if not cid: return f"Hata: '{musteri_adi}' isimli müşteri bulunamadı."

    mevcut_pids = [j['group_id'].split('_')[0] for j in st.session_state.db_data.get('jobs', [])
                   if j.get('customer_id') == cid and j.get('job_tag') == 'subscription' and j.get('group_id')]
    if not mevcut_pids:
        return f"Hata: {musteri_adi} için mevcut bir abonelik paketi bulunamadı. Önce ai_is_ekle ile abonelik=True olarak iş oluşturun."
    pid = mevcut_pids[0]

    mevcut_seans_no = [int(j['group_id'].split('_')[1]) for j in st.session_state.db_data.get('jobs', [])
                        if j.get('group_id', '').startswith(f"{pid}_") and j['group_id'].split('_')[1].isdigit()]
    baslangic = (max(mevcut_seans_no) + 1) if mevcut_seans_no else 0

    for i in range(int(eklenecek_kota)):
        gid = f"{pid}_{baslangic + i}"
        query = "INSERT INTO jobs (group_id, date, customer_id, job_type, price_worker, price_customer, job_tag, is_prepaid) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
        params = (gid, '', cid, 'pro', float(personel_yevmiyesi), 0.0, 'subscription', 0)
        add_to_queue(f"🤖 Kota Ekle: {musteri_adi}", query, params)

    return f"Başarılı! {musteri_adi} aboneliğine {eklenecek_kota} yeni kota eklendi (havuzda bekliyor, tarih atanmadı)."

def ai_kota_sil(musteri_adi: str, silinecek_kota: int) -> str:
    """Bir müşterinin henüz tarihe atanmamış (havuzda bekleyen) abonelik kotalarından belirtilen sayıda siler.
    Zaten bir tarihe atanmış/işlenmiş kotalara dokunmaz."""
    cid = _musteri_bul(musteri_adi)
    if not cid: return f"Hata: '{musteri_adi}' isimli müşteri bulunamadı."

    bekleyenler = [j for j in st.session_state.db_data.get('jobs', [])
                   if j.get('customer_id') == cid and j.get('job_tag') == 'subscription' and not j.get('date')]
    if not bekleyenler:
        return f"Hata: {musteri_adi} için havuzda bekleyen (tarihsiz) kota bulunamadı."

    silinecekler = bekleyenler[:int(silinecek_kota)]
    for j in silinecekler:
        add_to_queue(f"🤖 Kota Sil: {musteri_adi}", "DELETE FROM jobs WHERE id=%s", (j['id'],))

    return f"Başarılı! {musteri_adi} müşterisinin havuzundan {len(silinecekler)} kota silindi."

def ai_kisi_ekle(musteri_adi: str, tarih: str, personel_tipi: str, yevmiye: float = 0.0) -> str:
    """Belirli bir tarihte zaten planlanmış bir işe, ek personel (kişi sayısını artırmak için) ekler.
    personel_tipi 'ogrenci' veya 'pro' olmalı. Müşteriden ek ücret alınmaz (fiyat zaten ilk kayıtta alınmıştır, is_prepaid=0)."""
    cid = _musteri_bul(musteri_adi)
    if not cid: return f"Hata: '{musteri_adi}' isimli müşteri bulunamadı."

    mevcut = [j for j in st.session_state.db_data.get('jobs', []) if j.get('customer_id') == cid and j.get('date') == tarih]
    if not mevcut:
        return f"Hata: {musteri_adi} için {tarih} tarihinde mevcut bir iş bulunamadı. Yeni iş için ai_is_ekle kullanın."

    gid = mevcut[0].get('group_id') or str(uuid.uuid4())[:8]
    tag = mevcut[0].get('job_tag', 'one_time')
    jt = 'student' if personel_tipi.lower().startswith('ogr') or personel_tipi.lower().startswith('öğr') else 'pro'

    query = "INSERT INTO jobs (group_id, date, customer_id, job_type, price_worker, price_customer, job_tag, is_prepaid) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
    params = (gid, tarih, cid, jt, float(yevmiye), 0.0, tag, 0)
    add_to_queue(f"🤖 Kişi Ekle: {musteri_adi}", query, params)
    return f"Başarılı! {musteri_adi} için {tarih} tarihindeki işe 1 kişi daha eklendi."

def ai_kisi_sil(musteri_adi: str, tarih: str, adet: int = 1) -> str:
    """Belirli bir tarihte planlanmış bir işten, kişi sayısını azaltmak için belirtilen sayıda personel kaydını siler.
    Mümkünse henüz atanmamış (boş) kayıtlar öncelikli silinir."""
    cid = _musteri_bul(musteri_adi)
    if not cid: return f"Hata: '{musteri_adi}' isimli müşteri bulunamadı."

    mevcut = [j for j in st.session_state.db_data.get('jobs', []) if j.get('customer_id') == cid and j.get('date') == tarih]
    if not mevcut:
        return f"Hata: {musteri_adi} için {tarih} tarihinde iş bulunamadı."

    mevcut.sort(key=lambda j: 0 if (not j.get('assigned_student_id') and not j.get('assigned_pro_id')) else 1)
    silinecekler = mevcut[:int(adet)]
    for j in silinecekler:
        add_to_queue(f"🤖 Kişi Sil: {musteri_adi}", "DELETE FROM jobs WHERE id=%s", (j['id'],))

    return f"Başarılı! {musteri_adi} için {tarih} tarihindeki işten {len(silinecekler)} kişi kaldırıldı."

def ai_gider_ekle(tarih: str, aciklama: str, tutar: float) -> str:
    """Belirli bir güne, kira/malzeme/yakıt gibi ekstra bir gider kaydı ekler ('expenses' tablosu)."""
    query = "INSERT INTO expenses (date, description, amount) VALUES (%s, %s, %s)"
    params = (tarih, aciklama, float(tutar))
    add_to_queue(f"🤖 Gider Ekle: {aciklama}", query, params)
    return f"Başarılı! {tarih} tarihine '{aciklama}' açıklamasıyla {tutar} ₺ gider eklendi."

def ai_not_ekle(tarih: str, not_metni: str) -> str:
    """Belirli bir güne serbest metin not ekler. 'daily_notes' tablosunda tarih başına tek satır tutulduğu için,
    o tarihte zaten bir not varsa yenisi altına eklenir (üzerine yazılmaz)."""
    mevcut_not = next((n.get('note') or '' for n in st.session_state.db_data.get('notes', []) if n.get('date') == tarih), '')
    birlesik_not = f"{mevcut_not}\n{not_metni}".strip() if mevcut_not else not_metni

    query = """INSERT INTO daily_notes (date, note) VALUES (%s, %s)
               ON CONFLICT (date) DO UPDATE SET note = EXCLUDED.note"""
    params = (tarih, birlesik_not)
    add_to_queue(f"🤖 Not Ekle: {tarih}", query, params)
    return f"Başarılı! {tarih} tarihine not eklendi: '{not_metni}'"

def ai_maas_ode(personel_adi: str, tutar: float, tarih: str = None) -> str:
    """Aylık maaşla çalışan bir profesyonele maaş ödemesi kaydeder ('salary_payments' tablosu)."""
    pro = _personel_bul(personel_adi)
    if not pro: return f"Hata: '{personel_adi}' isimli personel bulunamadı."

    if not tarih: tarih = datetime.now().strftime("%d.%m.%Y")
    mk = f"{tarih.split('.')[1]}-{tarih.split('.')[2]}"
    query = "INSERT INTO salary_payments (pro_id,amount,payment_date,month_year,payment_type) VALUES (%s,%s,%s,%s,'monthly')"
    params = (pro['id'], float(tutar), tarih, mk)
    add_to_queue(f"🤖 Maaş Öde: {personel_adi}", query, params)
    return f"Başarılı! {personel_adi} isimli personele {tutar} ₺ maaş ödemesi kaydedildi."

def ai_gunluk_ucret_ode(personel_adi: str, tarih: str, tutar: float) -> str:
    """Günlük (yevmiyeli) çalışan bir personele, belirli bir gün için yapılan ödemeyi kaydeder.
    Ayrı bir günlük ücret tablosu olmadığı için genel 'transactions' kayıt defterine
    type='expense', category='gunluk_ucret' olarak işlenir.
    Aylık maaşlı personel için bu aracı DEĞİL, ai_maas_ode aracını kullan."""
    pro = _personel_bul(personel_adi)
    if not pro: return f"Hata: '{personel_adi}' isimli personel bulunamadı."

    query = "INSERT INTO transactions (date, type, category, amount, description, related_id) VALUES (%s, %s, %s, %s, %s, %s)"
    params = (tarih, 'expense', 'gunluk_ucret', float(tutar), f"{personel_adi} - günlük ücret", pro['id'])
    add_to_queue(f"🤖 Günlük Ücret: {personel_adi}", query, params)
    return f"Başarılı! {personel_adi} isimli personele {tarih} tarihi için {tutar} ₺ günlük ücret ödemesi kaydedildi."
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
        jt = st.radio("Tip", ["Tek Seferlik (Tarihli)", "Abonelik (Tarihsiz Kota)"], horizontal=True, key="ib_tip")

        if jt == "Tek Seferlik (Tarihli)":
            dc1, dc2 = st.columns(2)
            d1 = dc1.date_input("Başlangıç", datetime.now(), key="ib_d1")
            d2 = dc2.date_input("Bitiş", datetime.now(), key="ib_d2")
            days = st.multiselect("Günler", ["Pazartesi","Salı","Çarşamba","Perşembe","Cuma","Cumartesi","Pazar"], default=["Pazartesi"], key="ib_days")
            kota = 0
        else:
            st.info("Aboneliklerde tarih seçilmez. Kota, ziyaret sayısını belirtir; takvimden istenilen günlere dağıtılır.")
            kota = st.number_input("Kota (Toplam Ziyaret Sayısı)", min_value=1, value=4, step=1, key="ib_kota")
            d1, d2, days = None, None, []

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

        if st.button("✅ Sepete Ekle", type="primary", use_container_width=True):
            if sc == "-":
                st.warning("Lütfen bir müşteri seçin.")
            elif personel_sayisi < 1:
                st.warning("Lütfen en az 1 personel girin (tip ve sayı).")
            else:
                personeller = expand_personnel_by_type(ib_pro_n, ib_pro_u, ib_stu_n, ib_stu_u)
                if jt == "Tek Seferlik (Tarihli)":
                    dates = []
                    tr = ["Pazartesi","Salı","Çarşamba","Perşembe","Cuma","Cumartesi","Pazar"]
                    curr = d1
                    while curr <= d2:
                        if tr[curr.weekday()] in days: dates.append(curr)
                        curr += timedelta(1)
                    tag = 'one_time'
                else:
                    dates = [None] * int(kota)
                    tag = 'subscription'

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
            if st.button("💾 KUYRUĞA EKLE (KAYDETMEK İÇİN)", type="primary", use_container_width=True):
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
                    baslik = "🔄 Abonelik" if is_obj.job_tag == 'subscription' else "🔹 Tek Seferlik"
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
                    if st.button("🗑️ Sepetten Sil", key=f"del_draft_{i}", use_container_width=True):
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
                            if st.button(gun_basligi, key=f"cal_{d}", use_container_width=True): st.session_state.sel_date=ds
                            if ds in day_map:
                                for name, data in list(day_map[ds]['jobs'].items())[:3]:
                                    css = "job-subs" if data['tag']=='subscription' else "job-once"
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
                tag_icon = "🔄" if curr_tag == 'subscription' else "🔹"
                sub_label = subscription_labels_merged(group, sub_meta)
                gid = j.get('group_id')
                old_date = j.get('date') or ''
                gkey = gid or j.get('id', gi)
                session_gids = {(r.get('group_id') or '').strip() for r in group if (r.get('group_id') or '').strip()}

                with st.expander(f"{tag_icon} {j['name']}{sub_label} · 👷 {badge}"):
                    if curr_tag == 'subscription':
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
                        "Etiket", ["Tek Sefer", "Abonelik"],
                        index=0 if curr_tag == 'one_time' else 1, key=f"t_{gkey}_{gi}",
                    )
                    nv = 'subscription' if nt == "Abonelik" else 'one_time'
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
                    st.markdown("**Personel atamaları**")
                    for ri, row in enumerate(group):
                        ico = "🎓" if row['job_type'] == 'student' else "👔"
                        slot_name = row.get('staff_name') or f"{ico} #{ri + 1}"
                        aname = "❓"
                        if row.get('assigned_student_id'):
                            found = [s['name'] for s in db.get('students', []) if s['id'] == row['assigned_student_id']]
                            if found:
                                aname = found[0]
                        elif row.get('assigned_pro_id'):
                            found = [p['name'] for p in db.get('pros', []) if p['id'] == row['assigned_pro_id']]
                            if found:
                                aname = found[0]
                        st.caption(f"**{slot_name}** → Atanan: {aname}")

                        with st.popover(f"🎯 Ata: {slot_name}"):
                            ready_ids = [
                                int(a['person_id']) for a in db.get('availability', [])
                                if a['date'] == sd and a['status'] == 'available'
                            ]
                            if row['job_type'] == 'student':
                                musait = [s for s in db.get('students', []) if s['id'] in ready_ids] or db.get('students', [])
                                sl = {s['name']: s['id'] for s in musait}
                                sel = st.selectbox("Öğrenci", list(sl.keys()), key=f"s_{row['id']}_{gi}_{ri}")
                                if st.button("Ata", key=f"ba_{row['id']}_{gi}_{ri}"):
                                    add_to_queue(
                                        f"Atama: {sel}",
                                        "UPDATE jobs SET assigned_student_id=%s WHERE id=%s",
                                        (sl[sel], row['id']),
                                    )
                                    st.rerun()
                            else:
                                musait = [p for p in db.get('pros', []) if p['id'] in ready_ids] or db.get('pros', [])
                                pl = {p['name']: p['id'] for p in musait}
                                sel = st.selectbox("Profesyonel", list(pl.keys()), key=f"p_{row['id']}_{gi}_{ri}")
                                if st.button("Ata", key=f"bp_{row['id']}_{gi}_{ri}"):
                                    add_to_queue(
                                        f"Atama: {sel}",
                                        "UPDATE jobs SET assigned_pro_id=%s WHERE id=%s",
                                        (pl[sel], row['id']),
                                    )
                                    st.rerun()

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
        
        unscheduled = [j for j in jobs_list if not j.get('date') and j.get('job_tag') == 'subscription']
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
                    if st.button(f"📌 {sd} Tarihine Ata", key=f"ass_{pid}", use_container_width=True):
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
        tip_filt = st.selectbox("İş tipi", ["Tümü", "Tek sefer", "Abonelik"], key="ozet_tip")

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
    if tip_filt == "Tek sefer":
        tag_filt = "one_time"
    elif tip_filt == "Abonelik":
        tag_filt = "subscription"

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
            use_container_width=True,
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
                    tag_ico = "🔄" if s["job_tag"] == "subscription" else "🔹"
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
        if st.button("🔄 Otomatik Değerlere Sıfırla", key=f"analiz_reset_{month_key}", use_container_width=True):
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
            
            edited_df = st.data_editor(df_status, column_config=col_config, hide_index=True, use_container_width=True, key="ed_status")
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
            
            edited_av = st.data_editor(df_avans, column_config=col_config_av, hide_index=True, use_container_width=True, key="ed_av")
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
            
            edited_avail = st.data_editor(df_avail, column_config=col_config_av, hide_index=True, use_container_width=True, key="ed_avail_grid")
            
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
# TAB 8: 🤖 YENİ - AI ASİSTAN (NLP İLE İŞ EKLEME/TAŞIMA/SİLME)
with tabs[7]:
    _ai_start = __import__("time").perf_counter()
    st.markdown("### 🤖 Yapay Zeka Asistanı ile Hızlı Komutlar")
    st.info("💡 Asistana ne istediğinizi doğal bir cümleyle söyleyin. İş ekleyebilir, tarih taşıyabilir, iptal edebilir, kota ekleyip çıkarabilir, kişi sayısını değiştirebilir, gider/not girebilir ve maaş/yevmiye ödemesi kaydedebilir.\n\n*Örnekler:*\n- *'Ebru Baykanın 2 temmuzdaki kotasını 3 temmuza taşı.'*\n- *'Ahmet beye yarın 2 profesyonel gidecek, fiyat 2000.'*\n- *'Mehmetin bugünkü işini tamamen iptal et.'*\n- *'Ayşe hanımın aboneliğine 3 kota daha ekle.'*\n- *'Can'ın haftaya bekleyen 2 kotasını sil.'*\n- *'Bugün Mehmet'in işine 1 kişi daha ekle, yevmiyesi 800.'*\n- *'Yarınki işten 1 kişi eksilt.'*\n- *'Bugüne 500 TL yakıt gideri ekle.'*\n- *'Yarına not düş: müşteri anahtarı komşuda bırakacak.'*\n- *'Ali'ye 15000 TL maaş öde.'*\n- *'Veli'ye bugünkü yevmiyesi olan 900 TL'yi öde.'*")
    
    if "GEMINI_API_KEY" not in st.secrets:
        st.error("⚠️ Lütfen Streamlit Secrets ayarlarına GEMINI_API_KEY ekleyin.")
    else:
        import google.generativeai as genai
        _model_start = __import__("time").perf_counter()
        genai.configure(api_key=st.secrets["GEMINI_API_KEY"])
        bugunun_tarihi_str = datetime.now().strftime("%d.%m.%Y")
        
        # Yapay Zekaya verilecek kimlik/görev ve çalışma mantığı
        system_instruction = f"""
        Sen bir Temizlik/Vardiya firmasının gelişmiş ERP ve Vardiya Yönetim Asistanısın.
        Görevin, kullanıcının girdiği metindeki niyeti anlamak ve uygun aracı çalıştırmaktır.
        
        Bugünün tarihi: {bugunun_tarihi_str}. Kullanıcı 'yarın', 'bugün', 'haftaya' gibi terimler kullanırsa tarihi buna göre DD.MM.YYYY formatına çevir.

        İŞ MODELİ:
        - Abonelik işlerinde, ilk kota bir tarihe atanırken müşteriden peşin ücret alınır (price_customer ilk satıra yazılır).
          Kalan kotalar tarihsiz olarak havuzda bekler; ilerleyen günlerde ihtiyaç oldukça tarihe atanır, gerekirse yeni kota eklenir
          ya da kullanılmayan kota iptal edilir. Bu sistem SADECE iş takibi amaçlıdır, ekstra ödeme almaz/kesmez.
        - Her abonelik kotası tam olarak 1 personeli (1 iş satırını) temsil eder. Bir işe birden fazla kişi gidiyorsa,
          bu birden fazla kota/satır demektir.
        - Personelin bazılarının maaşı AYLIK (sabit + puantaj), bazılarınınki ise GÜNLÜK (yevmiye, her gün ayrı ödenir) verilir.
          Aylık maaşlı biri için ai_maas_ode, günlük yevmiyeli biri için ai_gunluk_ucret_ode kullanılır. Hangi tip olduğu belirsizse
          kullanıcıya varsayılan olarak günlük ödeme mantığıyla (ai_gunluk_ucret_ode) yaklaş, aksi açıkça belirtilmedikçe.
        - Bir işteki kişi sayısı zamanla değişebilir: iş sahaya çıktıktan sonra ek kişi gerekirse ai_kisi_ekle,
          kişi azaltılacaksa ai_kisi_sil kullanılır (bunlar tarihe zaten atanmış tekil işler içindir, kota havuzu değildir).
        - Güne özel ekstra giderler (kira, malzeme, yakıt vb.) için ai_gider_ekle, güne özel serbest notlar için ai_not_ekle kullanılır.

        ARAÇ SEÇİM KURALLARI:
        1. "Yeni iş / yeni kayıt" → ai_is_ekle
           - Bir işe N personel gidecekse veritabanına N ayrı satır eklenir; müşteri toplam tutarı sadece ilk satıra yazılır (araç otomatik yapar).
           - Belirtilmeyen bilgiler için varsayılanlar: Abonelik=False, Yevmiye=0, Fiyat=0.
        2. "Taşıma / erteleme / değiştirme (tarih)" → ai_is_tasi. KESİNLİKLE yeni iş ekleme, sadece tarihi güncelle.
        3. "İptal / tamamen silme (bir tarihteki tüm iş)" → ai_is_iptal
        4. "Aboneliğe kota/hak ekle" (örn: '2 kota daha ekle', 'aboneliğini uzat') → ai_kota_ekle
        5. "Abonelikten kota/hak sil" (havuzda bekleyen, henüz tarih verilmemiş) → ai_kota_sil
        6. "Var olan bir işe/tarihe ek kişi/personel gönder" (kota değil, zaten planlı bir işe ekleme) → ai_kisi_ekle
        7. "Var olan bir işten kişi/personel azalt" → ai_kisi_sil
        8. "Güne gider ekle" (kira, malzeme, genel gider vb.) → ai_gider_ekle
        9. "Güne not/hatırlatma ekle" → ai_not_ekle
        10. "Aylık maaşlı personele maaş öde" → ai_maas_ode
        11. "Günlük/yevmiyeli personele o günün ücretini öde" → ai_gunluk_ucret_ode

        ASLA aynı işlem için birden fazla araç kullanma. Niyeti net biçimde tespit et ve sadece ilgili aracı çalıştır.
        Eğer niyet belirsizse, en olası aracı seç ve kullanıcıya ne yaptığını net biçimde özetle.
        """
        
        try:
            model = genai.GenerativeModel(
                model_name='gemini-2.5-flash', # veya gemini-1.5-pro
                tools=[
                    ai_is_ekle, ai_is_tasi, ai_is_iptal,
                    ai_kota_ekle, ai_kota_sil,
                    ai_kisi_ekle, ai_kisi_sil,
                    ai_gider_ekle, ai_not_ekle,
                    ai_maas_ode, ai_gunluk_ucret_ode,
                ],
                system_instruction=system_instruction
            )
            perf_log("admin.py:tab_ai", "ai_model_init", {
                "elapsed_ms": round((__import__("time").perf_counter() - _model_start) * 1000, 2),
            }, "D")
            
            user_input = st.text_area("Ne yapmak istiyorsunuz?", placeholder="Komutunuzu buraya yazın...")
            
            if st.button("✨ Asistana Gönder (Kuyruğa Ekle)", type="primary"):
                if user_input:
                    with st.spinner("Asistan komutunuzu analiz ediyor ve uyguluyor..."):
                        chat = model.start_chat(enable_automatic_function_calling=True)
                        response = chat.send_message(user_input)
                        st.success(response.text)
                else:
                    st.warning("Lütfen asistan için bir komut yazın.")
        except Exception as e:
            st.error(f"AI Başlatma Hatası: {e}.")

    perf_log("admin.py:tab_ai", "tab_ai_render_total", {
        "elapsed_ms": round((__import__("time").perf_counter() - _ai_start) * 1000, 2),
    }, "D")