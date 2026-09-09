"""Panel dışından (API, CLI, bot) çalışan asistan.

Streamlit oturumuna ihtiyaç duymaz; veritabanına doğrudan bağlanır ve iş kurallarını
`vardiya.islemler` çekirdeğinden alır — yani panelle birebir aynı davranır.

Kullanım:
    from vardiya.asistan import Asistan
    a = Asistan()
    print(a.komut("yarına emir beye 2 profesyonel gidecek tek seferlik"))

Ortam değişkenleri (Streamlit secrets yerine):
    SUPABASE_HOST, SUPABASE_DB, SUPABASE_USER, SUPABASE_PASSWORD, SUPABASE_PORT
    GEMINI_API_KEY, GEMINI_MODEL (varsayılan gemini-2.5-flash)
"""
import os
from datetime import date, timedelta

import psycopg2
from psycopg2.extras import RealDictCursor

from vardiya import islemler as I
from vardiya.isakisi import FIYAT, fiyat_ozeti

MODEL_ADI = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")


def _gizli(anahtar, varsayilan=None):
    """Önce ortam değişkeni, yoksa Streamlit secrets (yerel geliştirme için)."""
    deger = os.environ.get(anahtar)
    if deger:
        return deger
    try:
        import streamlit as st
        s = st.secrets.get("supabase", {})
        eslesme = {
            "SUPABASE_HOST": "host", "SUPABASE_DB": "dbname", "SUPABASE_USER": "user",
            "SUPABASE_PASSWORD": "password", "SUPABASE_PORT": "port",
        }
        if anahtar in eslesme:
            return s.get(eslesme[anahtar], varsayilan)
        if anahtar == "GEMINI_API_KEY":
            return st.secrets.get("gemini", {}).get("api_key", varsayilan)
    except Exception:
        pass
    return varsayilan


def baglan():
    """Veritabanı bağlantısı (ortam değişkeni ya da secrets ile)."""
    return psycopg2.connect(
        host=_gizli("SUPABASE_HOST"),
        dbname=_gizli("SUPABASE_DB", "postgres"),
        user=_gizli("SUPABASE_USER"),
        password=_gizli("SUPABASE_PASSWORD"),
        port=int(_gizli("SUPABASE_PORT", 5432) or 5432),
        cursor_factory=RealDictCursor,
        sslmode="require",
        connect_timeout=10,
    )


def veri_yukle(conn):
    """Asistanın karar vermesi için gereken tabloları okur."""
    veri = {}
    with conn.cursor() as c:
        c.execute("""
            SELECT j.*, cu.name FROM jobs j
            JOIN customers cu ON j.customer_id = cu.id
        """)
        veri["jobs"] = c.fetchall()
        c.execute("SELECT * FROM customers ORDER BY name")
        veri["customers"] = c.fetchall()
        for anahtar, tablo in (
            ("pros", "professionals"), ("students", "students"),
            ("service_personnel", "service_personnel"), ("expenses", "expenses"),
            ("notes", "daily_notes"),
        ):
            try:
                c.execute(f"SELECT * FROM {tablo}")
                veri[anahtar] = c.fetchall()
            except Exception:
                conn.rollback()
                veri[anahtar] = []
    return veri


class Asistan:
    """İş kurallarını uygulayan başsız asistan.

    uygula=False verilirse hiçbir şey yazılmaz; yalnızca ne yapılacağı planlanır (kuru çalışma).
    """

    def __init__(self, uygula=True, api_key=None, model=MODEL_ADI, conn=None):
        self.uygula = bool(uygula)
        self.api_key = api_key or _gizli("GEMINI_API_KEY")
        self.model_adi = model
        self.conn = conn or baglan()
        self.veri = veri_yukle(self.conn)
        self.gunluk = []

    # --- altyapı ---

    def kapat(self):
        try:
            self.conn.close()
        except Exception:
            pass

    def _isle(self, eylemler):
        """Eylemleri uygula (ya da kuru çalışmada yalnızca kaydet)."""
        for aciklama, sql, params in eylemler:
            self.gunluk.append({"aciklama": aciklama, "sql": sql, "params": [str(p) for p in params]})
            if not self.uygula:
                continue
            with self.conn.cursor() as c:
                c.execute(sql, params)
        if self.uygula and eylemler:
            self.conn.commit()
            self.veri = veri_yukle(self.conn)

    def _yaz(self, sonuc):
        mesaj, eylemler = sonuc[0], sonuc[1]
        if not eylemler:
            return mesaj
        try:
            self._isle(eylemler)
        except Exception as e:
            self.conn.rollback()
            return f"Hata: işlem uygulanamadı ({e})."
        return ("Uygulandı: " if self.uygula else "Planlandı (kuru çalışma): ") + mesaj

    # --- doğrudan (AI'sız) API yüzeyi ---

    def is_ekle(self, istek):
        mesaj, eylemler, _ = I.is_ekle(self.veri, istek)
        return self._yaz((mesaj, eylemler))

    def is_tasi(self, musteri, yeni_tarih, eski_tarih=""):
        return self._yaz(I.is_tasi(self.veri, musteri, yeni_tarih, eski_tarih))

    def musteri_ekle(self, ad, telefon="", konum=""):
        mevcut, adaylar = I.musteri_bul(self.veri, ad)
        if mevcut:
            return {"id": mevcut["id"], "ad": mevcut["name"], "yeni": False, "adaylar": adaylar}
        aciklama, sql, params = I.musteri_ekle_eylemi(ad, telefon, konum)
        if not self.uygula:
            self.gunluk.append({"aciklama": aciklama, "sql": sql, "params": [str(p) for p in params]})
            return {"id": None, "ad": ad, "yeni": True, "adaylar": adaylar}
        with self.conn.cursor() as c:
            c.execute(sql, params)
            yeni_id = c.fetchone()["id"]
        self.conn.commit()
        self.veri = veri_yukle(self.conn)
        return {"id": yeni_id, "ad": ad, "yeni": True, "adaylar": adaylar}

    def gun_ozeti(self, tarih):
        return I.gun_ozeti(self.veri, tarih)

    def ay_ozeti(self, ay=0, yil=0):
        return I.ay_ozeti(self.veri, ay, yil)

    # --- AI araçları ---

    def _araclar(self):
        """Gemini'ye verilecek araç fonksiyonları (kapanış olarak bu asistana bağlı)."""

        def musteri_bul(ad: str) -> str:
            """Müşterinin kayıtlı olup olmadığını söyler, benzer adları aday olarak listeler."""
            musteri, adaylar = I.musteri_bul(self.veri, ad)
            if musteri:
                return f"Kayıtlı: {musteri['name']} ({(musteri.get('phone') or 'telefon yok')})."
            if adaylar:
                return (f"'{ad}' birebir yok. Benzerler: " + ", ".join(adaylar)
                        + ". Hangisi olduğunu kullanıcıya sor; hiçbiri değilse musteri_ekle çağır.")
            return f"'{ad}' sistemde yok. musteri_ekle ile yeni profil açılabilir."

        def musteri_ekle(ad: str, telefon: str = "", konum_url: str = "") -> str:
            """Yeni müşteri profili açar ve anında kaydeder; hemen ardından iş girilebilir."""
            sonuc = self.musteri_ekle(ad, telefon, konum_url)
            if not sonuc["yeni"]:
                return f"'{sonuc['ad']}' zaten kayıtlı, yeni profil açılmadı."
            return f"'{sonuc['ad']}' müşteri profili oluşturuldu."

        def is_ekle(is_json: str) -> str:
            """Yeni iş kurar. Tek argüman JSON metnidir.

            Şema: {"musteri": "Emir Kaya", "etiket": "tek seferlik|otel|anahtar teslim|abonelik",
            "tarihler": ["yarın"], "kota": 4, "pro_sayisi": 2, "ogrenci_sayisi": 0,
            "personeller": [{"tip": "pro", "ucret": 2500}], "musteri_tutari": 9600,
            "fiyat_modu": "gunluk|toplam", "isimler": ["Ali"], "not": "..."}
            Tek seferlik işlerde tutar verilmezse tarife uygulanır: profesyonel 4800 ₺/kişi,
            öğrenci 2800 ₺/kişi; yevmiye profesyonel 2500 ₺, öğrenci 1800 ₺.
            Abonelikte tarih verilmez, kota açılır ve tutarın tamamı ilk kotaya yazılır; abonelik
            paket ücreti zorunludur, kullanıcı söylemediyse önce sor."""
            mesaj, eylemler, _ = I.is_ekle(self.veri, is_json)
            return self._yaz((mesaj, eylemler))

        def is_tasi(musteri_adi: str, yeni_tarih: str, eski_tarih: str = "") -> str:
            """Rezervasyonu erteler. Tek seferlikte sadece tarih değişir; abonelikte ertelenen
            kotadan sonraki planlı kotalar da kaydırılarak abonelik düzeni yeniden kurulur.
            eski_tarih boşsa müşterinin en yakın planlı işi taşınır."""
            return self._yaz(I.is_tasi(self.veri, musteri_adi, yeni_tarih, eski_tarih))

        def is_iptal(musteri_adi: str, tarih: str) -> str:
            """Bir günün tüm iş kayıtlarını siler."""
            return self._yaz(I.is_iptal(self.veri, musteri_adi, tarih))

        def kota_ekle(musteri_adi: str, adet: int, personel_yevmiyesi: float = 0) -> str:
            """Mevcut aboneliğe havuzda bekleyen kota ekler."""
            return self._yaz(I.kota_ekle(self.veri, musteri_adi, adet, personel_yevmiyesi or None))

        def kota_sil(musteri_adi: str, adet: int) -> str:
            """Havuzda bekleyen tarihsiz kotalardan siler."""
            return self._yaz(I.kota_sil(self.veri, musteri_adi, adet))

        def kota_yerlestir(musteri_adi: str, tarih: str, adet: int = 1) -> str:
            """Havuzdaki kotayı belirli bir güne yerleştirir (kota tüketimi)."""
            return self._yaz(I.kota_yerlestir(self.veri, musteri_adi, tarih, adet))

        def kisi_ekle(musteri_adi: str, tarih: str, personel_tipi: str, yevmiye_tutari: float = 0) -> str:
            """Planlanmış işe ek personel ekler (tip: pro veya ogrenci)."""
            return self._yaz(I.kisi_ekle(self.veri, musteri_adi, tarih, personel_tipi, yevmiye_tutari or None))

        def kisi_sil(musteri_adi: str, tarih: str, adet: int = 1) -> str:
            """Planlanmış işten kişi çıkarır."""
            return self._yaz(I.kisi_sil(self.veri, musteri_adi, tarih, adet))

        def fiyat_guncelle(musteri_adi: str, tarih: str, musteri_tutari: float) -> str:
            """Bir günün müşteri tutarını değiştirir."""
            return self._yaz(I.fiyat_guncelle(self.veri, musteri_adi, tarih, musteri_tutari))

        def etiket_degistir(musteri_adi: str, tarih: str, etiket: str) -> str:
            """Bir günün iş etiketini değiştirir (otel, anahtar teslim, tek seferlik, abonelik)."""
            return self._yaz(I.etiket_degistir(self.veri, musteri_adi, tarih, etiket))

        def tahsilat_isaretle(musteri_adi: str, tarih: str) -> str:
            """Bir günün müşteri tahsilatını 'alındı' yapar."""
            return self._yaz(I.tahsilat_isaretle(self.veri, musteri_adi, tarih))

        def gider_ekle(tarih: str, aciklama: str, tutar: float) -> str:
            """Genel gider kaydı ekler (yakıt, malzeme vb.)."""
            return self._yaz(I.gider_ekle(tarih, aciklama, tutar))

        def not_ekle(tarih: str, not_metni: str) -> str:
            """Güne not yazar."""
            return self._yaz(I.not_ekle(self.veri, tarih, not_metni))

        def gun_ozeti(tarih: str) -> str:
            """Belirli bir günün işlerini, kadrosunu, cirosunu ve kârını özetler."""
            return I.gun_ozeti(self.veri, tarih)

        def musteri_ozeti(musteri_adi: str) -> str:
            """Müşterinin geçmiş işlerini, cirosunu, kârını ve bekleyen kotalarını özetler."""
            return I.musteri_ozeti(self.veri, musteri_adi)

        def ay_ozeti(ay: int = 0, yil: int = 0) -> str:
            """Ayın cirosunu, personel maliyetini, giderini, etiket dağılımını özetler."""
            return I.ay_ozeti(self.veri, ay, yil)

        def bekleyen_kotalar() -> str:
            """Havuzda bekleyen (tarihi girilmemiş) abonelik kotalarını listeler."""
            return I.bekleyen_kotalar(self.veri)

        def liste(tip: str = "musteri") -> str:
            """Kayıtlıları listeler (musteri, profesyonel, ogrenci, servis)."""
            return I.liste(self.veri, tip)

        return [
            musteri_bul, musteri_ekle, is_ekle, is_tasi, is_iptal,
            kota_ekle, kota_sil, kota_yerlestir, kisi_ekle, kisi_sil,
            fiyat_guncelle, etiket_degistir, tahsilat_isaretle, gider_ekle, not_ekle,
            gun_ozeti, musteri_ozeti, ay_ozeti, bekleyen_kotalar, liste,
        ]

    def sistem_talimati(self):
        bugun = date.today()
        musteriler = ", ".join((c.get("name") or "") for c in (self.veri.get("customers") or [])[:60])
        prolar = ", ".join((p.get("name") or "") for p in self.veri.get("pros") or [])
        ogrenciler = ", ".join((p.get("name") or "") for p in self.veri.get("students") or [])
        yakin = [
            f"{j.get('date')} {j.get('name')}"
            for j in self.veri.get("jobs") or []
            if (j.get("date") or "") and I._gun(j.get("date"))
            and bugun <= I._gun(j.get("date")) <= bugun + timedelta(days=10)
        ]
        return f"""Sen bir temizlik şirketinin operasyon asistanısın. Türkçe, kısa ve net konuşursun.
Bugün {bugun.strftime('%d.%m.%Y')} ({['Pazartesi','Salı','Çarşamba','Perşembe','Cuma','Cumartesi','Pazar'][bugun.weekday()]}).

FİYAT TARİFESİ ({fiyat_ozeti()}):
- Tek seferlik işte müşteriden kişi başına: profesyonel {FIYAT['pro_musteri']:,.0f} ₺, öğrenci {FIYAT['ogrenci_musteri']:,.0f} ₺.
- Personel yevmiyesi: profesyonel {FIYAT['pro_yevmiye']:,.0f} ₺, öğrenci {FIYAT['ogrenci_yevmiye']:,.0f} ₺.
- Abonelikte varsayılan {FIYAT['abonelik_kota']} kota açılır. Paket ücreti tarifeden hesaplanmaz:
  kullanıcı söylemediyse "bu abonelik için toplam ne kadar yazayım?" diye sor. Verilen tutar ilk
  kotaya yazılır; sonraki kotalar tüketilirken gelir yazılmaz, yalnızca personel gideri işlenir.
- Kullanıcı toplam tutar söylerse onu musteri_tutari olarak gönder; söylemezse tarife uygulanır.

İŞ AKIŞI:
1. musteri_bul ile müşteriyi kontrol et. Benzer adaylar dönerse hangisi olduğunu SOR, kendin seçme.
   Kayıt yoksa yeni profil açmak için onay al, sonra musteri_ekle çağır.
2. İşin türünü (profesyonel/öğrenci, kişi sayısı) ve etiketini belirle.
3. is_ekle'yi JSON ile çağır. Erteleme isteklerinde her zaman is_tasi kullan; tür ayrımını araç yapar.

BAĞLAM:
Müşteriler: {musteriler or 'yok'}
Profesyoneller: {prolar or 'yok'} | Öğrenciler: {ogrenciler or 'yok'}
Yaklaşan işler: {', '.join(yakin[:25]) or 'yok'}

KURALLAR: Tarihleri araçlara doğal dille de verebilirsin ('yarın', 'haftaya perşembe').
Eksik ve kritik bilgi varsa (müşteri kimliği, tarih, kişi sayısı) tahmin etme, sor.
Yaptığın işlemi tek cümleyle özetle; tutarları ₺ ile yaz."""

    def komut(self, metin, gecmis=None):
        """Doğal dil komutunu çalıştırır.

        Dönen: {"cevap": str, "islemler": [...], "uygulandi": bool}
        `gecmis`: [{"rol": "user"|"model", "metin": "..."}] biçiminde önceki mesajlar.
        """
        if not self.api_key:
            return {"cevap": "Hata: GEMINI_API_KEY tanımlı değil.", "islemler": [], "uygulandi": False}
        import google.generativeai as genai

        genai.configure(api_key=self.api_key)
        model = genai.GenerativeModel(
            self.model_adi,
            tools=self._araclar(),
            system_instruction=self.sistem_talimati(),
        )
        sohbet = model.start_chat(
            enable_automatic_function_calling=True,
            history=[
                {"role": "user" if m.get("rol") == "user" else "model",
                 "parts": [m.get("metin") or ""]}
                for m in (gecmis or [])
            ],
        )
        self.gunluk = []
        try:
            yanit = sohbet.send_message(str(metin or ""))
            cevap = (getattr(yanit, "text", "") or "").strip() or "İşlem tamamlandı."
        except Exception as e:
            return {"cevap": f"Hata: {e}", "islemler": self.gunluk, "uygulandi": False}
        return {"cevap": cevap, "islemler": self.gunluk, "uygulandi": self.uygula and bool(self.gunluk)}


def komut_calistir(metin, uygula=True, gecmis=None):
    """Tek seferlik kullanım: bağlan, komutu çalıştır, bağlantıyı kapat."""
    a = Asistan(uygula=uygula)
    try:
        return a.komut(metin, gecmis=gecmis)
    finally:
        a.kapat()
