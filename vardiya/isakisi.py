"""İş akışı kuralları: fiyat tablosu, kadro kurulumu, müşteri eşleştirme, abonelik düzeni.

Bu modül Streamlit'e bağlı değildir; hem admin paneli hem de API/asistan aynı kuralları
buradan kullanır. Fiyatlar tek yerden değişsin diye FIYAT sözlüğünde toplanmıştır.
"""
from datetime import datetime, date, timedelta
import difflib

FIYAT = {
    # Tek seferlik işte müşteriden kişi başına alınan ücret
    "pro_musteri": 4800.0,
    "ogrenci_musteri": 2800.0,
    # Personele ödenen sabit yevmiye
    "pro_yevmiye": 2500.0,
    "ogrenci_yevmiye": 1800.0,
    # Abonelik varsayılanları: 4 kota, paket ücreti kota × ziyaret ücreti
    "abonelik_kota": 4,
    "abonelik_katsayi": 1.0,
}

TIPLER = ("pro", "student")


def tip_coz(deger) -> str:
    """'öğrenci', 'ogrenci', 'student', 'öğr' → student; diğer her şey pro."""
    s = str(deger or "").strip().casefold()
    return "student" if s.startswith(("ogr", "öğr", "stu")) else "pro"


def yevmiye(tip) -> float:
    """Personele ödenen sabit günlük ücret."""
    return FIYAT["ogrenci_yevmiye"] if tip_coz(tip) == "student" else FIYAT["pro_yevmiye"]


def birim_musteri_ucreti(tip) -> float:
    """Tek seferlik işte kişi başına müşteriden alınan ücret."""
    return FIYAT["ogrenci_musteri"] if tip_coz(tip) == "student" else FIYAT["pro_musteri"]


def kadro_kur(pro_sayisi=0, ogrenci_sayisi=0, pro_yevmiye=None, ogrenci_yevmiye=None):
    """[{'tip': 'pro'|'student', 'ucret': yevmiye}] listesi üretir."""
    pu = float(pro_yevmiye) if pro_yevmiye is not None else FIYAT["pro_yevmiye"]
    ou = float(ogrenci_yevmiye) if ogrenci_yevmiye is not None else FIYAT["ogrenci_yevmiye"]
    kadro = [{"tip": "pro", "ucret": pu} for _ in range(max(0, int(pro_sayisi or 0)))]
    kadro += [{"tip": "student", "ucret": ou} for _ in range(max(0, int(ogrenci_sayisi or 0)))]
    return kadro


def kadro_say(kadro):
    """Kadro listesindeki profesyonel/öğrenci sayısı."""
    pro = sum(1 for p in kadro if tip_coz(p.get("tip")) == "pro")
    ogr = sum(1 for p in kadro if tip_coz(p.get("tip")) == "student")
    return pro, ogr


def ziyaret_ucreti(pro_sayisi=0, ogrenci_sayisi=0) -> float:
    """Bir ziyaretin varsayılan müşteri ücreti: kadro tip ve sayısına göre."""
    return (
        max(0, int(pro_sayisi or 0)) * FIYAT["pro_musteri"]
        + max(0, int(ogrenci_sayisi or 0)) * FIYAT["ogrenci_musteri"]
    )


def kadro_ziyaret_ucreti(kadro) -> float:
    pro, ogr = kadro_say(kadro)
    return ziyaret_ucreti(pro, ogr)


def abonelik_paket_ucreti(kota, pro_sayisi=0, ogrenci_sayisi=0) -> float:
    """Abonelik paketinin peşin ücreti: kota × ziyaret ücreti × katsayı."""
    kota = max(1, int(kota or FIYAT["abonelik_kota"]))
    return round(
        kota * ziyaret_ucreti(pro_sayisi, ogrenci_sayisi) * float(FIYAT["abonelik_katsayi"]),
        2,
    )


def ucret_dagilimi(toplam, kadro):
    """Toplam ücret verildiğinde kişi başı payları tip ağırlığına göre böler (bilgi amaçlı).

    Ağırlık, varsayılan birim ücretlerdir: profesyonel 4800, öğrenci 2800.
    """
    if not kadro:
        return []
    agirliklar = [birim_musteri_ucreti(p.get("tip")) for p in kadro]
    toplam_agirlik = sum(agirliklar) or 1.0
    return [
        {
            "tip": tip_coz(p.get("tip")),
            "yevmiye": float(p.get("ucret") or yevmiye(p.get("tip"))),
            "musteri_payi": round(float(toplam or 0) * a / toplam_agirlik, 2),
        }
        for p, a in zip(kadro, agirliklar)
    ]


def musteri_esle(customers, ad):
    """Müşteri eşleştir. Dönen: (müşteri | None, aday isimler).

    Sıra: birebir ad → tek kısmi eşleşme → benzer isimler (aday listesi).
    """
    q = str(ad or "").strip().casefold()
    if not q:
        return None, []
    kayitlar = [c for c in (customers or []) if (c.get("name") or "").strip()]
    for c in kayitlar:
        if (c.get("name") or "").strip().casefold() == q:
            return c, []

    # "emir bey", "nazlı hanım" gibi hitapları at
    temiz = q
    for ek in (" beyin", " beye", " bey", " hanımın", " hanıma", " hanım", " hanim", " abla", " abi"):
        if temiz.endswith(ek):
            temiz = temiz[: -len(ek)].strip()
    parcalar = [p for p in temiz.split() if len(p) > 1]

    def _kelime_eslesir(ad, parca):
        """Kelime başından eşleşme: 'emir' → 'Emir Kaya' evet, 'Nazlı Demir' hayır."""
        return any(k.startswith(parca) for k in str(ad or "").casefold().split())

    kismi = [
        c for c in kayitlar
        if parcalar and all(_kelime_eslesir(c.get("name"), p) for p in parcalar)
    ]
    if not kismi and temiz:
        # kelime eşleşmesi yoksa gevşek arama (ör. birleşik yazım)
        kismi = [c for c in kayitlar if temiz in (c.get("name") or "").casefold()]
    if len(kismi) == 1:
        return kismi[0], []
    if len(kismi) > 1:
        return None, [c["name"] for c in kismi]

    adlar = {(c.get("name") or "").casefold(): c.get("name") for c in kayitlar}
    yakin = difflib.get_close_matches(temiz or q, list(adlar.keys()), n=4, cutoff=0.55)
    return None, [adlar[y] for y in yakin]


# --- Abonelik düzeni ---

def _tarih(ds):
    try:
        return datetime.strptime(str(ds).strip(), "%d.%m.%Y").date()
    except (ValueError, TypeError):
        return None


def _metin(d):
    return d.strftime("%d.%m.%Y") if isinstance(d, date) else ""


def abonelik_periyodu(tarihler):
    """Planlanmış kota tarihlerinden düzeni tahmin et (gün cinsinden aralık).

    Aralıklar eşitse o değeri, değilse en sık görülen aralığı döndürür; tek tarih varsa 7.
    """
    gunler = sorted({_tarih(t) for t in tarihler if _tarih(t)})
    if len(gunler) < 2:
        return 7
    araliklar = [(gunler[i + 1] - gunler[i]).days for i in range(len(gunler) - 1)]
    araliklar = [a for a in araliklar if a > 0]
    if not araliklar:
        return 7
    return max(set(araliklar), key=araliklar.count)


def abonelik_erteleme_plani(tarihler, eski, yeni):
    """Bir kota ertelenince abonelik düzenini yeniden kur.

    Ertelenen günden sonraki planlı kotalar aynı kaydırmayla ileri alınır; böylece
    haftalık/periyodik düzen korunur. Dönen: {eski_tarih: yeni_tarih}.
    """
    eski_d, yeni_d = _tarih(eski), _tarih(yeni)
    if not eski_d or not yeni_d:
        return {}
    kayma = (yeni_d - eski_d).days
    if kayma == 0:
        return {}
    plan = {}
    for t in sorted({_tarih(x) for x in tarihler if _tarih(x)}):
        if t < eski_d:
            continue
        plan[_metin(t)] = _metin(t + timedelta(days=kayma))
    return plan


def abonelik_takvimi(baslangic, kota, periyot=7):
    """Başlangıç gününden itibaren periyoda göre kota tarihleri üretir."""
    d = _tarih(baslangic) if not isinstance(baslangic, date) else baslangic
    if not d:
        return []
    return [_metin(d + timedelta(days=periyot * i)) for i in range(max(1, int(kota or 1)))]


def fiyat_ozeti() -> str:
    """Sistem talimatına/gösterime uygun tek satırlık fiyat tablosu."""
    return (
        f"profesyonel: müşteriden {FIYAT['pro_musteri']:,.0f} ₺ / yevmiye {FIYAT['pro_yevmiye']:,.0f} ₺; "
        f"öğrenci: müşteriden {FIYAT['ogrenci_musteri']:,.0f} ₺ / yevmiye {FIYAT['ogrenci_yevmiye']:,.0f} ₺; "
        f"abonelik varsayılan kota: {FIYAT['abonelik_kota']}"
    )
