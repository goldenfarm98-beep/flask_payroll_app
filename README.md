# Flask Payroll

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
