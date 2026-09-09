"""İş kuralı testleri: tarife, kadro, abonelik ve erteleme mantığı.

Veritabanına bağlanmaz; `vardiya.islemler` fonksiyonlarını sahte veriyle çalıştırır.
Çalıştırma:  python kural_testleri.py
"""
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from vardiya import islemler as I
from vardiya.isakisi import FIYAT

BUGUN = date.today()
hata_sayisi = 0


def kontrol(baslik, kosul, ayrinti=""):
    global hata_sayisi
    if kosul:
        print(f"  [ok]   {baslik}")
    else:
        hata_sayisi += 1
        print(f"  [HATA] {baslik} → {ayrinti}")


def veri_kur():
    return {
        "customers": [
            {"id": 1, "name": "Emir Kaya", "phone": "0532"},
            {"id": 2, "name": "Nazlı Demir", "phone": ""},
            {"id": 3, "name": "Emir Şahin", "phone": ""},
        ],
        "pros": [{"id": 10, "name": "Ali Veli", "phone": "1"}],
        "students": [{"id": 20, "name": "Ayşe Yıldız"}],
        "service_personnel": [], "jobs": [], "notes": [], "expenses": [],
    }


print("1) Tek seferlik tarife (2 profesyonel = 9.600 ₺, yevmiye 2.500 ₺)")
veri = veri_kur()
mesaj, eylemler, onizleme = I.is_ekle(veri, {
    "musteri": "Emir Kaya", "etiket": "tek seferlik", "tarihler": ["yarın"], "pro_sayisi": 2})
kontrol("2 satır üretildi", len(onizleme) == 2, len(onizleme))
kontrol("müşteri tutarı 9600", onizleme[0]["price_customer"] == 2 * FIYAT["pro_musteri"], onizleme[0])
kontrol("ikinci satıra gelir yazılmadı", onizleme[1]["price_customer"] == 0, onizleme[1])
kontrol("yevmiyeler 2500", all(r["price_worker"] == FIYAT["pro_yevmiye"] for r in onizleme), onizleme)
kontrol("tarih yarın", onizleme[0]["date"] == (BUGUN + timedelta(days=1)).strftime("%d.%m.%Y"), onizleme[0]["date"])

print("2) Öğrenci tarifesi (1 öğrenci = 2.800 ₺, yevmiye 1.800 ₺)")
mesaj, eylemler, onizleme = I.is_ekle(veri, {
    "musteri": "Nazlı", "tarihler": ["12.09.2026"], "ogrenci_sayisi": 1})
kontrol("tutar 2800", onizleme[0]["price_customer"] == FIYAT["ogrenci_musteri"], onizleme[0])
kontrol("yevmiye 1800", onizleme[0]["price_worker"] == FIYAT["ogrenci_yevmiye"], onizleme[0])

print("3) Karışık kadro + kullanıcının verdiği toplam tutar")
mesaj, eylemler, onizleme = I.is_ekle(veri, {
    "musteri": "Nazlı", "etiket": "otel", "tarihler": ["12.09.2026"],
    "pro_sayisi": 2, "ogrenci_sayisi": 1, "musteri_tutari": 12000})
kontrol("3 satır", len(onizleme) == 3, len(onizleme))
kontrol("verilen tutar korundu", onizleme[0]["price_customer"] == 12000, onizleme[0])
kontrol("maliyet 2×2500 + 1800", sum(r["price_worker"] for r in onizleme) == 6800, onizleme)
kontrol("etiket otel", onizleme[0]["job_tag"] == "hotel", onizleme[0]["job_tag"])

print("4) Abonelik: tutar sorulur, 4 kota, ücret ilk kotada, kalan kotalar yalnızca gider")
tutarsiz = I.is_ekle(veri, {"musteri": "Emir Kaya", "etiket": "abonelik", "pro_sayisi": 1})[0]
kontrol("tutar verilmeyince sorar", tutarsiz.startswith("Hata") and "paket ücreti" in tutarsiz, tutarsiz)
mesaj, eylemler, onizleme = I.is_ekle(veri, {
    "musteri": "Emir Kaya", "etiket": "abonelik", "pro_sayisi": 1, "musteri_tutari": 16000})
kontrol("varsayılan 4 kota", len(onizleme) == FIYAT["abonelik_kota"], len(onizleme))
kontrol("paket ücreti ilk kotada", onizleme[0]["price_customer"] == 16000, onizleme[0])
kontrol("diğer kotalarda gelir yok", all(r["price_customer"] == 0 for r in onizleme[1:]), onizleme[1:])
kontrol("her kotada personel gideri var", all(r["price_worker"] == FIYAT["pro_yevmiye"] for r in onizleme), onizleme)
kontrol("kotalar havuzda (tarihsiz)", all(r["date"] == "" for r in onizleme), onizleme)
kontrol("kota grup id'leri ayrı", len({r["group_id"] for r in onizleme}) == 4, onizleme)

print("5) Müşteri eşleştirme")
belirsiz = I.is_ekle(veri, {"musteri": "emir bey", "pro_sayisi": 1, "tarihler": ["yarın"]})[0]
kontrol("'emir bey' adayları getirir", "Emir Kaya" in belirsiz and "Emir Şahin" in belirsiz, belirsiz)
kontrol("'Demir' karışmadı", "Nazlı Demir" not in belirsiz, belirsiz)
kontrol("'nazlı hanım' tek eşleşir", I.musteri_bul(veri, "nazlı hanım")[0]["id"] == 2, I.musteri_bul(veri, "nazlı hanım"))
kontrol("kayıtsız müşteri reddedilir", I.is_ekle(veri, {
    "musteri": "Zeynep Ak", "pro_sayisi": 1, "tarihler": ["yarın"]})[0].startswith("Hata"))

print("6) İsimle personel atama")
mesaj, eylemler, onizleme = I.is_ekle(veri, {
    "musteri": "Emir Kaya", "tarihler": ["yarın"], "pro_sayisi": 1, "isimler": ["Ali"]})
kontrol("isim atandı", onizleme[0]["staff_name"] == "Ali Veli", onizleme[0])
kontrol("pro id yazıldı", onizleme[0]["assigned_pro_id"] == 10, onizleme[0])

print("7) Tarih yorumlama")
kontrol("'haftaya perşembe' bir hafta ileri",
        I.tarih_coz("haftaya perşembe") != I.tarih_coz("perşembe"),
        (I.tarih_coz("haftaya perşembe"), I.tarih_coz("perşembe")))
kontrol("'3 temmuz' ayrıştı", I.tarih_coz("3 temmuz").startswith("03.07."), I.tarih_coz("3 temmuz"))
kontrol("'2026-09-10' ayrıştı", I.tarih_coz("2026-09-10") == "10.09.2026", I.tarih_coz("2026-09-10"))
kontrol("'yarın' doğru", I.tarih_coz("yarın") == (BUGUN + timedelta(days=1)).strftime("%d.%m.%Y"))

print("8) Erteleme: tek seferlik yalnızca tarih değişir")
veri2 = veri_kur()
veri2["jobs"] = [
    {"id": 100, "customer_id": 2, "name": "Nazlı Demir", "date": "10.09.2026",
     "group_id": "aaa", "job_tag": "one_time", "price_customer": 4800, "price_worker": 2500},
    {"id": 200, "customer_id": 1, "name": "Emir Kaya", "date": "10.09.2026",
     "group_id": "bbb_0", "job_tag": "subscription", "price_customer": 19200, "price_worker": 2500},
    {"id": 201, "customer_id": 1, "name": "Emir Kaya", "date": "17.09.2026",
     "group_id": "bbb_1", "job_tag": "subscription", "price_customer": 0, "price_worker": 2500},
    {"id": 202, "customer_id": 1, "name": "Emir Kaya", "date": "24.09.2026",
     "group_id": "bbb_2", "job_tag": "subscription", "price_customer": 0, "price_worker": 2500},
    {"id": 203, "customer_id": 1, "name": "Emir Kaya", "date": "",
     "group_id": "bbb_3", "job_tag": "subscription", "price_customer": 0, "price_worker": 2500},
]
mesaj, eylemler = I.is_tasi(veri2, "nazlı hanım", "12.09.2026", "10.09.2026")
kontrol("tek eylem", len(eylemler) == 1, eylemler)
kontrol("tek seferlik olarak tanındı", "tek seferlik" in mesaj, mesaj)
kontrol("yeni tarih parametrede", eylemler[0][2][0] == "12.09.2026", eylemler[0][2])

print("9) Erteleme: abonelik düzeni yeniden kurulur")
mesaj, eylemler = I.is_tasi(veri2, "Emir Kaya", "12.09.2026", "10.09.2026")
kontrol("3 planlı kota kaydırıldı", len(eylemler) == 3, len(eylemler))
kontrol("hepsi +2 gün", [e[2][0] for e in eylemler] == ["26.09.2026", "19.09.2026", "12.09.2026"],
        [e[2][0] for e in eylemler])
kontrol("çakışmayı önlemek için sondan başa", eylemler[0][2][2] == "24.09.2026", eylemler[0][2])
kontrol("havuzdaki kota etkilenmedi", all(e[2][2] != "" for e in eylemler), eylemler)

print("10) Kota ve havuz işlemleri")
kontrol("kota yerleştirme", not I.kota_yerlestir(veri2, "Emir Kaya", "cuma", 1)[0].startswith("Hata"),
        I.kota_yerlestir(veri2, "Emir Kaya", "cuma", 1)[0])
kontrol("kota ekleme", not I.kota_ekle(veri2, "Emir Kaya", 2)[0].startswith("Hata"),
        I.kota_ekle(veri2, "Emir Kaya", 2)[0])
kontrol("bekleyen kota raporu", "Emir Kaya (1)" in I.bekleyen_kotalar(veri2), I.bekleyen_kotalar(veri2))
kontrol("kaydedilmemiş satır silinmez",
        I.kisi_sil({"jobs": [{"customer_id": 1, "date": "10.09.2026", "job_tag": "one_time"}],
                    "customers": veri2["customers"]}, "Emir Kaya", "10.09.2026")[0].startswith("Hata"))

print("11) Aynı güne taşıma reddedilir")
kontrol("aynı tarih hatası", I.is_tasi(veri2, "nazlı hanım", "10.09.2026", "10.09.2026")[0].startswith("Hata"),
        I.is_tasi(veri2, "nazlı hanım", "10.09.2026", "10.09.2026")[0])

print()
if hata_sayisi:
    print(f"{hata_sayisi} kontrol başarısız.")
    sys.exit(1)
print("Tüm kontroller geçti.")
