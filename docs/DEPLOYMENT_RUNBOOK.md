# Phase 15: Operational Proving & Deployment Runbook

Dokumen ini merupakan panduan operasional standar (*Standard Operating Procedure / SOP*) untuk menjalankan **Phase 15: Controlled Deployment Validation** pada sistem automated prop trading berstandar institusional.

---

## 📋 1. Spesifikasi Infrastruktur Windows VPS

Untuk meminimalkan latency eksekusi ke server broker **Funding Pips** (server time GMT+2 / GMT+3 DST) dan menjamin kestabilan dual-process monolith:

| Komponen | Rekomendasi Minimum | Rekomendasi Produksi |
| :--- | :--- | :--- |
| **Sistem Operasi** | Windows Server 2019 / 2022 (64-bit) | Windows Server 2022 / 2025 Datacenter Edition |
| **Lokasi Datacenter** | London (LD4) atau Amsterdam (AMS) | London / Amsterdam (Latency < 5 ms ke broker server) |
| **CPU** | 2 vCPU (Dedicated) | 4 vCPU (Dedicated Compute) |
| **RAM** | 4 GB | 8 GB DDR4/DDR5 |
| **Storage** | 40 GB NVMe SSD | 80 GB NVMe SSD |
| **Uptime SLA** | 99.9% dengan auto-reboot monitor | 99.99% dengan redundant power & network |

---

## 🔧 2. Setup Lingkungan & MetaTrader 5 VPS

### Langkah 1: Instalasi Python & Dependency
1. Unduh dan pasang Python 64-bit (versi 3.11 atau 3.12 direkomendasikan untuk stabilitas library `MetaTrader5`):
   ```cmd
   curl -o python_installer.exe https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe
   python_installer.exe /quiet InstallAllUsers=1 PrependPath=1
   ```
2. Clone atau transfer direktori proyek ke VPS (misal: `C:\TradingSystems\prop-trading-system`).
3. Buat virtual environment dan pasang dependensi:
   ```cmd
   cd C:\TradingSystems\prop-trading-system
   python -m venv .venv
   .venv\Scripts\activate
   pip install --upgrade pip
   pip install -r requirements.txt
   pip install MetaTrader5
   ```

### Langkah 2: Konfigurasi Terminal MetaTrader 5
1. Unduh MT5 terminal dari client portal Funding Pips atau broker mitra.
2. Login ke akun **Demo Funding Pips** (Simulated Capital $100,000).
3. Buka **Tools $\to$ Options $\to$ Expert Advisors**:
   - Centang **Allow automated trading** (*Allow Algo Trading*).
   - Centang **Allow DLL imports**.
   - Centang **Allow WebRequest for listed URL** (jika diperlukan untuk webhook/telemetri).
4. Pastikan simbol `EURUSD` aktif di **Market Watch** dan tick feed berjalan normal.

---

## 🚦 3. Tahapan Validasi Terkendali (Phase 15 Protocol)

```
                     PHASE 15 VALIDATION PIPELINE
                                  │
    ┌─────────────────────────────┼─────────────────────────────┐
    ▼                             ▼                             ▼
Stage 1: Pre-Flight           Stage 2: Demo Soak            Stage 3: Rule Audit
- MT5 Connectivity Diagnostic - 72-Hour Continuous Run      - 00:00 Rollover DD Check
- Order Lifecycle Flow Test   - 7-Day Market Week Cycle     - News Blackout Compliance
- Storage & Lock Sanity       - 30-Day Stability Proof      - Slippage & Spread Telemetry
```

### 15.1 Pre-Flight Deployment Check
Sebelum menyalakan layanan daemon supervisor, jalankan uji koneksi MT5 dan lifecycle order demo:

```cmd
:: 1. Uji koneksi broker MT5
.venv\Scripts\python scripts\verify_mt5_connection.py

:: 2. Uji lifecycle order lengkap (Submit -> Verify SL/TP -> Close -> Reconcile)
.venv\Scripts\python scripts\test_order_lifecycle.py
```
> [!IMPORTANT]
> Jangan lanjutkan ke tahap daemon jika script di atas mengembalikan error latensi $> 100\text{ ms}$ atau kegagalan pemasangan Stop Loss.

---

### 15.2 Menjalankan Layanan Dual-Process di VPS

Sistem dijalankan dengan arsitektur **Dual-Process Monolith** terpisah antara Main Engine dan Independent Watchdog:

#### Opsi A: Menggunakan PowerShell Supervisor (Development & Monitoring Interaktif)
Buka PowerShell Administrator:
```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\scripts\run_production_services.ps1
```

#### Opsi B: Menggunakan NSSM Windows Background Services (Produksi Otomatis 24/7)
Install kedua service ke Windows Service Control Manager:
```cmd
scripts\nssm_install_services.bat
```
Layanan akan terdaftar sebagai:
- `QPTradingEngine`: Menjalankan `services\trading_engine.py` dengan auto-restart saat crash.
- `QPRiskWatchdog`: Menjalankan `services\risk_watchdog.py` untuk perlindungan akun out-of-band.

Status layanan dapat dimonitor melalui `services.msc` atau command line:
```cmd
sc query QPTradingEngine
sc query QPRiskWatchdog
```

---

## 🛡️ 4. Jadwal Soak Testing & Kriteria Kelulusan (Gate Review)

| Fase Uji | Durasi | Kriteria Kelulusan (Passing Gate) | Tindakan Bila Gagal |
| :--- | :--- | :--- | :--- |
| **72-Hour Soak** | 3 Hari Kalender (72 jam) | - 100% uptime heartbeat tanpa stale $> 10$s.<br>- 0 kegagalan rekonsiliasi state.<br>- 0 transaksi naked tanpa Stop Loss.<br>- Database SQLite WAL checkpoint normal. | Periksa kestabilan koneksi MT5 terminal atau disk I/O VPS. |
| **7-Day Market Week** | 1 Minggu Trading Penuh | - Melewati pergantian sesi London/NY Overlap.<br>- Eksekusi filter berita high-impact (NFP / FOMC / CPI) tanpa slippage berlebih.<br>- Penanganan penutupan pasar akhir pekan (Jumat 21:59 UTC). | Review parameter filter spread dan slippage budget. |
| **30-Day Stability** | 1 Bulan Kalender | - Drawdown harian tidak pernah melanggar batas Funding Pips (5% midnight baseline).<br>- Akumulasi slippage empiris berada dalam toleransi $< 0.5$ pip.<br>- Backup harian `VACUUM INTO` berjalan tanpa kegagalan integritas. | Penyesuaian sizing atau risk cap sebelum mendanai akun evaluasi. |

---

## 🚨 5. Prosedur Tanggap Darurat & Disaster Recovery

### Skenario 1: Terciptanya `halt.lock`
Jika Watchdog mendeteksi engine mati saat ada posisi terbuka, atau terjadi deviasi saldo di luar batas aman:
1. File `halt.lock` otomatis dibuat.
2. Seluruh order baru ditolak seketika (`FAIL-CLOSED`).
3. Seluruh posisi aktif di MT5 dilikuidasi ke pasar secara instan.
4. **Langkah Operator**:
   - Periksa log error di `logs/` dan tabel audit SQLite.
   - Pastikan akun MT5 bersih dari floating risk.
   - Hapus kunci darurat setelah penyebab diselesaikan:
     ```cmd
     del halt.lock
     ```
   - Restart service via PowerShell atau `net start QPTradingEngine`.

### Skenario 2: Restore Database SQLite dari Backup
Backup online non-blocking otomatis dibuat setiap hari di `backups/` menggunakan `scripts/backup_db.py`:
```cmd
:: Verifikasi integritas file backup
sqlite3 backups\trading_backup_YYYYMMDD_HHMMSS.db "PRAGMA integrity_check;"

:: Pulihkan database jika file utama storage\trading.db rusak
copy /Y backups\trading_backup_YYYYMMDD_HHMMSS.db storage\trading.db
```

---

## 📈 6. Parameter Target Evaluasi Funding Pips 2-Step

| Aturan Prop Firm | Batas Aturan | Implementasi Proteksi Kode |
| :--- | :--- | :--- |
| **Profit Target Fase 1** | $8.0\%$ ($+\$8,000$) | Strategi mematikan trading otomatis saat target tercapai. |
| **Profit Target Fase 2** | $5.0\%$ ($+\$5,000$) | Target lebih konservatif untuk mempercepat kelulusan. |
| **Maksimum Daily Loss** | $5.0\%$ ($-\$5,000$) | `DrawdownMonitor` mereset baseline setiap 00:00 server time. Hard kill switch di $4.0\%$. |
| **Maksimum Total Loss** | $10.0\%$ ($-\$10,000$) | Batas absolut dari saldo awal $100,000 (Floor: $\$90,000$). |
| **Minimum Trading Days** | 3 Hari Kalender | `PropFirmSimulator` memperhitungkan kalender aktif. |
| **Overnight & Weekend** | Diizinkan di Standard | Posisi diawasi oleh Watchdog dengan Stop Loss wajib. |
