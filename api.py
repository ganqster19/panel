"""Vardiya paneli HTTP API'si.

Çalıştırma:
    pip install -r requirements-api.txt
    uvicorn api:app --host 0.0.0.0 --port 8000

Gerekli ortam değişkenleri:
    SUPABASE_HOST, SUPABASE_DB, SUPABASE_USER, SUPABASE_PASSWORD, SUPABASE_PORT
    GEMINI_API_KEY          → doğal dil komutları için
    PANEL_API_KEY           → istekleri doğrulamak için (X-API-Key başlığı)

Uç noktalar:
    GET  /saglik                       → bağlantı ve kayıt sayıları
    POST /komut   {"metin": "..."}     → doğal dil komutu (asistan araçları kullanır)
    POST /is      {iş JSON'u}          → AI'sız, şemayla doğrudan iş girişi
    POST /ertele  {"musteri": ...}     → rezervasyon erteleme
    GET  /gun/{tarih}                  → günün özeti
    GET  /ay?ay=9&yil=2026             → ayın özeti
"""
import os

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from vardiya.asistan import Asistan
from vardiya.isakisi import FIYAT

app = FastAPI(title="Vardiya Panel API", version="1.0")

API_KEY = os.environ.get("PANEL_API_KEY", "")


def _yetki(anahtar):
    if not API_KEY:
        raise HTTPException(500, "PANEL_API_KEY tanımlı değil; API kapalı.")
    if anahtar != API_KEY:
        raise HTTPException(401, "Geçersiz API anahtarı.")


def _asistan(uygula=True):
    try:
        return Asistan(uygula=uygula)
    except Exception as e:
        raise HTTPException(503, f"Veritabanına bağlanılamadı: {e}")


class Komut(BaseModel):
    metin: str = Field(..., description="Doğal dil komutu")
    uygula: bool = Field(True, description="False ise yalnızca plan döner, yazma yapılmaz")
    gecmis: list[dict] = Field(default_factory=list, description="[{rol, metin}] önceki mesajlar")


class IsIstegi(BaseModel):
    musteri: str
    etiket: str = "tek seferlik"
    tarihler: list[str] = Field(default_factory=list)
    kota: int | None = None
    pro_sayisi: int = 0
    ogrenci_sayisi: int = 0
    personeller: list[dict] = Field(default_factory=list)
    musteri_tutari: float | None = None
    fiyat_modu: str = "gunluk"
    isimler: list[str] = Field(default_factory=list)
    yeni_musteri: bool = Field(False, description="Müşteri yoksa profili otomatik açar")
    telefon: str = ""
    not_: str = Field("", alias="not")
    uygula: bool = True

    model_config = {"populate_by_name": True}


class ErteleIstegi(BaseModel):
    musteri: str
    yeni_tarih: str
    eski_tarih: str = ""
    uygula: bool = True


@app.get("/saglik")
def saglik(x_api_key: str = Header(default="")):
    """Bağlantıyı ve tarife ayarlarını doğrular."""
    _yetki(x_api_key)
    a = _asistan(uygula=False)
    try:
        return {
            "durum": "ok",
            "musteri": len(a.veri.get("customers") or []),
            "is_satiri": len(a.veri.get("jobs") or []),
            "profesyonel": len(a.veri.get("pros") or []),
            "ogrenci": len(a.veri.get("students") or []),
            "tarife": FIYAT,
        }
    finally:
        a.kapat()


@app.post("/komut")
def komut(istek: Komut, x_api_key: str = Header(default="")):
    """Doğal dil komutu: 'yarına emir beye 2 profesyonel gidecek tek seferlik'."""
    _yetki(x_api_key)
    a = _asistan(uygula=istek.uygula)
    try:
        return a.komut(istek.metin, gecmis=istek.gecmis)
    finally:
        a.kapat()


@app.post("/is")
def is_ekle(istek: IsIstegi, x_api_key: str = Header(default="")):
    """AI'sız doğrudan iş girişi. Tutar verilmezse tarife uygulanır."""
    _yetki(x_api_key)
    a = _asistan(uygula=istek.uygula)
    try:
        veri = istek.model_dump(by_alias=True)
        veri["not"] = veri.pop("not_", "") or veri.get("not", "")
        yeni_musteri = veri.pop("yeni_musteri", False)
        telefon = veri.pop("telefon", "")
        veri.pop("uygula", None)
        musteri_bilgi = None
        if yeni_musteri:
            musteri_bilgi = a.musteri_ekle(istek.musteri, telefon)
        mesaj = a.is_ekle(veri)
        if mesaj.startswith("Hata"):
            raise HTTPException(400, mesaj)
        return {"sonuc": mesaj, "islemler": a.gunluk, "musteri": musteri_bilgi}
    finally:
        a.kapat()


@app.post("/ertele")
def ertele(istek: ErteleIstegi, x_api_key: str = Header(default="")):
    """Rezervasyon erteleme; abonelikte düzen yeniden kurulur."""
    _yetki(x_api_key)
    a = _asistan(uygula=istek.uygula)
    try:
        mesaj = a.is_tasi(istek.musteri, istek.yeni_tarih, istek.eski_tarih)
        if mesaj.startswith("Hata"):
            raise HTTPException(400, mesaj)
        return {"sonuc": mesaj, "islemler": a.gunluk}
    finally:
        a.kapat()


@app.get("/gun/{tarih}")
def gun(tarih: str, x_api_key: str = Header(default="")):
    """Günün iş özeti ('bugun', 'yarin' ya da GG.AA.YYYY)."""
    _yetki(x_api_key)
    a = _asistan(uygula=False)
    try:
        return {"tarih": tarih, "ozet": a.gun_ozeti(tarih)}
    finally:
        a.kapat()


@app.get("/ay")
def ay(ay: int = 0, yil: int = 0, x_api_key: str = Header(default="")):
    """Ayın ciro/kâr özeti."""
    _yetki(x_api_key)
    a = _asistan(uygula=False)
    try:
        return {"ozet": a.ay_ozeti(ay, yil)}
    finally:
        a.kapat()
