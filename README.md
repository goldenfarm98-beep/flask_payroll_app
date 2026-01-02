# Flask Payroll

## Ikhtisar
Aplikasi payroll berbasis Flask dengan workflow persetujuan, import karyawan, ekspor payroll/bank, dan halaman status server.

## Persiapan
1) Buat virtual environment dan install dependensi:
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2) Jalankan PostgreSQL lewat Docker:
```bash
docker-compose up -d db
```

3) Siapkan `.env` (contoh minimal):
```
DATABASE_URL=postgresql+psycopg2://payroll:payroll@localhost:5432/payroll
POSTGRES_USER=payroll
POSTGRES_PASSWORD=payroll
POSTGRES_DB=payroll
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
```

4) Jalankan migrasi:
```bash
set -a
source .env
set +a
flask db upgrade
```

5) Jalankan aplikasi:
```bash
flask run --host=0.0.0.0 --port=5000
```

## Reset Database + Jalankan Test
```bash
./scripts/reset_db.sh

source .venv/bin/activate
export DATABASE_URL=postgresql+psycopg2://payroll:payroll@localhost:5432/payroll
pytest -q
```

Catatan:
- Pastikan Docker berjalan.
- Script reset juga menghapus arsip backup lama di folder `backups/`.
- Jika `DATABASE_URL` berbeda, sesuaikan nilainya sebelum menjalankan test.

## Perubahan & Fitur Baru

### 1) Halaman Status Server
- URL: `/admin/server_status`
- Menampilkan nama database, ukuran, lokasi, status koneksi, backup terbaru, metrik operasional, dan info runtime.
- Untuk Postgres di Docker, disk database menampilkan pesan khusus karena disk usage volume tidak bisa dibaca langsung.
- Backup settings otomatis dibuat saat halaman dibuka.

### 2) Backup
- Pengaturan backup ada di `/admin/backup/settings`.
- Tombol backup/pengaturan di dashboard sudah dihapus (masih bisa diakses via menu).
- Reset DB via script menghapus arsip `backup_*.json`.

### 3) Karyawan
- Tambah kolom `Nama Bank` pada data karyawan.
- Import massal karyawan dari Excel/CSV.
  - Form tersedia di halaman `/employees`.
  - Template contoh: `static/templates/employee_import_template.xlsx`.
  - Kolom yang didukung: NIK, Nama, Jabatan/Posisi, Alamat, Telepon/No. HP,
    No. Rekening, Nama Bank, Tanggal Masuk (format tanggal bebas, direkomendasikan `YYYY-MM-DD`).
- Export karyawan (Excel/PDF) sudah menyertakan “Nama Bank”.

### 4) Payroll: PPH21 + BPJS Otomatis
- Field baru: `bpjs_kesehatan` dan `pph21`.
- Checkbox “Hitung otomatis PPH21 & BPJS” (default on) di tambah/edit payroll.
- Perhitungan otomatis memakai konfigurasi env:
  - `BPJS_KETENAGAKERJAAN_RATE` default `0.02`
  - `BPJS_KESEHATAN_RATE` default `0.01`
  - `BPJS_KESEHATAN_CAP` default `12000000`
  - `PPH21_RATE` default `0.05`
  - `PPH21_PTKP_MONTHLY` default `4500000`

### 5) Workflow Persetujuan Payroll
Status: `draft → submitted → approved`, plus `rejected`.
- Draft: bisa edit/hapus, bisa diajukan.
- Submitted: bisa disetujui, ditolak, atau dikembalikan ke draft.
- Rejected: tampilkan alasan penolakan, bisa kembali ke draft.
- Approved: terkunci.
- Bulk approve hanya memproses status `submitted`.

### 6) Alasan Penolakan
- Saat menolak payroll, wajib isi alasan lewat modal textarea.
- Alasan ditampilkan di daftar payroll dan slip gaji.
- Alasan dihapus saat payroll dikembalikan ke draft atau diajukan ulang.

### 7) Ekspor Bank
- `/reports/bank_export?pay_period=YYYY-MM&file_format=csv|excel`
- Sekarang menyertakan kolom `Nama Bank`.

## Catatan Teknis
- Database utama menggunakan PostgreSQL.
- Migrasi terbaru ada di folder `migrations/versions/`.
