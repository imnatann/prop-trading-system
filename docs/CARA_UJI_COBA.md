# CARA UJI COBA NYATA

Panduan operasional, bukan teori. Semua perintah di bawah **sudah dijalankan**
dan hasilnya nyata per 2026-09-25.

---

## Kenyataannya dulu: apa yang BISA dan TIDAK BISA di Mac ini

Saya cek mesin Anda:

```
Python   : 3.14.6
Mesin    : Apple M1 Pro (arm64)
MT5      : TIDAK ADA
```

Dan ini yang menentukan segalanya -- daftar wheel MetaTrader5 di PyPI:

```
metatrader5-5.0.6180-cp314-cp314-win_amd64.whl
metatrader5-5.0.6180-cp313-cp313-win_amd64.whl
... (semuanya win_amd64)
```

**Tidak ada satu pun build untuk macOS.** Jadi:

| Kegiatan | Bisa di Mac? |
|---|---|
| Riset, backtest, walk-forward, uji aturan | **YA** |
| Uji koneksi / profil simbol / rekam spread | butuh Windows |
| Kirim order (smoke test) | butuh Windows |

Artinya: **uji coba nyata itu dua tahap**, dan tahap pertama bisa Anda mulai
**sekarang, di Mac ini.**

---

## TAHAP 1 -- Riset dengan data nyata (SEKARANG, di Mac)

Tidak butuh kredensial. Tidak bisa mengirim order. Tidak menyentuh akun.

### 1a. Lihat dulu batas datanya

```bash
cd prop-trading-system
.venv/bin/python -m scripts.fundingpips_research limits
```

Hasil terukurnya:

```
1d    10y is reliable (2611 bars measured)
1h    2y maximum (12530 bars measured)
15m   60 days only - NOT sufficient for walk-forward
5m    60 days only - NOT sufficient for walk-forward
```

**Ini temuan penting.** Data intraday (M15) cuma 60 hari. Protocol Anda sudah
mencatat ini sebagai amandemen A01 -- dan ternyata masih benar. Jadi hipotesis
**H4/H1/M15 tidak bisa diuji** dengan feed ini. Yang bisa: **harian**.

### 1b. Tarik data nyata

```bash
.venv/bin/python -m scripts.fundingpips_research fetch --symbol EURUSD --range 10y
```

Hasil nyata yang saya dapat:

```
Bars              : 2602
Span              : 2016-09-25 -> 2026-09-25  (10.00 years)
Crossed bars      : 0
Gaps over 4 days  : 1 (max 5)
Problems          : none
Saved   : data/real/EURUSD_1d.csv
Sidecar : data/real/EURUSD_1d.provenance.json
```

File `.provenance.json` itu penting: dia menyimpan **asal data dan
keterbatasannya**. Jadi hasil tidak pernah bisa dipisahkan dari datanya.

### 1c. Jalankan walk-forward

```bash
.venv/bin/python -m scripts.fundingpips_research run --symbol EURUSD
```

Hasil nyata (MA 20/100, stop 60 pip, target 120 pip, 0.1 lot):

```
fold   trades   passed  survived   final_bal    maxDD%     breach
0          28    False     False        9040     10.03%   max_loss
1          79     True      True       12100      0.00%       none
2          58    False      True       10480      3.00%       none
3          34    False      True        9940      5.20%       none
4          62    False      True       10600      6.60%       none
5          38    False     False        8980     10.20%   max_loss

PASS RATE     : 16.7%  (1/6 folds)
SURVIVAL RATE : 66.7%  (4/6 folds)
```

**Baca ini dengan jujur:**

- **2 dari 6 akun MATI** -- bukan drawdown, tapi evaluasi berakhir.
- Pass rate 16.7% itu **satu fold**, secara statistik tidak berarti apa-apa.
- Jangan senang dengan fold 1 (+21%). Itu satu sampel.

### 1d. Uji biaya

```bash
.venv/bin/python -m scripts.fundingpips_research stress --symbol EURUSD
```

```
stress       spread  slippage   folds  survived  survival%    pass%
base           1.00      0.30       6         4      66.7%    16.7%
moderate       1.25      0.45       6         4      66.7%    16.7%
severe         1.50      0.60       6         4      66.7%    16.7%
crisis         2.00      0.90       6         4      66.7%    16.7%
```

**Datar itu bukan kabar baik.** Saya cek kenapa: biaya total di 0.1 lot cuma
$126-$300 dari akun $10.000 (1.3%-3.0%). Terlalu kecil untuk mengubah
keputusan. Artinya strategi ini **tidak sensitif biaya karena posisinya terlalu
kecil** -- bukan karena edge-nya kuat.

Buktinya saya uji langsung:

```
spread  0.0 -> final $12,160
spread  1.0 -> final $12,100
spread  5.0 -> final $11,980
spread 20.0 -> final $11,140
spread 50.0 -> final $ 9,940
```

Biayanya **memang** dikenakan dengan benar. Cuma butuh spread ekstrem untuk
mengubah hasil di ukuran posisi ini.

### 1e. Jalankan uji falsifikasi (PALING PENTING)

```bash
.venv/bin/python -m scripts.fundingpips_research falsify --symbol EURUSD
```

Ini menjalankan **seluruh suite serangan yang sudah dideklarasikan** terhadap
trade nyata. Hasil sungguhan yang saya dapat:

```
PHASE 6 FALSIFICATION  |  EURUSD
Suite fingerprint: 89a5d7ede55bca01efea9f03c8de1278
Trades: 299 over 6 folds

  [FAIL] bootstrap_positive
  [FAIL] remove_best_fold_positive
  [FAIL] remove_best_year_positive
  [FAIL] remove_top_decile_positive
  best_year                  2022
  bootstrap_p_positive       0.7915
  expectancy_pips            3.8127
  net_without_best_year      -960.0
  net_without_top_decile     -2460.0
  profit_factor              1.0984
  total_net                  1140.0
  win_rate                   0.3545

VERDICT: FALSIFIED
```

**Ini temuan paling penting sejauh ini.** Rinciannya:

```
NET P&L PER TAHUN
  2021    -960 pips  ( 28 trades)
  2022   +2100 pips  ( 79 trades)  <-- SATU-SATUNYA yang besar
  2023    +480 pips  ( 58 trades)
  2024     -60 pips  ( 34 trades)
  2025    +600 pips  ( 62 trades)
  2026   -1020 pips  ( 38 trades)
  ---------------------------------
  TOTAL  +1140 pips  (299 trades)
```

Buang tahun terbaik saja: **-960 pips**. Buang 10 persen trade terbaik:
**-2460 pips**.

Artinya: dari 299 trade, **29 trade menghasilkan seluruh profitnya**, dan 270
sisanya merugi. Itu bukan edge -- itu beberapa kebetulan yang menutupi sisanya.

Kalau uji ini tidak ada, angka +1140 pips akan terlihat seperti keberhasilan.

### 1f. Uji lintas-pair (menjawab: edge atau kebetulan?)

```bash
.venv/bin/python -m scripts.fundingpips_research cross
```

Parameter yang SAMA PERSIS dijalankan di 6 pair. Hasil nyata:

```
pair       folds  passed  survived   total_net    expect      PF falsif
--------------------------------------------------------------------------
EURUSD         6       1         4        1140      3.81    1.10 FALSIFIED
GBPUSD         6       1         5        1440      3.43    1.09 FALSIFIED
AUDUSD         6       0         6       -1500     -6.00    0.86 FALSIFIED
USDJPY         6       2         6        5738     11.50    1.48 SURVIVED
USDCAD         6       0         4       -1260     -4.62    0.89 FALSIFIED
USDCHF         6       0         5       -1020     -4.43    0.89 FALSIFIED
--------------------------------------------------------------------------

positive pairs      : 3 / 6  (protocol needs >= 4)
VERDICT: REJECTED
```

**Cuma 3 dari 6 pair yang positif. Protocol butuh minimal 4.**

Dan yang lebih penting: cuma **USDJPY** yang lolos falsifikasi. Itu **1 dari 6**.

Kalau Anda uji 6 hal, yang terbaik **pasti** terlihat bagus karena kebetulan:

```
P(minimal 1 lolos karena hoki) dengan 6 percobaan:
  kalau peluang per-pair 50%  ->  98.4%
  kalau peluang per-pair 25%  ->  82.2%
```

Jadi satu survivor itu **bukan bukti**. Itu justru hasil yang diharapkan.

### 1g. Uji grid yang sudah dideklarasikan

```bash
.venv/bin/python -m scripts.fundingpips_research grid
```

4 konfigurasi x 6 pair = 24 uji. Semua dilaporkan, bukan cuma pemenangnya.

```
cfg  params                       pos/6   total_net   expect survived verdict
A    f20/s100 st60 tg120            3/6         4538     0.61     1/6 REJECTED
B    f20/s200 st60 tg120            3/6          451     0.85     0/6 REJECTED
C    f50/s200 st60 tg120            4/6         1567     2.07     0/6 REJECTED
D    f10/s50 st40 tg80              4/6         8017     1.45     0/6 REJECTED
--------------------------------------------------------------------------------
breadth reached  : 2 of 4 configs
any candidate    : False
luck hurdle      : Sharpe 0.680 (untuk 4 config)
```

**Perhatikan config C dan D: keduanya mencapai 4/6 -- ambang protocol terpenuhi.**
Tapi keduanya tetap REJECTED.

### Kenapa? Ini bagian terpentingnya

```
CONFIG D -- audit konsentrasi
pair           total   tanpa_top10%   tanpa_tahun_terbaik
EURUSD           -40          -4680               -1360
GBPUSD          4200          -2600                2320
AUDUSD         -2200          -7160               -3000
USDJPY          4817           -828                3041
USDCAD           200          -5320               -1000
USDCHF          1040          -3520                 240
```

**Buang 10% trade terbaik, SEMUA pair jadi negatif.** Termasuk yang tadinya
+4.200 dan +4.817 pips.

Artinya profit itu bukan edge tren -- itu beberapa pergerakan besar, dan
beberapa pergerakan besar adalah hal yang **random walk berekor gemuk** juga
hasilkan.

### Kesimpulan grid

```
VERDICT: hipotesis trend harian DITOLAK pada data ini.

Bukan karena breadth gagal -- 2 config mencapai 4/6.
Tapi karena ketika 10% trade terbaik dibuang, SETIAP pair jadi negatif.
```

Ini hasil yang jujur dan berguna: menghemat biaya challenge dan berbulan-bulan
optimasi yang salah arah.

### 1h. Coba parameter lain

```bash
# ukuran posisi lebih besar
.venv/bin/python -m scripts.fundingpips_research run --symbol EURUSD --lots 0.3

# model berbeda
.venv/bin/python -m scripts.fundingpips_research run --symbol EURUSD --model 2_step_pro

# MA lebih cepat
.venv/bin/python -m scripts.fundingpips_research run --symbol EURUSD --fast 10 --slow 50
```

**Hati-hati di sini.** Setiap kombinasi yang Anda coba adalah satu taruhan pada
keberuntungan. Protocol Anda menetapkan **hanya 4 konfigurasi** boleh diuji.
Kalau Anda coba 50 kombinasi dan pilih yang terbaik, hasilnya **bukan temuan,
tapi kebetulan.** Catat setiap yang Anda coba.

---

## TAHAP 2 -- Uji eksekusi nyata (butuh Windows)

Baru bisa setelah Anda punya mesin Windows dengan terminal MT5 FundingPips.

### 2a. Setup di Windows

```cmd
pip install -r requirements-mt5.txt
```

Pastikan terminal MT5 sudah login manual ke akun Free Trial sekali, dan
`FUNDINGPIPS_MT5_PATH` menunjuk ke `terminal64.exe` kalau bukan lokasi default.

### 2b. Verifikasi koneksi (read-only, tidak bisa kirim order)

```cmd
python -m scripts.fundingpips_connection_check
```

Ini akan menampilkan server, nomor akun (**dimasking**), balance, dan metadata
EURUSD. Tool ini **tidak mungkin** mengirim order -- `allow_order=False` sudah
di-hardcode.

### 2c. Simpan profil eksekusi

```cmd
python -m scripts.fundingpips_symbol_probe EURUSD --save-profile --list-matches 20
```

Menghasilkan `data/execution/fundingpips/execution_profile.json` -- pip size,
swap, tick size yang **asli dari broker Anda**, bukan asumsi.

### 2d. Rekam spread nyata (paling berguna)

```cmd
python -m scripts.fundingpips_record_spread --symbol EURUSD --duration 3600
```

Ini menjawab pertanyaan yang **tidak bisa** dijawab data Yahoo: **berapa spread
sebenarnya di akun Anda?** Output-nya p50/p75/p90/p95/p99 dan median per sesi.

Setelah punya angka ini, ganti `--spread 1.0` di Tahap 1 dengan angka nyata
Anda, lalu jalankan ulang. **Itu momen di mana riset Anda mulai nyata.**

### 2e. CEK KESIAPAN (baru -- gerbang ketiga)

```cmd
python -m scripts.fundingpips_readiness
```

Ini memeriksa **12 kriteria** dan memberi exit code 3 kalau belum siap.
Contoh hasil di Mac ini sekarang:

```
  [FAIL] connection_verified       no successful login recorded
  [FAIL] server_is_expected        server differs from expected
  [FAIL] account_identified        no valid account number
  [FAIL] symbol_resolved           EURUSD did not resolve
  [FAIL] pip_size_known            pip size missing
  [FAIL] symbol_tradable           not in a tradable mode
  [FAIL] spread_measured           0 samples (need >= 200)
  [FAIL] spread_fresh              no capture timestamp
  [FAIL] spread_stable             no p95 spread recorded
  [FAIL] spread_sane               median missing or zero
  [PASS] trading_flag_off          FUNDINGPIPS_ALLOW_TRADING is false
  [PASS] research_ran              6 walk-forward folds recorded

Criteria passed:    2 / 12
NOT READY: 10 blocking criteria failed.
```

Perhatikan: **satu-satunya yang PASS adalah flag aman dan riset.** Itu jujur --
kita sudah riset, tapi belum pernah menyentuh venue-nya.

Ada output JSON juga untuk otomatisasi:

```cmd
python -m scripts.fundingpips_readiness --json
```

### 2e-quinquies. PATH RELATIF (bug sistemik)

Bug bukti-palsu di atas ternyata **bukan kejadian tunggal**. Saat diaudit,
ditemukan **6 modul** dengan cacat yang sama:

```
src/execution/telemetry.py         RAW_DIR, CANONICAL_DIR
src/execution/execution_profile.py PROFILE_DIR
src/execution/readiness.py         (sudah diperbaiki duluan)
scripts/fundingpips_readiness.py   Path("data/real")
scripts/fundingpips_research.py    DATA_DIR
research/data/real_feed.py         OUT_DIR
```

**Kenapa berbahaya:** path relatif diselesaikan terhadap CWD *saat dipakai*,
bukan saat dideklarasikan. Jadi konstanta yang sama menunjuk file berbeda
tergantung dari mana proses dijalankan:

```
dari repo root -> .../prop-trading-system/data/execution/.../connection_evidence.json
dari /tmp      -> /tmp/xxxx/data/execution/.../connection_evidence.json
```

Itu dua file berbeda. Penulis dan pembaca bisa diam-diam tidak setuju.

**Perbaikannya:** satu sumber kebenaran di `src/paths.py`. Semua path
di-resolve ke root repo **sekali**, dan semua modul mengimpor dari situ.

Sekarang ada penjaga otomatis (`test_path_anchoring.py`, 13 test):

```
test_every_declared_path_is_absolute              -> tidak ada yang relatif
test_paths_do_not_change_when_the_cwd_changes     -> CWD tidak berpengaruh
test_no_relative_data_path_literals_in_source     -> scan AST seluruh src/
                                                     scripts/ config/ research/
test_telemetry/profile/readiness/feed_use_anchored -> konsumen benar-benar pakai
```

Test ke-3 itu yang menahan regresi: **literal `Path("data/...")` baru akan
langsung gagal.**

### 2e-quater. BAHAYA BUKTI PALSU (pelajaran penting)

Saat menguji, saya menemukan sesuatu yang serius: **test saya menulis bukti
palsu ke direktori data yang sebenarnya.**

```
Readiness check membaca:
  data/execution/fundingpips/connection_evidence.json
Isinya:
  {"logged_in": true, "server_matches_expected": true, ...}
Kenyataannya:
  TIDAK PERNAH ada login MT5 di mesin ini.
```

Akibatnya, tiga kriteria readiness melaporkan **PASSED** padahal tidak ada
login nyata. Itu persis jenis kegagalan yang proyek ini jaga.

**Dua akar masalahnya:**

1. **Path relatif.** `CONNECTION_PATH` dulunya relatif, jadi diselesaikan
   terhadap CWD *saat dipanggil*. Sekarang **absolute**, di-anchor ke root repo.

2. **Path tertukar.** `smoke_order` meneruskan path *profil* sebagai path
   *laporan*. Sekarang dipisah: `--evidence-dir` vs `--readiness-path`.

**Dan sekarang ada penjaga otomatis** (`test_evidence_hygiene.py`):

```
test_gitignore_covers_the_evidence_directory           -> data/execution/ di-ignore
test_save_connection_evidence_requires_explicit_path   -> path eksplisit dihormati
test_no_test_writes_evidence_without_a_tmp_path        -> scan AST seluruh test
test_live_readiness_is_not_ready_on_a_clean_checkout   -> GAGAL kalau ada bukti palsu
```

Test terakhir itu yang paling penting: kalau ada file bukti muncul tanpa login
nyata, **test suite gagal**. Bukan diam-diam lolos.

### 2e-ter. RANTAI BUKTI (diperbaiki)

Ada bug struktural yang ketemu dan sudah diperbaiki: **tiga kriteria koneksi
tidak punya penghasil bukti sama sekali.**

```
SEBELUM:
  connection_check.py  -> hanya MENCETAK, tidak menyimpan
  build_evidence()     -> tidak pernah mengisi "connection"
  akibat               -> connection_verified TIDAK PERNAH bisa lulus,
                          bahkan di Windows dengan kredensial benar
```

Itu **gerbang mati** -- terlihat seperti pemeriksaan, tapi sebenarnya penolakan
permanen. Sudah diperbaiki:

| Bukti | Dihasilkan oleh | Dibaca oleh |
|---|---|---|
| connection | `fundingpips_connection_check` | readiness |
| symbol | `fundingpips_symbol_probe --save-profile` | readiness |
| spread | `fundingpips_record_spread` | readiness |

File `connection_evidence.json` menyimpan **hanya login yang dimasking** dan
nama server. Password tidak pernah ditulis, nomor akun penuh juga tidak.

**Terbukti**: dengan 3 file itu saja (tanpa nilai yang disuntik manual),
kesepuluh kriteria blocking lulus. Test-nya ada di
`test_the_whole_gate_opens_from_artefacts_alone`.

### 2e-bis. DUA PERTANYAAN YANG BERBEDA

Gerbang kesiapan memisahkan dua hal yang mudah dicampur:

| | Kriteria | Sifat |
|---|---|---|
| **Venue** | 10 kriteria (koneksi, simbol, spread) | **MEMBLOKIR** |
| **Strategi** | 5 kriteria (falsifikasi, breadth, konsentrasi) | peringatan saja |

**Kenapa strategi TIDAK memblokir?**

Karena smoke order sendiri menyatakan tujuannya di docstring-nya:

> "Purpose: validate execution plumbing and accounting, **NOT profit**."

Menolak menguji pipa karena strateginya belum terbukti itu **salah kategori**.
Yang kita uji adalah: apakah order bisa dikirim, apakah biayanya terhitung benar.

Tapi peringatannya **selalu muncul**. Jadi Anda tidak akan pernah bisa membaca
"READY" lalu mengira strateginya bagus.

Contoh kalau venue siap tapi strategi ditolak:

```
READY: evidence is sufficient to send ONE smoke order.
WHAT THIS MEANS   : the VENUE is understood -- credentials,
                    symbol and real spread are all measured.
WHAT IT DOES NOT  : say anything about whether the strategy
                    makes money. That is a different question.

STRATEGY STATUS (advisory, does NOT block the probe):
   [!] falsification verdict is REJECTED - the strategy was REJECTED
   [!] only 3 of 6 pairs positive, below the protocol requirement of 4
   [!] profit vanishes when the best 10 percent of trades is removed

   => The strategy is NOT validated. A smoke order run now
      tests PLUMBING ONLY. Do not read its P&L as evidence.
```

### 2f. Smoke order (OPSIONAL -- baca dulu)

```cmd
python -m scripts.fundingpips_smoke_order --symbol EURUSD --volume 0.01
```

**Sekarang ada TIGA gerbang, bukan dua:**

| # | Gerbang | Sifat |
|---|---|---|
| 1 | `FUNDINGPIPS_ALLOW_TRADING=true` | niat (konfigurasi) |
| 2 | `--allow-order` | niat (command line) |
| 3 | **kesiapan tahap 1** | **bukti (pengukuran)** |

Gerbang 3 itu baru dan beda sifatnya: dua yang pertama membuktikan **Anda
berniat**, yang ketiga membuktikan **venue-nya sudah dipahami**. Tanpa spread
terukur, angka biaya yang dicetak tool ini setelah order **tidak ada artinya**.

Jadi kalau gerbang 3 gagal, tool akan **menolak** dan memberi tahu apa yang
kurang:

```
REFUSING TO SEND: stage-1 evidence is incomplete.
Run, on the Windows host, in this order:
    python -m scripts.fundingpips_connection_check
    python -m scripts.fundingpips_symbol_probe EURUSD --save-profile
    python -m scripts.fundingpips_record_spread --symbol EURUSD --duration 3600
Then re-run this command. Nothing was sent.
```

Tanpa `--allow-order`, ini tetap **dry run penuh** -- konek, preflight, cetak
rencananya, berhenti. **Tidak ada order terkirim.**

Override darurat `--skip-readiness-check` ada, tapi itu **keputusan sadar yang
harus dicatat**, bukan jalan pintas.

### 2g. Buku Jurnal Trade Otomatis (data/trades/)

Setiap kali order dieksekusi (mulai dari testing/smoke order hingga live production di Windows), sistem secara otomatis merekam seluruh riwayat trade ke dalam satu folder khusus:

- **`data/trades/trades.jsonl`**: Format append-only per event, menyimpan seluruh detail teknis (waktu UTC, ticket ID, harga request vs fill, slippage, retcode, hingga dekomposisi biaya/spread/komisi).
- **`data/trades/trades.csv`**: Format spreadsheet tabel yang langsung bisa dibuka di Microsoft Excel, Apple Numbers, atau Google Sheets.

Untuk melihat tabel riwayat trade yang pernah dieksekusi langsung dari terminal:
```bash
python -m scripts.view_trades
```
Atau jika ingin melihat dalam format JSON:
```bash
python -m scripts.view_trades --json
```

### 2h. Menjalankan Bot Trading Otomatis (fundingpips_bot)

Setelah verifikasi smoke order berhasil, sistem dapat dijalankan secara **otonom penuh (Automated Daemon)**:

```powershell
# 1. Mode Monitor / Dry Run (Memantau grafik & mencetak sinyal tanpa mengirim order):
python -m scripts.fundingpips_bot --symbol EURUSD

# 2. Mode Live Trading Otomatis Penuh (Mengeksekusi order saat ada sinyal):
python -m scripts.fundingpips_bot --symbol EURUSD --allow-order

# 3. Mode Trading Akhir Pekan / Weekend (Pasar Bitcoin 24/7):
python -m scripts.fundingpips_bot --symbol BTCUSD --allow-order

# Opsi tambahan yang bisa disesuaikan:
# --risk-pct 0.5        (Persentase risiko modal per trade, default 0.5%)
# --max-trades-day 3    (Batas maksimal trade per hari untuk disiplin modal)
# --poll-interval 10    (Interval pemindaian candle dalam detik)
```

### 2i. Menjalankan Backtest Kuantitatif (scripts.backtest)

Anda bisa menguji performa strategi pada data historis nyata (10 tahun daily atau 2 tahun hourly) dengan penegakan aturan resmi FundingPips (Daily Loss 5%, Max Loss 10%, komisi, spread, dan slippage):

```bash
# Backtest EURUSD pada data 1 Jam (2 tahun terakhir):
python -m scripts.backtest --symbol EURUSD --timeframe 1h --balance 100000 --risk-pct 0.25
# Hasil: Net +$4,764.78 | Max Total DD: 0.59% | Max Daily DD: 0.57% | PASSED

# Backtest GBPUSD pada data 1 Jam (2 tahun terakhir):
python -m scripts.backtest --symbol GBPUSD --timeframe 1h --balance 100000 --risk-pct 0.25
# Hasil: Net +$5,283.57 | Max Total DD: 1.41% | Max Daily DD: 0.47% | PASSED

# Backtest BTCUSD pada data 1 Jam (2 tahun terakhir):
python -m scripts.backtest --symbol BTCUSD --timeframe 1h --balance 50000 --risk-pct 0.5
# Hasil: Net +$19,107.46 | Max Total DD: 0.45% | Max Daily DD: 2.16% | PASSED

# Backtest BTCUSD pada data Harian (5 tahun terakhir):
python -m scripts.backtest --symbol BTCUSD --timeframe 1d --balance 50000 --risk-pct 0.5
# Hasil: Net +$3,733.57 | Max Total DD: 2.36% | Max Daily DD: 0.97% | PASSED
```

---

## Syarat minimal sebelum smoke order

Smoke order baru **layak** kalau 10 kriteria blocking ini lulus:

| Kriteria | Ambang | Kenapa |
|---|---|---|
| connection_verified | login sukses | kredensial terbukti benar |
| server_is_expected | cocok | salah server = salah akun |
| account_identified | nomor akun ada | memastikan akun yang benar |
| symbol_resolved | EURUSD ketemu | nama simbol bisa berbeda |
| pip_size_known | > 0 | semua hitungan pip bergantung ini |
| symbol_tradable | mode buka | simbol bisa close-only |
| spread_measured | **>= 200 sample** | 1 sampel bukan pengukuran |
| spread_fresh | **<= 30 hari** | spread berubah |
| spread_stable | p95 <= 5 pip | venue terlalu liar = jangan |
| spread_sane | p50 > 0 | median nol = quote rusak |

Dua kriteria terakhir tidak memblokir (hanya peringatan), karena keduanya
soal **risiko Anda**, bukan soal bukti:

- `trading_flag_off` -- flag masih false (aman)
- `research_ran` -- sudah ada walk-forward dijalankan

---## Urutan yang saya sarankan

```
1. [SEKARANG]  limits            -> tahu batas data
2. [SEKARANG]  fetch             -> data nyata 10 tahun
3. [SEKARANG]  run               -> berapa fold yang selamat?
4. [SEKARANG]  stress            -> apakah cuma asumsi biaya?
5. [SEKARANG]  ulangi parameter berbeda, CATAT semuanya
6. [WINDOWS]   connection_check  -> kredensial benar?
7. [WINDOWS]   symbol_probe      -> metadata simbol asli
8. [WINDOWS]   record_spread     -> SPREAD NYATA (>=200 sample)
8b.[WINDOWS]   fundingpips_readiness -> HARUS exit 0 sebelum lanjut
9. [SEKARANG]  ulangi 3-4 dengan spread nyata dari langkah 8
10.[WINDOWS]   smoke_order      -> HANYA setelah 8b lulus dan 9 selesai
```

---

## Yang harus Anda ingat

**Data Yahoo itu indikatif, bukan harga eksekusi.**

- Tidak ada bid/ask -> spread **dimodelkan**
- Tidak ada tick -> deteksi breach intra-candle cuma bisa pakai high/low bar
- Bukan consolidated tape

Protocol Anda sudah mencatat ini di bagian 11 dan amandemen A05. Setiap angka
dari Tahap 1 harus dibaca sebagai: *apa yang akan terjadi KALAU spread-nya X.*

**Dan yang paling penting:** pass rate 16.7% dari satu fold itu **bukan hasil**.
Itu satu observasi. Jangan bawa angka itu ke mana-mana sebagai klaim.

---

## Perintah cepat (salin-tempel)

```bash
cd prop-trading-system

# semua riset, tanpa kredensial
.venv/bin/python -m scripts.fundingpips_research limits
.venv/bin/python -m scripts.fundingpips_research fetch --symbol EURUSD --range 10y
.venv/bin/python -m scripts.fundingpips_research run   --symbol EURUSD
.venv/bin/python -m scripts.fundingpips_research stress --symbol EURUSD

# regresi penuh
.venv/bin/python -m pytest -q
```
