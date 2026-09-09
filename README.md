# Vardiya Panel

Temizlik işletmesi vardiya yönetimi — Supabase PostgreSQL + Streamlit.

## Dosya yapısı

```
vardiya-panel/
├── admin.py              # Admin paneli (finans, analiz, takvim, AI)
├── mobil.py              # Mobil yönetici paneli
├── servis.py             # Servis ekibi listesi (salt okunur)
├── api.py                # HTTP API (FastAPI) — panel dışından iş girişi ve asistan
├── kural_testleri.py     # İş kuralı testleri (veritabanı gerektirmez)
├── vardiya/
│   ├── db.py             # Veritabanı ve panel yardımcıları
│   ├── isakisi.py        # Fiyat tarifesi, kadro, müşteri eşleştirme, abonelik düzeni
│   ├── islemler.py       # Panel ve API'nin paylaştığı iş operasyonları
│   ├── asistan.py        # Streamlit'siz asistan (API/CLI/bot için)
│   ├── auth.py           # Panel şifre koruması
│   └── perf.py           # Performans ölçümü (opsiyonel)
├── .streamlit/
│   ├── config.toml
│   └── secrets.toml.example
├── requirements.txt
└── runtime.txt
```

## Paneller

| Dosya | Kim kullanır | Streamlit Cloud Main file |
|-------|----------------|---------------------------|
| `mobil.py` | Yönetici (mobil) | `mobil.py` |
| `servis.py` | Servis ekibi | `servis.py` |
| `admin.py` | Masaüstü yönetim | `admin.py` |

## Lokal çalıştırma

```bash
pip install -r requirements.txt
copy .streamlit\secrets.toml.example .streamlit\secrets.toml
# secrets.toml içindeki değerleri doldurun

streamlit run mobil.py
streamlit run servis.py
streamlit run admin.py
```

## Yeni repoya taşıma

```bash
# Eski .git klasörünü silin (temiz başlangıç)
Remove-Item -Recurse -Force .git

git init -b main
git add .
git status   # secrets.toml ve __pycache__ listede OLMAMALI
git commit -m "Initial commit: vardiya panel"
git remote add origin https://github.com/KULLANICI/YENI-REPO.git
git push -u origin main
```

> Repoyu **Private** yapın. `secrets.toml` asla commit edilmemeli.

## Streamlit Community Cloud

1. GitHub'a yükleyin (`secrets.toml` **yüklenmesin**).
2. [share.streamlit.io](https://share.streamlit.io) → GitHub ile giriş.
3. **Her panel için ayrı app** oluşturun:

   - App 1 → Main file: `mobil.py`
   - App 2 → Main file: `servis.py`
   - App 3 → Main file: `admin.py`

4. Her app'te **Settings → Secrets** → `.streamlit/secrets.toml.example` şablonunu gerçek değerlerle doldurun.

5. Deploy sonrası **Reboot app** yapın.

## Fiyat tarifesi

Tüm otomatik hesaplar `vardiya/isakisi.py` içindeki `FIYAT` sözlüğünden gelir; tek yerden değişir.

| Kalem | Varsayılan |
|-------|-----------|
| Tek seferlik iş, profesyonel (kişi başı, müşteriden) | 4.800 ₺ |
| Tek seferlik iş, öğrenci (kişi başı, müşteriden) | 2.800 ₺ |
| Profesyonel yevmiyesi | 2.500 ₺ |
| Öğrenci yevmiyesi | 1.800 ₺ |
| Abonelik varsayılan kota | 4 |

Kullanıcı işe kendi tutarını söylerse (örn. "toplam 9600") tarife yerine o tutar kullanılır.
Abonelik paket ücreti tarifeden hesaplanmaz; asistan tutarı sorar, verilen tutar ilk kotaya yazılır.
Kota tüketildiğinde gelir yazılmaz, yalnızca personel yevmiyesi gider olarak işlenir.

Kuralları doğrulamak için: `python kural_testleri.py`

## HTTP API

```bash
pip install -r requirements-api.txt

$env:SUPABASE_HOST="..."; $env:SUPABASE_DB="postgres"; $env:SUPABASE_USER="..."
$env:SUPABASE_PASSWORD="..."; $env:GEMINI_API_KEY="..."; $env:PANEL_API_KEY="kendi-anahtarınız"

uvicorn api:app --host 0.0.0.0 --port 8000
```

Tüm isteklerde `X-API-Key` başlığı gerekir.

| Uç nokta | İş |
|----------|-----|
| `GET /saglik` | Bağlantı, kayıt sayıları, geçerli tarife |
| `POST /komut` | Doğal dil: `{"metin": "yarına Emir beye 2 profesyonel gidecek tek seferlik"}` |
| `POST /is` | AI'sız şemayla iş girişi (tutar verilmezse tarife uygulanır) |
| `POST /ertele` | `{"musteri": "Nazlı", "yeni_tarih": "haftaya perşembe"}` |
| `GET /gun/{tarih}` | Günün kadro/ciro/kâr özeti |
| `GET /ay?ay=9&yil=2026` | Ayın özeti |

`uygula: false` gönderilirse hiçbir şey yazılmaz; yalnızca yapılacak işlemlerin planı döner.

Panelin AI sekmesi ile API aynı çekirdeği (`vardiya/islemler.py`) kullanır: panelde işlemler
onay kuyruğuna düşer, API'de doğrudan uygulanır.

Kod içinden kullanmak için:

```python
from vardiya.asistan import Asistan

a = Asistan()
print(a.komut("nazlı hanımın rezervasyonunu haftaya perşembeye ertele")["cevap"])
print(a.is_ekle({"musteri": "Emir Kaya", "etiket": "abonelik", "pro_sayisi": 1}))
a.kapat()
```

## Güvenlik

- `secrets.toml` repoda olmamalı (`.gitignore`'da).
- **[auth]** bölümünde güçlü, birbirinden farklı şifreler kullanın.
- Supabase şifresini yalnızca Streamlit Secrets'ta tutun.
- GitHub reposunu **Private** yapın.
- Eski repodaki commit geçmişinde şifre kalmış olabilir — Supabase ve panel şifrelerini **değiştirin**.

### Veri görünürlüğü

| Panel | Veri erişimi |
|-------|----------------|
| `admin.py` | Tüm geçmiş |
| `mobil.py` | Son 1 hafta + tarihsiz bekleyen kotalar |
| `servis.py` | Son 1 hafta |
