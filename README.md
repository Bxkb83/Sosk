# Telegram mağaza botu

Python 3.12+ ve `aiogram` ile çalışan, Railway'de sürekli polling yapan Telegram botu. Bot; genel hizmet kataloğu, bakiye, 6 dakikalık sipariş iptali, promo kodları, Stars yükleme, referans ödülü ve admin onayı içerir.

**Sanal numara, telefon numarası kiralama veya SMS/OTP doğrulama kodu alma/gönderme özelliği yoktur.** Bu amaçla hizmet ekleme girişimleri reddedilir.

## Railway ve GitHub kurulumu

1. `telegram-store-bot` klasörünü GitHub deposuna ekleyin.
2. Railway'de bu depodan bir servis oluşturun ve **Root Directory** değerini `/telegram-store-bot` yapın.
3. `Variables` bölümünde `.env.example` içindeki değişkenleri tanımlayın. Gerçek bot tokenini yalnızca Railway Variables'a ekleyin.
4. Önceki token sohbet içinde açığa çıktığı için BotFather'dan yenisini alın; eski tokeni kullanmayın.
5. Botu zorunlu katılım kanalına ve log kanalına yönetici olarak ekleyin. Katılım kontrolünün çalışması için botun katılım kanalında yönetici olması gerekir.
6. SQLite verisinin redeploy sırasında kaybolmaması için Railway'de `/data` konumuna kalıcı volume bağlayın ve `DB_PATH=/data/bot.db` ayarlayın.
7. Telegram polling aynı bot tokeniyle tek çalışan örnek gerektirir; Railway'de replica sayısını `1` bırakın.
8. Deploy edin. Railway bu klasördeki `requirements.txt` dosyasından bağımlılıkları kurar; `railway.json` başlangıç komutunu tanımlar.

## Railway Variables

| Değişken | Gereklilik | Açıklama |
| --- | --- | --- |
| `BOT_TOKEN` | Zorunlu | BotFather'dan alınan, yeni ve gizli bot tokeni. |
| `ADMIN_IDS` | Zorunlu | Admin Telegram kullanıcı ID'leri; birden fazlaysa virgülle ayırın. |
| `FORCE_JOIN_CHANNEL` | İsteğe bağlı | Zorunlu kanal: public kanal için `@kanaladi`, private kanal için `-100...` ID. |
| `JOIN_CHANNEL_URL` | Private/numeric kanal için zorunlu | Kullanıcının katılacağı davet URL'si. |
| `LOG_CHANNEL_ID` | İsteğe bağlı | İşlem kayıtlarının gönderileceği kanalın `-100...` ID'si. |
| `PROMO_CHANNEL_ID` | İsteğe bağlı | Promoların otomatik duyurulacağı kanal. Bot bu kanalda yönetici olmalıdır. |
| `WELCOME_BONUS_TL` | İsteğe bağlı | Yeni kullanıcı bakiyesine bir defa eklenen hoş geldin bonusu; varsayılan `1`. |
| `VIP_PRICE_TL` | VIP satışında gerekli | VIP satın alma bedeli; boşsa kullanıcı VIP satın alamaz. Bakiye ile ödenir. |
| `VIP_DURATION_DAYS` | İsteğe bağlı | VIP satın alımının süresi; varsayılan `30`, sınır `1-365`. |
| `STAR_TOPUP_TIERS` | Stars ile bakiye için zorunlu | `Stars:TL,Stars:TL` biçimi. Örnek: `100:50,250:125`. Her oranı kendiniz belirleyin; bu örnek fiyat önerisi değildir. |
| `REFERRAL_BONUS_TL` | İsteğe bağlı | Davet edilen kişinin ilk siparişi tamamlanınca davet edene verilecek ödül; varsayılan `5`. |
| `BANK_TRANSFER_ENABLED` | Varsayılan kapalı | Yalnızca Telegram dışında teslim edilen uygun fiziksel/offline hizmetlerde `true` yapın. Bot içindeki dijital ürün/hizmet alımlarında banka havalesiyle bakiye yüklemeyin. |
| `PAYMENT_INSTRUCTIONS` | Banka havalesi açılırsa | Yalnızca yukarıdaki kapsam için kullanıcıya gösterilen ödeme talimatı. |
| `DB_PATH` | İsteğe bağlı | SQLite dosya yolu; Railway volume bağladıktan sonra `/data/bot.db` kullanın. |

Telegram'ın güncel ödeme kuralları gereği bot içindeki dijital ürün ve hizmet satışlarında Telegram Stars (`XTR`) kullanılmalıdır ([resmî dokümantasyon](https://core.telegram.org/bots/payments-stars)). Stars seçeneği, `STAR_TOPUP_TIERS` boş bırakılırsa devre dışı kalır. Stars ödemeleri, Telegram'ın onaylı ödeme güncellemesi geldikten sonra tek seferlik ve işlem kaydıyla bakiyeye eklenir.

## Admin akışları

Admin komutları yalnızca `ADMIN_IDS` listesindeki hesaplarda çalışır.

- `/addservice` — hizmet adı, fiyat ve açıklamayı sırayla sorar.
- `/delservice` — kapatılacak hizmet ID'sini sorar.
- `/addbalance` / `/removebalance` — kullanıcı adı veya Telegram ID'si, ardından tutarı sorar. Eski hızlı biçimler de geçerlidir: `/addbalance @kullanici 10`.
- `/promoadd` — promo kodu, bonus, toplam kullanım ve dakika süresini sırayla sorar.
- `/couponadd KOD YUZDE KULLANIM DAKIKA` — indirim kuponu oluşturur.
- `/promodel` — kapatılacak promo kodunu sorar.
- Sipariş bildirimi genel teslimat notu ve iade seçenekleri sunar; telefon numarası ve SMS/doğrulama kodu teslimatı desteklenmez.
- `/ban ID`, `/unban ID`, `/usernote ID NOT` — kullanıcı erişimini ve admin notlarını yönetir.
- `/broadcast` — onay ekranıyla toplu mesaj gönderir.
- `/tickets`, `/ticketreply ID YANIT`, `/ticketclose ID` — destek taleplerini yönetir.
- `/export` — kullanıcı, sipariş ve bakiye hareketlerini Excel dosyası olarak dışa aktarır.
- `/vipgrant ID GUN`, `/vipservice ID on|off`, `/flashsale ID YUZDE DAKIKA` — VIP ve süreli indirimleri yönetir.
- `/stats` — günlük ve toplam kullanıcı/sipariş analizini gösterir.
- `/userinfo @kullanici` veya `/userinfo ID`
- `/totalusers`, `/pendings`, `/deposits`, `/stats`, `/admin`
- Herhangi bir adımda `/cancel` ile akış iptal edilir.

Kullanıcı komutları: `/start`, `/help`, `/services`, `/balance`, `/stars`, `/history`, `/balancehistory`, `/daily`, `/promo KOD`, `/coupon KOD`, `/referral`, `/leaderboard`, `/transfer ID TUTAR`, `/ticket MESAJ`, `/vip`, `/vipbuy`, `/mystery`, `/badges`, `/missions`, `/language tr|en`.

Yeni promosyon, `PROMO_CHANNEL_ID` ayarlıysa kanala otomatik gönderilir; kullanım sayacı güncellenir ve promosyon dolunca ya da süresi geçince duyuru kaldırılır. Süre, dakikaya göre belirlenen bitiş zamanı ile takip edilir; arka plan görevi dakikada bir sona eren kayıtları işler.

Ek özellikler: ilk kayıt hoş geldin bonusu, davet liderlik tablosu, sayfalı sipariş/bakiye geçmişi, kullanıcılar arası bakiye transferi, destek talepleri, sipariş puanlaması, kullanıcı başına 30 saniye mesaj sınırı ve olağan dışı transfer uyarıları. Yüzde kuponları sonraki siparişe uygulanır; VIP ve flash indirimler birlikte birikmez, geçerli en yüksek indirim seçilir. Gizemli kutu ve haftalık görevler yalnızca nakde çevrilemeyen puan verir. Dil seçimi başlangıç/menü/yardım metinlerini destekler; tüm işlem mesajlarının çevirisi henüz tamamlanmamıştır. Admin broadcast ve ban/not özellikleri yalnızca `ADMIN_IDS` kullanıcılarına açıktır.

Referans ödülü sadece davet edilen kullanıcının ilk siparişi admin tarafından tamamlandığında eklenir. Kayıt/sipariş verileri SQLite'ta tutulur; bot yeni bir Railway örneği veya kaybolan volume ile açılırsa yerel DB dosyası da kaybolabilir.

## Yerel kontrol

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

Gerçek Telegram hesabıyla test etmek için Railway'e geçerli Variables eklenmelidir; kaynak kodda token saklamayın.
