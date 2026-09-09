"""Panel ve API'nin paylaştığı iş operasyonları.

Her operasyon `(mesaj, eylemler)` döndürür. Eylem = `(aciklama, sql, params)` üçlüsü ve
hiçbiri burada çalıştırılmaz: admin paneli bunları onay kuyruğuna atar, API/asistan ise
doğrudan uygular. Böylece iş kuralları tek yerde durur.

`veri` sözlüğü panelden ya da API'den gelen ham tablolardır:
    {"customers": [...], "jobs": [...], "pros": [...], "students": [...],
     "service_personnel": [...], "notes": [...], "expenses": [...]}
"""
import json
import re
import uuid
import calendar
from datetime import datetime, date, timedelta

from vardiya.isakisi import (
    FIYAT, tip_coz, yevmiye, kadro_kur, kadro_say, ziyaret_ucreti,
    abonelik_paket_ucreti, musteri_esle, abonelik_erteleme_plani,
)

AYLAR = {
    "ocak": 1, "şubat": 2, "subat": 2, "mart": 3, "nisan": 4, "mayıs": 5, "mayis": 5,
    "haziran": 6, "temmuz": 7, "ağustos": 8, "agustos": 8, "eylül": 9, "eylul": 9,
    "ekim": 10, "kasım": 11, "kasim": 11, "aralık": 12, "aralik": 12,
}
GUNLER = {
    "pazartesi": 0, "salı": 1, "sali": 1, "çarşamba": 2, "carsamba": 2,
    "perşembe": 3, "persembe": 3, "cuma": 4, "cumartesi": 5, "pazar": 6,
}

JOB_SQL = """INSERT INTO jobs (group_id, date, customer_id, job_type, price_worker,
                 price_customer, job_tag, is_prepaid, staff_name, staff_phone,
                 assigned_pro_id, assigned_student_id, job_note)
             VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"""


def _kur(d, mo, y) -> str:
    try:
        return date(int(y), int(mo), int(d)).strftime("%d.%m.%Y")
    except (ValueError, TypeError):
        return ""


def tarih_coz(metin) -> str:
    """'yarın', '3 temmuz', '2026-09-10', '10.9' gibi girdileri GG.AA.YYYY'ye çevirir."""
    t = str(metin or "").strip().casefold()
    if not t:
        return ""
    bugun = date.today()
    if t in ("bugün", "bugun", "bu gün", "bu gun"):
        return bugun.strftime("%d.%m.%Y")
    if t in ("yarın", "yarin"):
        return (bugun + timedelta(days=1)).strftime("%d.%m.%Y")
    if t in ("öbür gün", "obur gun", "ertesi gün", "ertesi gun"):
        return (bugun + timedelta(days=2)).strftime("%d.%m.%Y")
    if t in ("dün", "dun"):
        return (bugun - timedelta(days=1)).strftime("%d.%m.%Y")

    # "haftaya perşembe", "gelecek hafta cuma" → gün adını bir hafta ileri al
    haftaya = bool(re.search(r"haftaya|gelecek hafta|önümüzdeki hafta|onumuzdeki hafta|sonraki hafta", t))
    t = re.sub(r"\b(haftaya|gelecek|önümüzdeki|onumuzdeki|sonraki|bu)\s*(hafta)?\b", " ", t).strip()

    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", t)
    if m:
        return _kur(m.group(3), m.group(2), m.group(1))

    m = re.match(r"^(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{2,4}))?$", t)
    if m:
        yil = int(m.group(3)) if m.group(3) else bugun.year
        if yil < 100:
            yil += 2000
        return _kur(m.group(1), m.group(2), yil)

    m = re.match(r"^(\d{1,2})\s+([a-zçğıöşü]+)\s*(\d{4})?$", t)
    if m and m.group(2) in AYLAR:
        yil = int(m.group(3)) if m.group(3) else bugun.year
        return _kur(m.group(1), AYLAR[m.group(2)], yil)

    for ad, wd in GUNLER.items():
        if ad in t:
            fark = (wd - bugun.weekday()) % 7 or 7
            if haftaya:
                fark += 7
            return (bugun + timedelta(days=fark)).strftime("%d.%m.%Y")
    return ""


def _gun(ds):
    try:
        return datetime.strptime(str(ds).strip(), "%d.%m.%Y").date()
    except (ValueError, TypeError):
        return None


def musteri_bul(veri, ad):
    return musteri_esle(veri.get("customers") or [], ad)


def kisi_bul(veri, ad):
    """Personeli isimle bul: profesyonel, öğrenci, servis personeli."""
    q = str(ad or "").strip().casefold()
    if not q:
        return None
    for kind, key in (("pro", "pros"), ("student", "students"), ("service", "service_personnel")):
        for p in veri.get(key) or []:
            nm = (p.get("name") or "").casefold()
            if nm and (nm == q or q in nm):
                return {"kind": kind, "id": p.get("id"), "name": p.get("name"), "phone": p.get("phone")}
    return None


ETIKET_DB = {
    "subscription": "subscription", "one_time": "one_time", "hotel": "hotel",
    "turnkey": "turnkey", "construction": "turnkey", "insaat": "turnkey",
}


def etiket_coz(deger) -> str:
    """Serbest metni ya da veritabanı değerini geçerli etikete çevirir."""
    s = str(deger or "").strip().casefold()
    if s in ETIKET_DB:
        return ETIKET_DB[s]
    if "abone" in s or "kota" in s:
        return "subscription"
    if "otel" in s or "hotel" in s:
        return "hotel"
    if "anahtar" in s or "teslim" in s or "inşaat" in s or "insaat" in s:
        return "turnkey"
    return "one_time"


def abonelik_mi(tag) -> bool:
    return etiket_coz(tag) == "subscription"


def gunun_isleri(veri, cid, tarih):
    return [
        j for j in veri.get("jobs") or []
        if j.get("customer_id") == cid and (j.get("date") or "") == tarih
    ]


def kadro_say_metni(pro_n, ogr_n) -> str:
    """'2 profesyonel + 1 öğrenci' biçiminde okunur kadro metni."""
    return " + ".join(
        x for x in (f"{pro_n} profesyonel" if pro_n else "", f"{ogr_n} öğrenci" if ogr_n else "") if x
    ) or "kadro yok"


def kayitli(satirlar):
    """Henüz veritabanına yazılmamış (id'siz) önizleme satırlarını ayıklar."""
    return [j for j in satirlar if j.get("id")]


def planli_gunler(veri, cid):
    gunler = {
        (j.get("date") or "").strip() for j in veri.get("jobs") or []
        if j.get("customer_id") == cid and (j.get("date") or "").strip()
    }
    return sorted(gunler, key=lambda d: _gun(d) or date.min)


def is_satirlari(job_tag, tarihler, musteri_id, musteri_tutari, fiyat_modu, personeller):
    """İşi jobs tablosu satırlarına böler.

    Abonelikte müşteri tutarı yalnızca ilk kotanın ilk satırına yazılır; kalan kotalar
    tüketilirken gelir yazılmaz, sadece personel yevmiyesi gider olur.
    Tek seferlikte fiyat_modu 'Günlük' ise her ziyarette, 'Toplam' ise yalnızca ilkinde alınır.
    """
    pkg_id = str(uuid.uuid4())[:8]
    abonelik = abonelik_mi(job_tag)
    satirlar = []
    odendi = False
    for i, d in enumerate(tarihler):
        ds = d.strftime("%d.%m.%Y") if isinstance(d, date) else (str(d or "") if d else "")
        gid = f"{pkg_id}_{i}" if abonelik else pkg_id
        if abonelik:
            ziyaret_tutari = float(musteri_tutari) if i == 0 else 0.0
        else:
            ziyaret_tutari = float(musteri_tutari) if (fiyat_modu == "Günlük" or not odendi) else 0.0
            if fiyat_modu != "Günlük":
                odendi = True
        ilk = False
        for p in personeller:
            cut = ziyaret_tutari if not ilk else 0.0
            ilk = True
            satirlar.append((
                gid, ds, musteri_id, tip_coz(p.get("tip")), float(p.get("ucret") or 0),
                cut, etiket_coz(job_tag), 1 if cut > 0 else 0,
            ))
    return satirlar


def _json_coz(kaynak):
    if isinstance(kaynak, dict):
        return kaynak, None
    try:
        return json.loads(kaynak), None
    except Exception:
        m = re.search(r"\{.*\}", str(kaynak or ""), re.S)
        if not m:
            return None, "Hata: geçerli JSON bulunamadı."
        try:
            return json.loads(m.group(0)), None
        except Exception as e:
            return None, f"Hata: JSON okunamadı ({e})."


def is_ekle(veri, istek):
    """Yeni iş kurar. `istek` JSON metni ya da sözlük.

    Dönen: (mesaj, eylemler, satir_onizleme). Fiyat/yevmiye verilmezse tarife uygulanır.
    """
    param, hata = _json_coz(istek)
    if hata:
        return hata, [], []
    if not isinstance(param, dict):
        return "Hata: istek bir JSON nesnesi olmalı.", [], []

    musteri, adaylar = musteri_bul(veri, param.get("musteri"))
    if not musteri:
        if adaylar:
            return (
                f"Hata: '{param.get('musteri')}' netleşmedi. Adaylar: " + ", ".join(adaylar),
                [], [],
            )
        return (
            f"Hata: '{param.get('musteri')}' kayıtlı müşteri değil; önce müşteri profili açılmalı.",
            [], [],
        )

    tag = etiket_coz(param.get("etiket"))

    personeller = []
    for p in param.get("personeller") or []:
        if isinstance(p, dict):
            ptip = tip_coz(p.get("tip"))
            ham = p.get("ucret", p.get("yevmiye"))
            personeller.append({
                "tip": ptip,
                "ucret": float(ham) if ham not in (None, "") else yevmiye(ptip),
            })
    if not personeller:
        pro_n = int(param.get("pro_sayisi") or param.get("profesyonel_sayisi") or 0)
        ogr_n = int(param.get("ogrenci_sayisi") or 0)
        if not pro_n and not ogr_n:
            adet = max(1, int(param.get("kisi_sayisi") or 1))
            if tip_coz(param.get("personel_tipi")) == "student":
                ogr_n = adet
            else:
                pro_n = adet
        personeller = kadro_kur(
            pro_n, ogr_n,
            pro_yevmiye=param.get("pro_yevmiye", param.get("yevmiye")),
            ogrenci_yevmiye=param.get("ogrenci_yevmiye", param.get("yevmiye")),
        )

    if abonelik_mi(tag):
        kota = int(param.get("kota") or len(param.get("tarihler") or []) or FIYAT["abonelik_kota"])
        tarihler = [None] * max(1, kota)
    else:
        ham = param.get("tarihler") or ([param.get("tarih")] if param.get("tarih") else [])
        tarihler = []
        for t in ham:
            ds = tarih_coz(t)
            if not ds:
                return f"Hata: '{t}' tarihi anlaşılmadı (GG.AA.YYYY bekleniyor).", [], []
            tarihler.append(_gun(ds))
        if not tarihler:
            return "Hata: tarihli işlerde en az bir tarih gerekir.", [], []

    pro_n, ogr_n = kadro_say(personeller)
    ham_tutar = param.get("musteri_tutari", param.get("toplam_ucret"))
    otomatik = ham_tutar in (None, "")
    if otomatik:
        if abonelik_mi(tag):
            # Abonelik paketinin bedeli işe göre pazarlıkla belirlendiği için varsayılan uygulanmaz.
            oneri = abonelik_paket_ucreti(len(tarihler), pro_n, ogr_n)
            return (
                f"Hata: abonelik paket ücreti belirtilmedi. {musteri['name']} için "
                f"{len(tarihler)} kota × ({kadro_say_metni(pro_n, ogr_n)}) planlandı; "
                f"tek seferlik tarifeyle karşılığı {oneri:,.0f} ₺ olurdu. "
                "Kullanıcıya paket için ne kadar yazılacağını sor ve musteri_tutari olarak gönder.",
                [], [],
            )
        tutar = ziyaret_ucreti(pro_n, ogr_n)
        fiyat_modu = "Günlük"
    else:
        tutar = float(ham_tutar)
        fiyat_modu = "Toplam" if str(param.get("fiyat_modu") or "").casefold().startswith("top") else "Günlük"

    isimler = [str(x).strip() for x in (param.get("isimler") or []) if str(x).strip()]
    bulunamayan = [n for n in isimler if not kisi_bul(veri, n)]
    job_note = (param.get("not") or param.get("aciklama") or "").strip() or None

    satirlar = is_satirlari(tag, tarihler, musteri["id"], tutar, fiyat_modu, personeller)
    eylemler, onizleme = [], []
    for idx, (gid, ds, cid, jtype, wp, cut, jtag, prepaid) in enumerate(satirlar):
        slot = idx % len(personeller)
        kisi = kisi_bul(veri, isimler[slot]) if slot < len(isimler) else None
        if kisi and kisi["kind"] in ("pro", "student"):
            jtype = kisi["kind"]
        pro_id = kisi["id"] if kisi and kisi["kind"] == "pro" else None
        stu_id = kisi["id"] if kisi and kisi["kind"] == "student" else None
        params = (
            gid, ds, cid, jtype, wp, cut, jtag, prepaid,
            kisi["name"] if kisi else None, (kisi.get("phone") if kisi else None) or None,
            pro_id, stu_id, job_note,
        )
        eylemler.append((f"İş: {musteri['name']}", JOB_SQL, params))
        onizleme.append({
            "group_id": gid, "date": ds, "customer_id": cid, "job_type": jtype,
            "price_worker": wp, "price_customer": cut, "job_tag": jtag,
            "is_prepaid": prepaid, "name": musteri["name"], "is_collected": 0,
            "is_worker_paid": 0, "assigned_pro_id": pro_id, "assigned_student_id": stu_id,
            "staff_name": kisi["name"] if kisi else None, "job_note": job_note,
        })

    maliyet = sum(float(p["ucret"] or 0) for p in personeller) * len(tarihler)
    gelir = tutar * len(tarihler) if (fiyat_modu == "Günlük" and not abonelik_mi(tag)) else tutar
    birim = "kota" if abonelik_mi(tag) else "ziyaret"
    kadro_txt = kadro_say_metni(pro_n, ogr_n)
    mesaj = (
        f"{musteri['name']} · {len(tarihler)} {birim} × ({kadro_txt}) = {len(satirlar)} satır. "
        f"Müşteri tutarı {tutar:,.0f} ₺ ({fiyat_modu}{', tarifeden otomatik' if otomatik else ''}), "
        f"toplam gelir {gelir:,.0f} ₺, personel maliyeti {maliyet:,.0f} ₺, net {gelir - maliyet:,.0f} ₺."
    )
    if bulunamayan:
        mesaj += " Atanamayan isim(ler): " + ", ".join(bulunamayan) + "."
    return mesaj, eylemler, onizleme


def is_tasi(veri, musteri_adi, yeni_tarih, eski_tarih=""):
    """Rezervasyonu erteler. Abonelikse sonraki kotalar da kaydırılıp düzen yeniden kurulur."""
    musteri, adaylar = musteri_bul(veri, musteri_adi)
    if not musteri:
        return (f"Hata: '{musteri_adi}' bulunamadı."
                + (f" Adaylar: {', '.join(adaylar)}" if adaylar else ""), [])

    yeni = tarih_coz(yeni_tarih)
    if not yeni:
        return "Hata: yeni tarih anlaşılmadı.", []

    eski = tarih_coz(eski_tarih) if str(eski_tarih or "").strip() else ""
    if not eski:
        bugun = date.today()
        gelecek = [g for g in planli_gunler(veri, musteri["id"]) if (_gun(g) or date.min) >= bugun]
        if not gelecek:
            return f"Hata: {musteri['name']} için ileri tarihli planlı iş yok.", []
        eski = gelecek[0]

    mevcut = gunun_isleri(veri, musteri["id"], eski)
    if not mevcut:
        planli = planli_gunler(veri, musteri["id"])
        return (f"Hata: {musteri['name']} için {eski} tarihinde iş yok."
                + (f" Planlı günleri: {', '.join(planli[:8])}" if planli else ""), [])
    if eski == yeni:
        return f"Hata: iş zaten {eski} tarihinde; taşımaya gerek yok.", []

    if not any(abonelik_mi(j.get("job_tag")) for j in mevcut):
        return (
            f"{musteri['name']} · tek seferlik iş {eski} → {yeni} taşınacak ({len(mevcut)} satır).",
            [(f"Erteleme: {musteri['name']}",
              "UPDATE jobs SET date = %s WHERE customer_id = %s AND date = %s",
              (yeni, musteri["id"], eski))],
        )

    pids = {(j.get("group_id") or "").split("_")[0] for j in mevcut if j.get("group_id")}
    paket_gunleri = sorted(
        {
            (j.get("date") or "").strip() for j in veri.get("jobs") or []
            if (j.get("group_id") or "").split("_")[0] in pids and (j.get("date") or "").strip()
        },
        key=lambda d: _gun(d) or date.min,
    )
    plan = abonelik_erteleme_plani(paket_gunleri, eski, yeni)
    if not plan:
        return "Hata: erteleme planı kurulamadı — yeni tarih eskisiyle aynı olabilir.", []

    kayma = ((_gun(yeni) - _gun(eski)).days) if (_gun(yeni) and _gun(eski)) else 0
    sirali = sorted(plan.items(), key=lambda x: _gun(x[0]) or date.min, reverse=kayma > 0)
    eylemler = [
        (f"Abonelik düzeni: {musteri['name']}",
         "UPDATE jobs SET date = %s WHERE customer_id = %s AND date = %s AND job_tag = 'subscription'",
         (yeni_g, musteri["id"], eski_g))
        for eski_g, yeni_g in sirali
    ]
    zincir = ", ".join(
        f"{a}→{b}" for a, b in sorted(plan.items(), key=lambda x: _gun(x[0]) or date.min)
    )
    return (
        f"{musteri['name']} aboneliği {kayma:+d} gün kaydırıldı, {len(plan)} planlı kota "
        f"yeniden dizildi ({zincir}). Havuzdaki tarihsiz kotalar etkilenmedi.",
        eylemler,
    )


def is_iptal(veri, musteri_adi, tarih):
    """Bir günün tüm iş satırlarını siler."""
    musteri, adaylar = musteri_bul(veri, musteri_adi)
    if not musteri:
        return (f"Hata: '{musteri_adi}' bulunamadı."
                + (f" Adaylar: {', '.join(adaylar)}" if adaylar else ""), [])
    ds = tarih_coz(tarih)
    if not ds:
        return "Hata: tarih anlaşılmadı.", []
    mevcut = gunun_isleri(veri, musteri["id"], ds)
    if not mevcut:
        return f"Hata: {musteri['name']} için {ds} tarihinde iş yok.", []
    return (
        f"{musteri['name']} · {ds} · {len(mevcut)} satır silinecek.",
        [(f"İptal: {musteri['name']}",
          "DELETE FROM jobs WHERE customer_id = %s AND date = %s",
          (musteri["id"], ds))],
    )


def kota_ekle(veri, musteri_adi, adet, personel_yevmiyesi=None):
    """Mevcut aboneliğe havuzda bekleyen kota ekler."""
    musteri, adaylar = musteri_bul(veri, musteri_adi)
    if not musteri:
        return (f"Hata: '{musteri_adi}' bulunamadı."
                + (f" Adaylar: {', '.join(adaylar)}" if adaylar else ""), [])
    cid = musteri["id"]
    paketler = [
        (j.get("group_id") or "").split("_")[0] for j in veri.get("jobs") or []
        if j.get("customer_id") == cid and abonelik_mi(j.get("job_tag")) and j.get("group_id")
    ]
    if not paketler:
        return f"Hata: {musteri['name']} için abonelik paketi yok; önce abonelik işi açılmalı.", []
    pid = paketler[0]
    sira = [
        int((j.get("group_id") or "").split("_")[1])
        for j in veri.get("jobs") or []
        if (j.get("group_id") or "").startswith(f"{pid}_")
        and (j.get("group_id") or "").split("_")[1].isdigit()
    ]
    baslangic = (max(sira) + 1) if sira else 0
    ucret = float(personel_yevmiyesi) if personel_yevmiyesi not in (None, "") else FIYAT["pro_yevmiye"]

    eylemler = []
    for i in range(max(1, int(adet or 1))):
        eylemler.append((
            f"Kota ekle: {musteri['name']}",
            "INSERT INTO jobs (group_id, date, customer_id, job_type, price_worker, price_customer, job_tag, is_prepaid) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (f"{pid}_{baslangic + i}", "", cid, "pro", ucret, 0.0, "subscription", 0),
        ))
    return f"{musteri['name']} aboneliğine {len(eylemler)} kota eklenecek (havuzda bekler).", eylemler


def kota_sil(veri, musteri_adi, adet):
    """Havuzda bekleyen (tarihsiz) kotalardan siler."""
    musteri, adaylar = musteri_bul(veri, musteri_adi)
    if not musteri:
        return (f"Hata: '{musteri_adi}' bulunamadı."
                + (f" Adaylar: {', '.join(adaylar)}" if adaylar else ""), [])
    bekleyen = kayitli([
        j for j in veri.get("jobs") or []
        if j.get("customer_id") == musteri["id"] and abonelik_mi(j.get("job_tag"))
        and not (j.get("date") or "").strip()
    ])
    if not bekleyen:
        return f"Hata: {musteri['name']} için havuzda bekleyen kota yok.", []
    secilen = bekleyen[:max(1, int(adet or 1))]
    eylemler = [
        (f"Kota sil: {musteri['name']}", "DELETE FROM jobs WHERE id=%s", (j["id"],))
        for j in secilen
    ]
    return f"{musteri['name']} havuzundan {len(secilen)} kota silinecek.", eylemler


def kota_yerlestir(veri, musteri_adi, tarih, adet=1):
    """Havuzdaki kotayı belirli bir güne yerleştirir (kota tüketimi)."""
    musteri, adaylar = musteri_bul(veri, musteri_adi)
    if not musteri:
        return (f"Hata: '{musteri_adi}' bulunamadı."
                + (f" Adaylar: {', '.join(adaylar)}" if adaylar else ""), [])
    ds = tarih_coz(tarih)
    if not ds:
        return "Hata: tarih anlaşılmadı.", []
    bekleyen = kayitli([
        j for j in veri.get("jobs") or []
        if j.get("customer_id") == musteri["id"] and abonelik_mi(j.get("job_tag"))
        and not (j.get("date") or "").strip()
    ])
    if not bekleyen:
        return f"Hata: {musteri['name']} için havuzda bekleyen kota yok.", []
    secilen = bekleyen[:max(1, int(adet or 1))]
    eylemler = [
        (f"Kota yerleştir: {musteri['name']}", "UPDATE jobs SET date=%s WHERE id=%s", (ds, j["id"]))
        for j in secilen
    ]
    return f"{musteri['name']} için {len(secilen)} kota {ds} tarihine yerleştirilecek.", eylemler


def kisi_ekle(veri, musteri_adi, tarih, personel_tipi, yevmiye_tutari=None):
    """Planlanmış işe ek personel ekler."""
    musteri, adaylar = musteri_bul(veri, musteri_adi)
    if not musteri:
        return (f"Hata: '{musteri_adi}' bulunamadı."
                + (f" Adaylar: {', '.join(adaylar)}" if adaylar else ""), [])
    ds = tarih_coz(tarih)
    mevcut = gunun_isleri(veri, musteri["id"], ds)
    if not mevcut:
        return f"Hata: {musteri['name']} için {ds or tarih} tarihinde iş yok.", []
    tip = tip_coz(personel_tipi)
    ucret = float(yevmiye_tutari) if yevmiye_tutari not in (None, "") else yevmiye(tip)
    gid = mevcut[0].get("group_id") or str(uuid.uuid4())[:8]
    tag = etiket_coz(mevcut[0].get("job_tag"))
    return (
        f"{musteri['name']} · {ds} işine 1 {'öğrenci' if tip == 'student' else 'profesyonel'} "
        f"eklenecek (yevmiye {ucret:,.0f} ₺).",
        [(f"Kişi ekle: {musteri['name']}",
          "INSERT INTO jobs (group_id, date, customer_id, job_type, price_worker, price_customer, job_tag, is_prepaid) "
          "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
          (gid, ds, musteri["id"], tip, ucret, 0.0, tag, 0))],
    )


def kisi_sil(veri, musteri_adi, tarih, adet=1):
    """Planlanmış işten kişi çıkarır; önce atanmamış satırlar."""
    musteri, adaylar = musteri_bul(veri, musteri_adi)
    if not musteri:
        return (f"Hata: '{musteri_adi}' bulunamadı."
                + (f" Adaylar: {', '.join(adaylar)}" if adaylar else ""), [])
    ds = tarih_coz(tarih)
    mevcut = gunun_isleri(veri, musteri["id"], ds)
    if not mevcut:
        return f"Hata: {musteri['name']} için {ds or tarih} tarihinde iş yok.", []
    mevcut = sorted(
        kayitli(mevcut),
        key=lambda j: 0 if (not j.get("assigned_student_id") and not j.get("assigned_pro_id")) else 1,
    )
    if not mevcut:
        return f"Hata: {musteri['name']} · {ds} işi henüz kaydedilmemiş; önce kaydet.", []
    secilen = mevcut[:max(1, int(adet or 1))]
    eylemler = [
        (f"Kişi sil: {musteri['name']}", "DELETE FROM jobs WHERE id=%s", (j["id"],))
        for j in secilen
    ]
    return f"{musteri['name']} · {ds} işinden {len(secilen)} kişi çıkarılacak.", eylemler


def fiyat_guncelle(veri, musteri_adi, tarih, musteri_tutari):
    """Bir günün müşteri tutarını günceller (tutar ilk satıra yazılır)."""
    musteri, adaylar = musteri_bul(veri, musteri_adi)
    if not musteri:
        return (f"Hata: '{musteri_adi}' bulunamadı."
                + (f" Adaylar: {', '.join(adaylar)}" if adaylar else ""), [])
    ds = tarih_coz(tarih)
    mevcut = kayitli(gunun_isleri(veri, musteri["id"], ds))
    if not mevcut:
        return f"Hata: {musteri['name']} için {ds or tarih} tarihinde kayıtlı iş yok.", []
    tutar = float(musteri_tutari or 0)
    return (
        f"{musteri['name']} · {ds} işinin müşteri tutarı {tutar:,.0f} ₺ olacak.",
        [
            (f"Fiyat sıfırla: {musteri['name']}",
             "UPDATE jobs SET price_customer=0, is_prepaid=0 WHERE customer_id=%s AND date=%s",
             (musteri["id"], ds)),
            (f"Fiyat yaz: {musteri['name']}",
             "UPDATE jobs SET price_customer=%s, is_prepaid=%s WHERE id=%s",
             (tutar, 1 if tutar > 0 else 0, mevcut[0]["id"])),
        ],
    )


def etiket_degistir(veri, musteri_adi, tarih, etiket):
    """Bir günün işinin etiketini değiştirir."""
    musteri, adaylar = musteri_bul(veri, musteri_adi)
    if not musteri:
        return (f"Hata: '{musteri_adi}' bulunamadı."
                + (f" Adaylar: {', '.join(adaylar)}" if adaylar else ""), [])
    ds = tarih_coz(tarih)
    if not gunun_isleri(veri, musteri["id"], ds):
        return f"Hata: {musteri['name']} için {ds or tarih} tarihinde iş yok.", []
    yeni = etiket_coz(etiket)
    return (
        f"{musteri['name']} · {ds} işi '{yeni}' etiketine geçecek.",
        [(f"Etiket: {musteri['name']}",
          "UPDATE jobs SET job_tag=%s WHERE customer_id=%s AND date=%s",
          (yeni, musteri["id"], ds))],
    )


def tahsilat_isaretle(veri, musteri_adi, tarih):
    """Bir günün müşteri tahsilatını alındı olarak işaretler."""
    musteri, adaylar = musteri_bul(veri, musteri_adi)
    if not musteri:
        return (f"Hata: '{musteri_adi}' bulunamadı."
                + (f" Adaylar: {', '.join(adaylar)}" if adaylar else ""), [])
    ds = tarih_coz(tarih)
    mevcut = kayitli([
        j for j in gunun_isleri(veri, musteri["id"], ds) if float(j.get("price_customer") or 0) > 0
    ])
    if not mevcut:
        return f"Hata: {musteri['name']} · {ds or tarih} için tahsil edilecek tutar yok.", []
    toplam = sum(float(j.get("price_customer") or 0) for j in mevcut)
    eylemler = [
        (f"Tahsilat: {musteri['name']}", "UPDATE jobs SET is_collected=1 WHERE id=%s", (j["id"],))
        for j in mevcut
    ]
    return f"{musteri['name']} · {ds} · {toplam:,.0f} ₺ tahsil edildi işaretlenecek.", eylemler


def musteri_ekle_eylemi(ad, telefon="", konum=""):
    """Yeni müşteri kaydı için eylem (id gerektiren işler için önce uygulanmalı)."""
    return (
        f"Müşteri: {ad}",
        "INSERT INTO customers (name, phone, location) VALUES (%s, %s, %s) RETURNING id",
        (str(ad).strip(), str(telefon or "").strip(), str(konum or "").strip()),
    )


def personel_ekle_eylemi(ad, tip, telefon="", maas=0.0):
    if tip_coz(tip) == "student":
        return (
            f"Öğrenci: {ad}",
            "INSERT INTO students (name, phone) VALUES (%s, %s)",
            (str(ad).strip(), str(telefon or "").strip()),
        )
    return (
        f"Profesyonel: {ad}",
        "INSERT INTO professionals (name, phone, salary) VALUES (%s, %s, %s)",
        (str(ad).strip(), str(telefon or "").strip(), float(maas or 0)),
    )


def gider_ekle(tarih, aciklama, tutar):
    ds = tarih_coz(tarih) or date.today().strftime("%d.%m.%Y")
    return (
        f"{ds} · '{aciklama}' · {float(tutar):,.0f} ₺ gider eklenecek.",
        [(f"Gider: {aciklama}",
          "INSERT INTO expenses (date, description, amount) VALUES (%s, %s, %s)",
          (ds, aciklama, float(tutar)))],
    )


def not_ekle(veri, tarih, not_metni):
    ds = tarih_coz(tarih) or date.today().strftime("%d.%m.%Y")
    mevcut = next(
        (n.get("note") or "" for n in veri.get("notes") or [] if n.get("date") == ds), ""
    )
    birlesik = f"{mevcut}\n{not_metni}".strip() if mevcut else not_metni
    return (
        f"{ds} notu güncellenecek: '{not_metni}'",
        [(f"Not: {ds}",
          "INSERT INTO daily_notes (date, note) VALUES (%s, %s) "
          "ON CONFLICT (date) DO UPDATE SET note = EXCLUDED.note",
          (ds, birlesik))],
    )


# --- Okuma / özet ---

def _ziyaret_ozetleri(veri, cid=None, ilk=None, son=None):
    """Ziyaret bazlı özet: (tarih, müşteri, etiket, kişi, ciro, maliyet)."""
    from vardiya.db import (
        group_jobs_by_visit, visit_group_label, visit_customer_revenue,
        visit_worker_cost, job_tag_label,
    )
    isler = [j for j in veri.get("jobs") or [] if (j.get("date") or "").strip()]
    if cid is not None:
        isler = [j for j in isler if j.get("customer_id") == cid]
    ozetler = []
    for grup in group_jobs_by_visit(isler):
        rep = visit_group_label(grup)
        g = _gun(rep.get("date"))
        if ilk and (not g or g < ilk):
            continue
        if son and (not g or g > son):
            continue
        ciro = visit_customer_revenue(grup)
        maliyet = visit_worker_cost(grup)
        ozetler.append({
            "tarih": rep.get("date") or "",
            "gun": g or date.min,
            "musteri": rep.get("name") or "—",
            "etiket": job_tag_label(rep.get("job_tag")),
            "job_tag": etiket_coz(rep.get("job_tag")),
            "kisi": len(grup),
            "isimler": ", ".join(
                sorted({(r.get("staff_name") or "").strip() for r in grup if (r.get("staff_name") or "").strip()})
            ) or "atanmadı",
            "ciro": ciro,
            "maliyet": maliyet,
            "kar": ciro - maliyet,
        })
    ozetler.sort(key=lambda x: (x["gun"], x["musteri"]))
    return ozetler


def gun_ozeti(veri, tarih):
    ds = tarih_coz(tarih)
    if not ds:
        return "Hata: tarih anlaşılmadı."
    g = _gun(ds)
    ozetler = _ziyaret_ozetleri(veri, ilk=g, son=g)
    if not ozetler:
        return f"{ds}: planlanmış iş yok."
    satir = [f"{ds} · {len(ozetler)} ziyaret:"]
    for s in ozetler:
        satir.append(
            f"- {s['musteri']} · {s['etiket']} · {s['kisi']} kişi ({s['isimler']}) · "
            f"ciro {s['ciro']:,.0f} ₺ · kâr {s['kar']:,.0f} ₺"
        )
    satir.append(
        f"Toplam ciro {sum(s['ciro'] for s in ozetler):,.0f} ₺, "
        f"kâr {sum(s['kar'] for s in ozetler):,.0f} ₺."
    )
    return "\n".join(satir)


def musteri_ozeti(veri, musteri_adi, son_kayit=8):
    musteri, adaylar = musteri_bul(veri, musteri_adi)
    if not musteri:
        return (f"Hata: '{musteri_adi}' bulunamadı."
                + (f" Adaylar: {', '.join(adaylar)}" if adaylar else ""))
    ozetler = _ziyaret_ozetleri(veri, cid=musteri["id"])
    bekleyen = len({
        (j.get("group_id") or "") for j in veri.get("jobs") or []
        if j.get("customer_id") == musteri["id"] and abonelik_mi(j.get("job_tag"))
        and not (j.get("date") or "").strip()
    } - {""})
    if not ozetler:
        return f"{musteri['name']}: tarihli iş yok. Havuzda bekleyen kota: {bekleyen}."
    satir = [
        f"{musteri['name']} · {len(ozetler)} ziyaret · ciro {sum(s['ciro'] for s in ozetler):,.0f} ₺ · "
        f"kâr {sum(s['kar'] for s in ozetler):,.0f} ₺ · bekleyen kota {bekleyen}. Son işler:"
    ]
    for s in list(reversed(ozetler))[:max(1, int(son_kayit or 8))]:
        satir.append(
            f"- {s['tarih']} · {s['etiket']} · {s['kisi']} kişi ({s['isimler']}) · "
            f"ciro {s['ciro']:,.0f} ₺ · kâr {s['kar']:,.0f} ₺"
        )
    return "\n".join(satir)


def ay_ozeti(veri, ay=0, yil=0):
    bugun = date.today()
    sm = int(ay) if ay else bugun.month
    sy = int(yil) if yil else bugun.year
    ilk = date(sy, sm, 1)
    son = date(sy, sm, calendar.monthrange(sy, sm)[1])
    ozetler = _ziyaret_ozetleri(veri, ilk=ilk, son=son)
    if not ozetler:
        return f"{sm:02d}.{sy}: iş kaydı yok."
    giderler = sum(
        float(e.get("amount") or 0) for e in veri.get("expenses") or []
        if (e.get("date") or "").endswith(f".{sm:02d}.{sy}")
    )
    ciro = sum(s["ciro"] for s in ozetler)
    maliyet = sum(s["maliyet"] for s in ozetler)
    etiketler = {}
    for s in ozetler:
        etiketler[s["etiket"]] = etiketler.get(s["etiket"], 0.0) + s["ciro"]
    musteriler = {}
    for s in ozetler:
        musteriler[s["musteri"]] = musteriler.get(s["musteri"], 0.0) + s["kar"]
    satir = [
        f"{sm:02d}.{sy} · {len(ozetler)} ziyaret · ciro {ciro:,.0f} ₺ · personel {maliyet:,.0f} ₺ · "
        f"ek gider {giderler:,.0f} ₺ · net {ciro - maliyet - giderler:,.0f} ₺.",
        "Etiket dağılımı: " + ", ".join(f"{k} {v:,.0f} ₺" for k, v in etiketler.items()),
        "En kârlı müşteriler: " + ", ".join(
            f"{k} {v:,.0f} ₺" for k, v in sorted(musteriler.items(), key=lambda x: -x[1])[:5]
        ),
    ]
    return "\n".join(satir)


def bekleyen_kotalar(veri):
    sayac = {}
    for j in veri.get("jobs") or []:
        if not abonelik_mi(j.get("job_tag")) or (j.get("date") or "").strip():
            continue
        ad = j.get("name") or "—"
        sayac.setdefault(ad, set()).add(j.get("group_id") or f"row{j.get('id')}")
    if not sayac:
        return "Havuzda bekleyen kota yok."
    return "Bekleyen kotalar: " + ", ".join(f"{ad} ({len(g)})" for ad, g in sorted(sayac.items()))


def liste(veri, tip="musteri"):
    s = str(tip or "").casefold()
    if s.startswith(("mus", "müş")):
        adlar, baslik = [c.get("name") for c in veri.get("customers") or []], "Müşteriler"
    elif s.startswith(("ogr", "öğr")):
        adlar, baslik = [p.get("name") for p in veri.get("students") or []], "Öğrenciler"
    elif s.startswith("ser"):
        adlar, baslik = [p.get("name") for p in veri.get("service_personnel") or []], "Servis personeli"
    else:
        adlar, baslik = [p.get("name") for p in veri.get("pros") or []], "Profesyoneller"
    adlar = [a for a in adlar if a]
    if not adlar:
        return f"{baslik}: kayıt yok."
    return f"{baslik} ({len(adlar)}): " + ", ".join(adlar)
