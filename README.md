# Telegram siparis botu

Bot, Telegram'da hizmet listesini, kullanici bakiyelerini, siparisleri,
manuel bakiye taleplerini ve promosyonlari yonetir. Calistirmak icin Python
3.11+ gerekir.

## Railway + GitHub kurulumu

1. Bu dosyalari GitHub deposunun kok dizinine ekleyip push edin.
2. Railway'de **New Project → Deploy from GitHub Repo** ile depoyu secin.
   `requirements.txt` ve `railway.json` otomatik algilanir; bot `python main.py`
   komutuyla worker olarak baslar. Web portu acmasi gerekmez.
3. Railway servisinde **Variables** bolumune asagidaki degiskenleri ekleyin.
   Bot tokenini GitHub'a, bu sohbete veya kaynak koda koymayin.
4. Railway servisinde bir **Volume** olusturup mount path'i `/data` yapin;
   `DB_PATH` degerini `/data/bot.db` olarak ayarlayin. Volume olmadan SQLite
   veritabani yeniden baslatma/yeni deploy sirasinda kaybolabilir.
5. Deploy loglarinda `Telegram bot connected` satirini ve servis durumunu
   kontrol edin.

## Railway Variables

| Degisken | Gerekli | Deger |
| --- | --- | --- |
| `BOT_TOKEN` | Evet | BotFather'dan alinan bot tokeni. Railway'in Variables alanina secret olarak girin. |
| `ADMIN_IDS` | Evet | Admin Telegram kullanici ID'leri; birden fazlaysa virgul ile: `123456789,987654321`. |
| `DB_PATH` | Evet (kalici veri icin) | Volume mount'u `/data` ise `/data/bot.db`. |
| `FORCE_JOIN_CHANNEL` | Istege bagli | Katilim zorunlu kanali: `@kanaladi` veya `-100...` kanal ID'si. |
| `JOIN_CHANNEL_URL` | Ozel kanal icin gerekli | Kullaniciya gosterilecek davet linki. Herkese acik `@kanaladi` icin bos birakilabilir. |
| `LOG_CHANNEL_ID` | Istege bagli | Islem kayitlarinin gidecegi kanal ID'si; genellikle `-100...`. |
| `PAYMENT_INSTRUCTIONS` | Istege bagli | Bakiye yukleme ekraninda gosterilen odeme aciklamasi. |
| `LOG_LEVEL` | Istege bagli | Varsayilan `INFO`. |

### Kanal izinleri

- Zorunlu katilim kontrolu icin botu katilim kanalina **yonetici** ekleyin.
- Log kanali kullanacaksaniz botu log kanalina **mesaj gonderebilen yonetici**
  olarak ekleyin.
- Bot acilirken kanal erisimi kontrol edilir. Kanal ID'si veya yetkiler yanlissa
  Railway deploy logunda nedenini belirten hata gorunur.

## Yerelde calistirma

```bash
python -m venv .venv
source .venv/bin/activate  # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env
```

`.env` dosyasina gercek `BOT_TOKEN` ve `ADMIN_IDS` degerlerini girip su komutla
baslatin:

```bash
set -a && source .env && set +a
python main.py
```

`.env` Git'e eklenmez. Railway'e yukaridaki degerleri Variables bolumunden
girin; kaynak koda token yapistirmayin.

## Admin komutlari

`/admin` komutuyla komut listesini gorun. Hizmet ekleme ornegi:

```text
/addservice Tasarim|250|Logo tasarimi
```

Kullanicilar botla ilk kez konusarak kaydolur. Manuel odeme talepleri,
yoneticinin Telegram'da onaylamasindan sonra bakiyeye eklenir.
