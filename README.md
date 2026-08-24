# Vardiya Panel

Temizlik işletmesi vardiya yönetimi — Supabase PostgreSQL + Streamlit.

## Dosya yapısı

```
vardiya-panel/
├── admin.py              # Admin paneli (finans, analiz, takvim, AI)
├── mobil.py              # Mobil yönetici paneli
├── servis.py             # Servis ekibi listesi (salt okunur)
├── vardiya/
│   ├── db.py             # Veritabanı ve iş mantığı
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
| `mobil.py` | Dün ve sonrası (+ tarihsiz bekleyen kotalar) |
| `servis.py` | Dün ve sonrası |
