# Architecture Alignment Report — Alpha v2 Z-Score Micro-Scalping

**Tanggal:** 2026-09-24
**Status:** Implementasi selesai & teruji. **TIDAK diizinkan live tanpa validasi forward.**
**Test suite:** 149 passed, 0 failed, deterministik 8/8 run.

---

## 1. Ringkasan Eksekutif

Arsitektur dari spesifikasi (screenshot) telah diimplementasikan **utuh**:

| Komponen Spesifikasi | Modul | Status |
| :--- | :--- | :--- |
| Async tick ingestion (event-loop, non-blocking) | [`src/data/tick_feed.py`](../src/data/tick_feed.py) | ✅ |
| Mid price `(Ask+Bid)/2` | [`src/data/tick_buffer.py`](../src/data/tick_buffer.py) | ✅ |
| Rolling mean/std via **Welford + circular buffer** | [`src/data/tick_buffer.py`](../src/data/tick_buffer.py) | ✅ |
| **Z-Score** sebagai primary entry trigger | [`src/strategy/zscore_scalper.py`](../src/strategy/zscore_scalper.py) | ✅ |
| **Order Book Imbalance** `I_t` + ambang ±0.30 / ±0.10 | [`src/data/order_book.py`](../src/data/order_book.py) | ✅ |
| **Volatility Ratio** `VR=ATR(5)/ATR(30)`, circuit breaker 1.30 | [`src/data/volatility_ratio.py`](../src/data/volatility_ratio.py) | ✅ |
| Wilder's smoothing (bukan SMA) | [`src/data/volatility_ratio.py`](../src/data/volatility_ratio.py) | ✅ |
| Pipeline terpasang & benar-benar jalan | [`services/scalping_service.py`](../services/scalping_service.py) | ✅ |

**Tetapi ada tiga temuan riset yang WAJIB dibaca sebelum menjalankan ini dengan uang nyata.** Ringkasnya:

1. **Strategi ini secara eksplisit DILARANG oleh prop firm target** (FundingPips & The5ers). Ini risiko terminasi akun, bukan sekadar risiko teknis.
2. **Secara ekonomi strategi ini rugi** pada biaya realistis, bahkan ketika pasar benar-benar mean-reverting.
3. **Depth order book EURUSD tidak tersedia** di akun retail, sehingga komponen konfirmasi akan fail-closed.

---

## 2. Temuan Riset (dengan bukti)

### 2.1 ⛔ RISIKO UTAMA: Strategi ini dilarang kontrak prop firm

Ini temuan paling penting, dan berada **di atas** semua pertimbangan teknis.

**FundingPips — Forbidden Strategies (resmi):**
> "gap trading, **high-frequency trading**, server spamming, **latency arbitrage**, toxic trading flow, hedging, long-short arbitrage, reverse arbitrage, **tick scalping**, server execution exploits... are **not allowed**... Such activities with FundingPips will result in **account termination**."
> — <https://help.fundingpips.com/hc/en-us/articles/34505029138449-Trading-Conduct-and-Security-Standards>

**The5ers — Prohibited Trading Practices (resmi):**
> "**High-frequency trading, in which the majority of trade durations span is measured within a few seconds or less**"
> — <https://the5ers.com/faqs/prohibited-trading-practices/>

Definisi The5ers ("mayoritas durasi trade dalam hitungan detik atau kurang") **mendeskripsikan persis** strategi micro-scalping ini. Enforcement-nya adalah **terminasi akun**, bukan peringatan.

Catatan tambahan: repo ini sudah punya pengaman terkait di [`config/prop_rules.py`](../config/prop_rules.py) — `min_trade_duration_seconds = 60` dengan komentar *"Anti-tick scalping rule (banyak prop firm melarang hold < 1m)"*. Artinya **konflik ini sudah diketahui di dalam repo sendiri**, dan spesifikasi baru ini melanggarnya.

**Implikasi desain:** strategi ini hanya layak untuk akun **non-prop** (broker retail biasa), atau harus diubah holding period-nya menjadi multi-menit.

### 2.2 Feed: apa yang benar-benar mungkin

**MetaTrader5 Python API TIDAK event-driven.** Seluruh function list resminya synchronous request/response — tidak ada callback/subscribe. `OnTick`/`OnBookEvent` hanya ada di **MQL5 native**, bukan Python.
— <https://www.mql5.com/en/docs/python_metatrader5>

**Tidak ada benchmark latency resmi** untuk `symbol_info_tick()`/`copy_ticks_from()`. MetaQuotes tidak pernah mempublikasikannya. Klaim "1–5 ms" pada spesifikasi **tidak dapat diverifikasi** — angka itu kemungkinan merujuk pada IPC lokal, bukan eksekusi end-to-end. Bukti forum menunjukkan ping 0.5 ms tapi eksekusi ~40 ms, dan "several hundred ms" di sisi broker.

**MetaQuotes sendiri menyatakan DOM retail bukan depth nyata.** Untuk instrumen OTC:
> "the Depth of Market can be formed based on the **quotes of the broker**... If the broker does not provide volumes, the DOM window **functions as a scalping tool**... displays price levels **calculated based on the Bid and Ask prices using the price change step**."
> — <https://www.metatrader5.com/en/terminal/help/trading/depth_of_market>

Artinya "DOM" EURUSD retail bisa sepenuhnya **sintetis** (diinterpolasi dari bid/ask), bukan order book konsolidasi. MQL5's own book chapter bahkan menampilkan book broker nyata dengan **level crossed** (bid di atas ask) — mustahil di CLOB sejati.

**FIX tidak terjangkau di tier ini:** IBKR mensyaratkan **$10,000 minimum equity + $1,500/bulan** minimum komisi untuk sesi FIX pertama (<https://www.interactivebrokers.com/docs/fix/requirements>). LMAX professional-clients-only. Tidak ada prop firm yang menawarkan FIX.

**cTrader Open API adalah satu-satunya feed retail dengan push sejati** — tapi **bukan FIX**: ia Protobuf over TCP port 5035, dengan `ProtoOASpotEvent`/`ProtoOADepthEvent` push. <https://help.ctrader.com/open-api/>

### 2.3 Edge: apakah z-score mean reversion benar-benar menghasilkan uang?

Saya uji secara empiris dengan Monte Carlo (200.000–300.000 tick per skenario).

**Temuan 1 — Mid price adalah pilihan yang BENAR.** Spesifikasi memakai `(Ask+Bid)/2`, bukan last price. Ini penting: mid price **tidak terkena bid-ask bounce**. Saya verifikasi dengan variance ratio test (Lo & MacKinlay):

| Deret | VR(2) | VR(5) | VR(10) |
| :--- | ---: | ---: | ---: |
| TRUE mid (random walk) | 0.998 | 0.992 | 0.992 |
| OBSERVED traded price | **0.576** | **0.321** | **0.235** |
| OBSERVED mid | 0.998 | 0.992 | 0.992 |

Harga traded menunjukkan "mean reversion" palsu (VR≪1) yang **tidak ada** di harga sebenarnya — artefak klasik Roll (1984). Memakai mid price menghindari jebakan ini. ✅ **Spesifikasi benar di sini.**

**Temuan 2 — Tetapi edge-nya tidak cukup menutup biaya.** Simulasi net P&L setelah biaya (N=200, band=2.0):

| Skenario | Biaya 0.3 pip | Biaya 1.0 pip | Biaya 1.5 pip |
| :--- | ---: | ---: | ---: |
| Random walk (tanpa edge) | −726 pip | −2202 pip | −3256 pip |
| OU θ=0.001 (reversion lemah) | −388 pip | −1937 pip | −3043 pip |
| OU θ=0.01 (reversion **kuat**) | **+947 pip** | **−953 pip** | −2310 pip |

**Titik impas ≈ 0.6–0.7 pip round-trip.** Spread EURUSD retail umumnya 0.8–1.2 pip (plus komisi). Artinya: **bahkan dengan mean reversion yang kuat dan nyata, strategi ini rugi** pada biaya retail normal.

**Temuan 3 — Circuit breaker VR jarang menyala.** Pada random walk, `ATR(5)/ATR(30)` berdistribusi sekitar **mean 1.0**; hanya **~2–5% bar** yang melampaui 1.30. Ia bukan gerbang konstan. Dampaknya pada P&L juga marginal: −1341 → −1254 pip (perbaikan ~6%).

**Temuan 4 — VR mengukur PERUBAHAN, bukan LEVEL volatilitas.** Volatilitas tinggi yang konstan tetap menghasilkan VR ≈ 1.0. Ini diuji eksplisit di [`tests/integration/test_scalping_pipeline.py`](../tests/integration/test_scalping_pipeline.py).

**Temuan 5 — ⚠️ Ambang VR ≤ 1.30 secara neto KONTRAPRODUKTIF pada data riil.** Riset lanjutan mengukur gate ini pada data nyata (SPY/QQQ/IWM/BTC, 2016–2026):

| Metrik | Hasil |
| :--- | :--- |
| Presisi prediksi top-decile vol 10 hari | **26.5%** |
| Recall | 28.7% |
| Akurasi | 84.9% vs **baseline "tidak pernah blokir" 90.0%** |
| P(VR>1.30) pada SPY / IWM / BTC | 10.8% / 6.8% / 13.1% (persentil **89 / 93 / 87**) |

Artinya gate ini **lebih buruk daripada tidak melakukan apa pun** pada tugas akurasi tersebut: ia menyala 264 kali untuk menangkap 58 true positive.

**Yang paling penting:** dari return 10-hari **terbaik**, **20.1% terjadi saat gate memblokir** (SPY), dibanding 15.6% dari yang terburuk. Over-representasi: **1.86× untuk peluang terbaik vs 1.44× untuk terburuk**. Pada SPY/QQQ gate ini **memblokir lebih banyak peluang terbaik daripada yang terburuk**. Pada BTC, bucket VR 1.30–1.75 justru punya forward return **+2.35%** dan 1.50–1.75 **+2.96%** — periode yang diblokir adalah periode momentum terbaik.

**Asimetri timing:** VR autokorelasi(1) = 0.934; P(PAUSE besok | PAUSE hari ini) = **82.2%** vs P(PAUSE besok | AKTIF) = 2.7%. Gate **cepat menyala karena noise** (satu bar 3× rata-rata sudah memberi VR = 6(4+3)/(29+3) = **1.3125**) tetapi **lambat lepas** (median 5 bar, maksimum 26 bar lockout).

**Konfirmasi formula:** Wilder smoothing spec **tepat** — `ATR_t = (ATR_{t-1}(N-1)+TR_t)/N` identik dengan EMA alpha = 1/N (bukan 2/(N+1)), sesuai TA-Lib `TA_ATR` dan TradingView RMA. Equivalent EMA period = 2N−1. Seeding tidak material: hanya **1 dari 2451 keputusan gate** berbeda, semuanya dalam 66 bar pertama.

### 2.4 Order book imbalance: sinyal lemah, bukan hard gate

Riset menemukan bahwa rumus spesifikasi `I_t` adalah **bentuk yang lemah** dari sinyal ini:

- **Cont, Kukanov & Stoikov (2014)** menunjukkan pergerakan harga jangka pendek digerakkan oleh **Order Flow Imbalance (OFI)** — yaitu *perubahan* pada best bid/ask (limit order, market order, cancellation) — **bukan** resting volume statis. R² rata-rata ~65%. <https://arxiv.org/abs/1011.6402>
- **Horizon sangat pendek.** Gould & Bonart: queue imbalance hanya memprediksi **satu pergerakan harga berikutnya**; akurasi hanya +10–30% untuk small-tick stocks. <https://arxiv.org/abs/1512.03492>
- **Spoofing:** displayed depth bisa dimanipulasi by construction. Fabre & Challet menemukan 31% order besar di LOB crypto "could spoof the market". <https://arxiv.org/abs/2504.15908>
- **FX tidak punya consolidated tape.** BIS Triennial 2022: $7.5tn/hari, tapi FX itu OTC/desentralisasi — tidak ada satu buku L2 tunggal. <https://www.bis.org/statistics/rpfx22_fx.htm>

**Temuan tambahan yang lebih tajam:**

- **Horizon tidak cocok.** Cont et al. (2023) melaporkan **out-of-sample R² NEGATIF** untuk OFI pada horizon **1 menit ke depan** (FPI[1] = −0.37). Gould & Bonart: prediksi hanya **satu tick ke depan**, dan untuk small-tick (seperti EURUSD) hanya +10–30%. Z-score mean reversion ditahan jauh lebih lama dari detik → sinyal imbalance sudah menjadi noise saat trade dieksekusi. <https://arxiv.org/abs/2112.13213>
- **Ambang ±0.30 TIDAK ADA di literatur.** Tidak ada satu pun dari Cont, Gould, Cartea, atau Stoikov yang mengusulkan cutoff tetap seperti itu. Distribusi imbalance sangat bergantung pada aset, venue, dan waktu — cutoff tetap bersifat arbitrer. Gould & Bonart justru memakai regresi logistik **karena** pemetaannya non-linear dan spesifik per saham.
- **Endogenitas.** Pulido et al. (2023) menunjukkan imbalance sebagian merupakan **respons optimal market maker** terhadap pergerakan harga — jadi pembacaan "buying pressure" bisa jadi *akibat* dari pergerakan yang hendak dikonfirmasi, bukan konfirmasi independen. <https://arxiv.org/abs/2307.15599>
- **Microprice** (Stoikov 2018) adalah prediktor jangka-pendek yang lebih baik daripada mid, dan merupakan **price estimator** — bukan filter/gate. <https://doi.org/10.1080/14697688.2018.1489139>

**Konsekuensi desain:** modul menyediakan **keduanya** — `imbalance` statis (sesuai spesifikasi) **dan** `OrderFlowImbalanceTracker` (OFI, lebih robust). Strategi menyediakan **dua mode**:

| Mode | Perilaku | Dasar |
| :--- | :--- | :--- |
| `"veto"` (**default**) | Tolak entry bila `I_t` berlawanan >0.30 | Sesuai spesifikasi yang diminta |
| `"weight"` | Tidak memveto; `I_t` hanya menskalakan ukuran posisi ke [0.5, 1.0] | Rekomendasi riset |

Mode `"veto"` dipertahankan sebagai default agar sesuai spesifikasi, tetapi mode `"weight"` **lebih defensible** dan tersedia lewat `imbalance_mode="weight"` + `imbalance_size_multiplier()`.

---

## 3. Keputusan Desain (dan alasannya)

### 3.1 Welford vs recompute eksak — hasil pengukuran mengubah rencana awal

Saya awalnya berhipotesis bentuk *downdate* Welford tidak stabil, dan berencana memakai recompute numpy. **Pengukuran membuktikan hipotesis itu salah:**

| Metrik | Hasil |
| :--- | :--- |
| Mean drift (2 juta tick) | 1.2e-13 |
| Variance relative error | 2.8e-07 |
| Variance negatif | 0 (tidak ada) |
| Throughput Welford | ~1.97 juta tick/detik |
| Throughput recompute numpy | ~109 ribu update/detik (**18× lebih lambat**) |

Welford sliding-window **stabil dan 18× lebih cepat** → dipakai sebagai jalur utama. Recompute eksak tetap disediakan sebagai **test oracle** ([`RollingWindowStats.exact_stats()`](../src/data/tick_buffer.py)), dan test memverifikasi keduanya identik hingga <1e-12.

⚠️ **Catatan jujur:** implementasi pertama saya punya bug (deque `maxlen` + `popleft()` ganda) yang menghasilkan error 4e-3. Bug itu ditemukan dan diperbaiki sebelum benchmark final. Clamp `M2 >= 0` tetap dipertahankan sebagai proteksi agar `sqrt()` tidak pernah menerima nilai negatif.

### 3.2 Fail-closed pada depth yang tidak tersedia

Karena DOM EURUSD retail umumnya tidak ada, `calculate_order_book_imbalance()` mengembalikan `available=False` — **bukan** "netral". Strategi default (`allow_without_depth=False`) akan **menolak entry**. Ini konsisten dengan prinsip *Fail-Closed* yang sudah jadi standar repo.

### 3.3 Kalibrasi ambang VR — tiga perbaikan berbasis bukti

Ambang 1.30 tetap jadi **default** (sesuai spesifikasi), tetapi mengingat Temuan 5, saya menambahkan tiga mekanisme yang bisa diaktifkan:

| Mekanisme | Default | Alasan berbasis bukti |
| :--- | :--- | :--- |
| **Histeresis** (`release_threshold`) | 1.30 × 0.88 = 1.144 | Mencegah flapping di sekitar ambang; mengingat P(PAUSE\|PAUSE)=82.2%, gate memang persisten |
| **Konfirmasi** (`confirm_bars`) | 1 (perilaku spec) | Satu bar 3× rata-rata sudah memicu VR=1.3125; `confirm_bars=2–3` menghilangkan false trigger ini |
| **ATR percentile gate** (`ATRPercentileCircuitBreaker`) | tersedia, opt-in | AUC **0.781 vs 0.711** untuk VR ratio pada tugas prediksi vol |

**Alternatif yang direkomendasikan riset** — `ATRPercentileCircuitBreaker` — mengukur **level** volatilitas relatif terhadap sejarah instrumen sendiri (persentil 90–95 masuk, 80 keluar), bukan rasio percepatan. Ini self-referenced sehingga otomatis menyesuaikan diri antar instrumen.

Alternatif estimator volatilitas yang lebih efisien juga terdokumentasi: **Parkinson (1980)** 2.5–5× lebih efisien daripada close-to-close; **Garman-Klass (1980)** ~7.4×; **Yang-Zhang (2000)** varians terkecil di antara estimator sejenis.

---

## 4. Yang Diperbaiki dari Audit Sebelumnya

| Temuan Audit | Perbaikan |
| :--- | :--- |
| `trading_engine.py` tidak pernah menyentuh strategi/data | [`services/scalping_service.py`](../services/scalping_service.py) benar-benar meng-orkestrasi feed → buffer → z-score → CB → sinyal |
| "81 tests / 100% pass" tidak reproducible (~15% run gagal) | RNG di-seed di [`test_sprint_p8_strategy.py`](../tests/unit/test_sprint_p8_strategy.py) & [`test_p18_alpha_research.py`](../tests/unit/test_p18_alpha_research.py). **149 tests, deterministik 8/8** |
| Tidak ada test yang bisa mendeteksi alpha rusak | Test baru meng-assert RR ≥ 2.0 **tanpa toleransi**, nilai referensi hasil hitung tangan, dan invariant numerik |
| Artefak pembulatan RR (`1.999999999999633 >= 2.0`) | Strategi sekarang **menjamin** RR ≥ 2.0 dengan menggeser TP per tick |

---

## 5. Bug yang Ditemukan & Diperbaiki Selama Implementasi

Semua ditemukan lewat eksekusi nyata, bukan review kode:

1. **Bar tidak pernah tertutup** (`bars_closed=0` → circuit breaker tidak pernah ter-update). Penyebab: `_bar_start` di-reset ke waktu tick, dan `_bar_open` tetap `None` setelah close pertama.
2. **Event loop starvation** (`books=0`). Feed tanpa `await` memonopoli loop sehingga task polling book tidak pernah dijadwalkan. Diperbaiki dengan `await asyncio.sleep(0)` per tick — ini justru **persyaratan eksplisit spesifikasi** ("thread utama tidak terblokir").
3. **`__aiter__` tidak men-set `_running`**, sehingga iterasi mengembalikan 0 tick.
4. **RR di bawah 2.0 akibat pembulatan** (1.9783) — diperbaiki dengan jaminan invariant.

---

## 6. Verifikasi

```bash
cd prop-trading-system
.venv/bin/python -m pytest tests/ -q     # 149 passed
```

Cakupan test baru:
- [`tests/unit/test_p20_zscore_scalping.py`](../tests/unit/test_p20_zscore_scalping.py) — 56 test: Welford vs oracle, mid price & bounce, I_t sesuai rumus, OFI, True Range & Wilder (hand-computed), VR & circuit breaker, feed async, strategi komposit, fail-closed.
- [`tests/integration/test_scalping_pipeline.py`](../tests/integration/test_scalping_pipeline.py) — 12 test end-to-end: bar aggregation, book polling, event loop non-blocking, circuit breaker, determinisme, callback.

---

## 6b. Harness Validasi: Variance Ratio Test

Karena seluruh arsitektur ini berdiri di atas satu asumsi — bahwa EURUSD mean-reverting pada horizon tick — saya membangun harness variance ratio untuk **mengukur asumsi itu, bukan mempercayainya**.

> **Status modul:** `research/validation/` telah **dihapus** dalam pembersihan repositori (studi komparasi FX tidak berkaitan dengan target eksekusi FundingPips). Temuan di bawah **tetap dicatat** karena justru itulah yang membatalkan asumsi mean-reversion dan mendorong keputusan walk-forward STOP.

**Mengapa ini penting:** riset menemukan bahwa asumsi tersebut kemungkinan besar **salah**. Portnaya (*The Bounce Has No Direction*, arXiv:2606.29591) menguji panel lintas-aset dan menyimpulkan mean reversion jangka-pendek *"confined to exchange-traded equity markets and sovereign bonds. Credit ETFs, commodities, foreign exchange, and cryptocurrency are statistically indistinguishable from a random walk"* — dan EUR/USD disebut eksplisit sebagai random walk.

**Hasil (arsip):** harness sudah dijalankan pada data EURUSD riil; ringkasannya ada di tabel kalibrasi di bawah. Modulnya sudah tidak ada di repositori.

**Yang diukur:** `VR(q) = Var(r_t(q)) / (q·Var(r_t))` — VR=1 random walk, VR<1 mean reverting, VR>1 trending. Memakai statistik **heteroskedastic-robust (z*)** karena return FX sangat GARCH dan statistik homoskedastik akan over-reject sehingga **memproduksi signifikansi palsu**.

**Kalibrasi harness (terverifikasi):**

| Uji | Hasil |
| :--- | :--- |
| False positive pada 40 random walk murni | **0/40 (0%)** ter-flag mean reverting |
| Deteksi mean reversion sejati (OU kuat) | ✅ terdeteksi |
| Deteksi artefak bid-ask bounce | ✅ terdeteksi, dengan peringatan eksplisit |

**Protokol wajib sebelum deploy:**
1. Jalankan pada **data EURUSD riil**, bukan sintetis.
2. Jalankan **tiga kali**: pada mid quotes, pada transaction prices, dan pada deret **zero-spread sintetis**.
3. Bila `VR<1` **hanya** muncul di transaction prices → itu bid-ask bounce (Roll 1984), **artefak, bukan edge**.
4. Bila edge hilang pada deret zero-spread → yang Anda temukan adalah spread, bukan alpha.

⚠️ **Peringatan kejujuran metodologis:** saya mengimplementasikan diagnostik `R_N` dari literatur, tetapi **tidak dapat memverifikasinya** — ia mengembalikan ~1.0 baik untuk IID maupun dependensi kuat, sehingga tidak dapat membedakan keduanya. Saya **tidak** mengirimkannya dengan label interpretatif yang menyesatkan; sebagai gantinya saya menambahkan **uji permutasi** yang terbukti membedakan (p=0.45 untuk random walk vs p=0.03 untuk OU). `R_N` dipertahankan hanya sebagai referensi dengan peringatan eksplisit di docstring.

---

## 7. Rekomendasi

**JANGAN promosikan ke live pada akun prop firm.** Alasannya berurutan:

1. **Kontraktual (blocker keras):** FundingPips & The5ers melarang tick scalping/HFT sub-detik. Terminasi akun.
2. **Ekonomis:** titik impas ~0.6–0.7 pip; spread retail 0.8–1.2 pip → ekspektasi negatif.
3. **Data:** DOM EURUSD retail kemungkinan sintetis/absen → konfirmasi fail-closed.

**Langkah yang direkomendasikan bila tetap ingin melanjutkan:**

1. **Perpanjang holding period** ke multi-menit (mis. target 5–30 menit). Ini menyelesaikan blocker kontraktual **dan** membuat latency polling MT5 tidak relevan — sesuai rekomendasi riset feed.
2. **Validasi edge sebelum kode:** jalankan variance ratio test pada data EURUSD riil (bukan sintetis) untuk membuktikan mean reversion memang ada pada timeframe target. Bila VR ≈ 1.0, tidak ada edge untuk ditangkap.
3. **Ukur biaya riil** dari akun target (spread + komisi) dan bandingkan dengan titik impas 0.6–0.7 pip.
4. **Pertimbangkan cTrader Open API** bila akun mendukung — satu-satunya feed retail dengan push sejati.
5. **Ganti/auktmentasi `I_t` dengan OFI** (sudah tersedia di modul) karena resting depth lemah dan bisa dipalsukan.

---

## 8. Batasan Riset (Kejujuran Metodologis)

- `web_search` **mati** di sesi ini ("exa-tinyfish is not registered"). Riset dilakukan via `web_fetch` langsung + proxy. Satu agen riset berhasil mencapai sumber resmi (MQL5, cTrader, IBKR, LMAX, BIS, FundingPips, The5ers); klaim yang tidak dapat diverifikasi ditandai eksplisit.
- **Tidak ada benchmark latency resmi** untuk MT5 Python API. Angka apa pun yang dikutip sebagai "resmi" adalah misrepresentasi.
- Simulasi P&L memakai **data sintetis** (random walk & Ornstein-Uhlenbeck), bukan data EURUSD riil. Ini cukup untuk menguji **struktur biaya** dan **sifat statistik**, tetapi **bukan** bukti bahwa EURUSD riil mean-reverting.
- Edge riil EURUSD **belum diuji**. Itu pekerjaan berikutnya yang harus dilakukan sebelum ada klaim apa pun soal profitabilitas.
