from flask import Flask, render_template, request, redirect, url_for, session, flash, abort, send_file
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, date, timezone, timedelta
from sqlalchemy import func
from sqlalchemy.ext.hybrid import hybrid_property
from flask import make_response
from werkzeug.utils import secure_filename
import csv
import os
import io
import secrets
import json
import threading
import time
from collections import defaultdict
import pandas as pd
import pdfkit  # pastikan sudah install pdfkit dan wkhtmltopdf
from flask_migrate import Migrate
from flask import request
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from decimal import Decimal


app = Flask(__name__)

# SECRET_KEY untuk session
app.secret_key = "secretkey123"  # Ganti dengan yang lebih aman

# Konfigurasi Database
basedir = os.path.abspath(os.path.dirname(__file__))


def get_database_uri():
    url = os.getenv("DATABASE_URL") or os.getenv("SQLALCHEMY_DATABASE_URI")
    if url:
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]
        return url
    return "sqlite:///" + os.path.join(basedir, "payroll.db")


db_url = get_database_uri()
app.config['SQLALCHEMY_DATABASE_URI'] = db_url
search_path = os.getenv("DB_SEARCH_PATH")
if search_path and db_url.startswith("postgres"):
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        "connect_args": {"options": f"-csearch_path={search_path}"}
    }
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# Inisialisasi Flask-Migrate
migrate = Migrate(app, db)

# --- Backup helpers ---
def ensure_backup_dir():
    backup_dir = os.path.join(basedir, "backups")
    os.makedirs(backup_dir, exist_ok=True)
    return backup_dir


def serialize_value(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return value.hex()
    return value


def export_database_json():
    backup_dir = ensure_backup_dir()
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    backend = db.engine.url.get_backend_name()
    filename = f"backup_{backend}_{timestamp}.json"
    path = os.path.join(backup_dir, filename)

    metadata = sa.MetaData()
    metadata.reflect(bind=db.engine)

    data = {
        "meta": {
            "exported_at": datetime.utcnow().isoformat() + "Z",
            "backend": backend,
            "database": db.engine.url.render_as_string(hide_password=True),
        },
        "tables": {},
    }

    for table in metadata.sorted_tables:
        rows = db.session.execute(sa.select(table)).mappings().all()
        data["tables"][table.name] = [
            {k: serialize_value(v) for k, v in row.items()} for row in rows
        ]

    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=True, indent=2)

    return path


# --- Model Database ---
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    fullname = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)
    role = db.Column(db.String(50), default='user')  # misal 'admin' atau 'user'


class BackupSettings(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    enabled = db.Column(db.Boolean, nullable=False, default=False)
    interval_hours = db.Column(db.Integer, nullable=False, default=24)
    retention_count = db.Column(db.Integer, nullable=False, default=7)
    next_run_at = db.Column(db.DateTime, nullable=True)
    last_run_at = db.Column(db.DateTime, nullable=True)
    last_status = db.Column(db.String(20), nullable=True)
    last_error = db.Column(db.Text, nullable=True)
    last_backup_file = db.Column(db.String(255), nullable=True)


AUTO_BACKUP_POLL_SECONDS = max(10, int(os.getenv("AUTO_BACKUP_POLL_SECONDS", "60")))
backup_worker_thread = None
backup_worker_lock = threading.Lock()
backup_run_lock = threading.Lock()


def get_backup_settings(create_if_missing=True):
    try:
        settings = BackupSettings.query.first()
    except Exception:
        db.session.rollback()
        return None

    if not settings and create_if_missing:
        settings = BackupSettings(
            enabled=False,
            interval_hours=24,
            retention_count=7,
        )
        db.session.add(settings)
        db.session.commit()

    return settings


def compute_next_run(now, interval_hours):
    hours = max(1, int(interval_hours or 24))
    return now + timedelta(hours=hours)


def list_backup_files(limit=10):
    backup_dir = ensure_backup_dir()
    files = []

    try:
        for entry in os.scandir(backup_dir):
            if not entry.is_file():
                continue
            if not entry.name.startswith("backup_") or not entry.name.endswith(".json"):
                continue
            stat = entry.stat()
            files.append({
                "name": entry.name,
                "mtime": datetime.utcfromtimestamp(stat.st_mtime),
                "size": stat.st_size,
            })
    except FileNotFoundError:
        return []

    files.sort(key=lambda item: item["mtime"], reverse=True)
    if limit:
        return files[:limit]
    return files


def prune_old_backups(retention_count):
    retention = max(1, int(retention_count or 7))
    backup_dir = ensure_backup_dir()
    files = list_backup_files(limit=None)

    for item in files[retention:]:
        try:
            os.remove(os.path.join(backup_dir, item["name"]))
        except OSError:
            continue


def run_scheduled_backup():
    if not backup_run_lock.acquire(blocking=False):
        return

    try:
        with app.app_context():
            settings = get_backup_settings(create_if_missing=False)
            if not settings or not settings.enabled:
                return

            now = datetime.utcnow()
            if settings.next_run_at and now < settings.next_run_at:
                return

            backup_path = export_database_json()
            settings.last_run_at = now
            settings.last_status = "success"
            settings.last_error = None
            settings.last_backup_file = os.path.basename(backup_path)
            settings.next_run_at = compute_next_run(now, settings.interval_hours)
            db.session.commit()
            prune_old_backups(settings.retention_count)
    except Exception as exc:
        with app.app_context():
            db.session.rollback()
            settings = get_backup_settings(create_if_missing=False)
            if settings:
                settings.last_run_at = datetime.utcnow()
                settings.last_status = "failed"
                settings.last_error = str(exc)
                settings.next_run_at = compute_next_run(settings.last_run_at, settings.interval_hours)
                db.session.commit()
    finally:
        backup_run_lock.release()


def auto_backup_loop():
    while True:
        try:
            run_scheduled_backup()
        except Exception:
            app.logger.exception("Auto backup gagal.")
        time.sleep(AUTO_BACKUP_POLL_SECONDS)


def should_start_backup_worker():
    if os.getenv("AUTO_BACKUP_DISABLED") == "1":
        return False
    if os.getenv("PYTEST_CURRENT_TEST"):
        return False
    if os.getenv("FLASK_RUN_FROM_CLI") == "true":
        return os.getenv("WERKZEUG_RUN_MAIN") == "true"
    return True


def start_backup_worker():
    global backup_worker_thread
    if not should_start_backup_worker():
        return
    with backup_worker_lock:
        if backup_worker_thread and backup_worker_thread.is_alive():
            return
        backup_worker_thread = threading.Thread(
            target=auto_backup_loop,
            name="auto-backup",
            daemon=True,
        )
        backup_worker_thread.start()


start_backup_worker()

class Employee(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, nullable=True)
    nik = db.Column(db.String(20), unique=True, nullable=False)
    name = db.Column(db.String(100), nullable=False)
    position = db.Column(db.String(100), nullable=True)

    # Informasi tambahan
    address = db.Column(db.String(255), nullable=True)
    phone = db.Column(db.String(50), nullable=True)

    # ⬇⬇ Tambahkan baris ini ⬇⬇
    no_rek = db.Column(db.String(50), nullable=True)  # Nomor rekening karyawan

    hire_date = db.Column(db.Date, nullable=True)
    photo = db.Column(db.String(255), nullable=True)
    status = db.Column(db.String(20), default='active')  # active/inactive


class Payroll(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey('employee.id'), nullable=False)
    pay_period = db.Column(db.String(7), nullable=True)
    gaji_pokok = db.Column(db.Float, default=0)
    bpjs_ketenagakerjaan = db.Column(db.Float, default=0)
    tunjangan_makan = db.Column(db.Float, default=0)
    tunjangan_transport = db.Column(db.Float, default=0)
    tunjangan_lainnya = db.Column(db.Float, default=0)
    potongan_gaji = db.Column(db.Float, default=0)
    alpha = db.Column(db.Integer, default=0)
    hutang = db.Column(db.Float, default=0)
    upah_lembur = db.Column(db.Float, default=0)
    thr = db.Column(db.Float, default=0)
    loan_deduction = db.Column(db.Float, default=0)  # Kolom baru untuk potongan pinjaman
    installments = db.relationship('PayrollLoan', backref='payroll', cascade='all, delete-orphan')
    employee = db.relationship('Employee', backref=db.backref('payrolls', lazy=True))
    status = db.Column(db.String(20), default='draft')  # draft/approved
    approved_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    approved_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @hybrid_property
    def take_home_pay(self):
        pendapatan = (
            self.gaji_pokok
            + self.bpjs_ketenagakerjaan
            + self.tunjangan_makan
            + self.tunjangan_transport
            + self.tunjangan_lainnya
            + self.upah_lembur
            + self.thr
        )
        potongan_alpha = self.alpha * (self.gaji_pokok / 30.0)
        total_potongan = self.potongan_gaji + self.hutang + potongan_alpha + self.loan_deduction
        return pendapatan - total_potongan

    @take_home_pay.expression
    def take_home_pay(cls):
        pendapatan = (
            cls.gaji_pokok
            + cls.bpjs_ketenagakerjaan
            + cls.tunjangan_makan
            + cls.tunjangan_transport
            + cls.tunjangan_lainnya
            + cls.upah_lembur
            + cls.thr
        )
        potongan_alpha = cls.alpha * (cls.gaji_pokok / 30.0)
        total_potongan = cls.potongan_gaji + cls.hutang + potongan_alpha + cls.loan_deduction
        return pendapatan - total_potongan
    # ----- TOTAL POTONGAN UNTUK TABEL PAYROLL ----- 
    
    @hybrid_property
    def total_deductions(self):
            pot_alpha = self.alpha * (self.gaji_pokok / 30.0)
            return self.potongan_gaji + self.hutang + pot_alpha + self.loan_deduction

    @total_deductions.expression
    def total_deductions(cls):
            pot_alpha = cls.alpha * (cls.gaji_pokok / 30.0)
            return cls.potongan_gaji + cls.hutang + pot_alpha + cls.loan_deduction

    @hybrid_property
    def pay_period_date(self):
        return parse_period_to_date(self.pay_period)

    @pay_period_date.expression
    def pay_period_date(cls):
        # Cross-dialect conversion "YYYY-MM" -> date (cast string).
        return sa.cast(cls.pay_period + '-01', sa.Date)

# === MODEL PINJAMAN ===
class Loan(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey('employee.id'), nullable=False)
    amount = db.Column(db.Float, nullable=False)  # Pokok pinjaman
    tenor = db.Column(db.Integer, nullable=False)  # dalam bulan
    interest_rate = db.Column(db.Float, default=0.0)  # dalam persentase
    # Perhitungan cicilan: (amount + (amount * interest_rate/100)) / tenor
    installment = db.Column(db.Float, nullable=False)  
    status = db.Column(db.String(20), default="pending")  # pending, approved, rejected, completed
    application_date = db.Column(db.DateTime, default=datetime.now(timezone.utc))
    approval_date = db.Column(db.DateTime, nullable=True)
    reason = db.Column(db.Text, nullable=True)
    
    # Field baru: jumlah angsuran yang telah dibayar
    installments_paid = db.Column(db.Integer, default=0)
    @hybrid_property
    def remaining(self):
        total_loan = self.amount + (self.amount * self.interest_rate / 100)
        total_paid = sum(p.payment_amount
                         for p in self.payments
                         if p.status in ('approved', 'posted'))
        return max(total_loan - total_paid, 0)
    
    @remaining.expression
    def remaining(cls):
        total_loan = cls.amount + (cls.amount * cls.interest_rate / 100)
        subq = sa.select(sa.func.coalesce(sa.func.sum(Payment.payment_amount), 0)) \
                 .where(Payment.loan_id == cls.id,
                        Payment.status.in_(('approved', 'posted'))) \
                 .scalar_subquery()
        return total_loan - subq
    
    # ----- TOTAL CICILAN YANG SUDAH DIBAYAR -----
    @hybrid_property
    def paid_installment(self):
        """
        Jumlah kumulatif pembayaran (approved + posted) yang sudah diterima.
        Jika belum pernah bayar, kembalikan 0.
        """
        return sum(p.payment_amount for p in self.payments
                if p.status in ('approved', 'posted'))

    @paid_installment.expression
    def paid_installment(cls):
        subq = sa.select(sa.func.coalesce(sa.func.sum(Payment.payment_amount), 0)) \
                .where(Payment.loan_id == cls.id,
                        Payment.status.in_(('approved', 'posted'))) \
                .scalar_subquery()
        return subq


    
    employee = db.relationship('Employee', backref=db.backref('loans', lazy=True))

class Payment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    loan_id = db.Column(db.Integer, db.ForeignKey('loan.id'), nullable=False)
    payment_date = db.Column(db.DateTime, default=datetime.now(timezone.utc))
    payment_amount = db.Column(db.Float, nullable=False)
    status = db.Column(db.String(20), default='pending')  # status: pending, approved

    loan = db.relationship('Loan', backref=db.backref('payments', lazy=True, cascade="all, delete-orphan"))

class PayrollLoan(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    payroll_id = db.Column(db.Integer, db.ForeignKey('payroll.id'), nullable=False)
    loan_id    = db.Column(db.Integer, db.ForeignKey('loan.id'),  nullable=False)
    payment_id = db.Column(db.Integer, db.ForeignKey('payment.id'), nullable=False)  # NEW
    installment_number = db.Column(db.Integer, nullable=False)
    amount = db.Column(db.Float, nullable=False)

    loan    = db.relationship('Loan')
    payment = db.relationship('Payment')


class CompensationComponent(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(50), unique=True, nullable=False)
    name = db.Column(db.String(120), nullable=False)
    comp_type = db.Column(db.String(20), nullable=False)  # gaji_pokok / tunjangan / potongan
    calc_type = db.Column(db.String(20), default='fixed')  # fixed / percentage
    default_value = db.Column(db.Float, default=0)
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class EmployeeCompensation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey('employee.id'), nullable=False)
    component_id = db.Column(db.Integer, db.ForeignKey('compensation_component.id'), nullable=False)
    value = db.Column(db.Float, nullable=True)  # jika None gunakan default component
    start_period = db.Column(db.String(7), nullable=True)  # "YYYY-MM"
    active = db.Column(db.Boolean, default=True)

    component = db.relationship('CompensationComponent')
    employee = db.relationship('Employee')


class AuditLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    action = db.Column(db.String(100), nullable=False)
    entity_type = db.Column(db.String(100), nullable=False)
    entity_id = db.Column(db.Integer, nullable=False)
    details = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


# --- Helper audit ---
def log_action(action, entity_type, entity_id, details=None):
    user_id = session.get('user_id')
    entry = AuditLog(
        user_id=user_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        details=details
    )
    db.session.add(entry)
    db.session.commit()



# --- ROUTES ---

# Tambahkan baris berikut:
app.config['UPLOAD_FOLDER'] = os.path.join(basedir, 'static/uploads')


def get_pdfkit_config():
    """
    Kembalikan konfigurasi pdfkit dengan wkhtmltopdf.
    - Gunakan env WKHTMLTOPDF_PATH jika diset.
    - Jika wkhtmltopdf tidak ditemukan, kembalikan None agar pemanggil bisa memberi pesan error.
    """
    wkhtmltopdf_path = os.getenv("WKHTMLTOPDF_PATH")
    try:
        if wkhtmltopdf_path:
            if os.path.isfile(wkhtmltopdf_path):
                return pdfkit.configuration(wkhtmltopdf=wkhtmltopdf_path)
            return None
        # Autodetect di PATH
        return pdfkit.configuration()
    except Exception:
        return None


@app.route('/')
def index():
    total_employees = Employee.query.count()
    active_employees = Employee.query.filter_by(status='active').count()
    inactive_employees = Employee.query.filter_by(status='inactive').count()

    now = datetime.now()
    month_start = date(now.year, now.month, 1)
    hires_this_month = Employee.query.filter(Employee.hire_date >= month_start).count()

    latest_period = db.session.query(func.max(Payroll.pay_period)).scalar()
    payroll_total = 0
    payroll_approved = 0
    payroll_draft = 0
    payroll_take_home = 0
    payroll_deductions = 0
    if latest_period:
        payroll_total = Payroll.query.filter(Payroll.pay_period == latest_period).count()
        payroll_approved = Payroll.query.filter(
            Payroll.pay_period == latest_period,
            Payroll.status == 'approved'
        ).count()
        payroll_draft = Payroll.query.filter(
            Payroll.pay_period == latest_period,
            Payroll.status != 'approved'
        ).count()
        payroll_take_home = db.session.query(
            func.sum(Payroll.take_home_pay)
        ).filter(Payroll.pay_period == latest_period).scalar() or 0
        payroll_deductions = db.session.query(
            func.sum(Payroll.total_deductions)
        ).filter(Payroll.pay_period == latest_period).scalar() or 0

    payroll_approved_pct = int(round((payroll_approved / payroll_total) * 100)) if payroll_total else 0

    loans_active = Loan.query.filter(Loan.status == 'approved').count()
    loans_pending = Loan.query.filter(Loan.status == 'pending').count()
    loans_completed = Loan.query.filter(Loan.status == 'completed').count()
    loan_total = db.session.query(
        func.sum(Loan.amount + (Loan.amount * Loan.interest_rate / 100))
    ).filter(Loan.status == 'approved').scalar() or 0
    loan_outstanding = db.session.query(
        func.sum(Loan.remaining)
    ).filter(Loan.status == 'approved').scalar() or 0
    loan_paid = max(loan_total - loan_outstanding, 0)
    loan_paid_pct = int(round((loan_paid / loan_total) * 100)) if loan_total else 0

    payments_pending = Payment.query.filter(Payment.status == 'pending').count()

    latest_period_label = "Belum ada payroll"
    if latest_period:
        try:
            year, month = latest_period.split('-')
            month_names = [
                "Januari", "Februari", "Maret", "April", "Mei", "Juni",
                "Juli", "Agustus", "September", "Oktober", "November", "Desember"
            ]
            month_index = int(month) - 1
            if 0 <= month_index < len(month_names):
                latest_period_label = f"{month_names[month_index]} {year}"
            else:
                latest_period_label = latest_period
        except ValueError:
            latest_period_label = latest_period

    active_pct = int(round((active_employees / total_employees) * 100)) if total_employees else 0

    return render_template(
        'index.html',
        total_employees=total_employees,
        active_employees=active_employees,
        inactive_employees=inactive_employees,
        active_pct=active_pct,
        hires_this_month=hires_this_month,
        latest_period=latest_period,
        latest_period_label=latest_period_label,
        payroll_total=payroll_total,
        payroll_approved=payroll_approved,
        payroll_draft=payroll_draft,
        payroll_take_home=payroll_take_home,
        payroll_deductions=payroll_deductions,
        payroll_approved_pct=payroll_approved_pct,
        loans_active=loans_active,
        loans_pending=loans_pending,
        loans_completed=loans_completed,
        loan_total=loan_total,
        loan_outstanding=loan_outstanding,
        loan_paid_pct=loan_paid_pct,
        payments_pending=payments_pending
    )

def calculate_loan_deduction(employee_id):
    """
    Menghitung total potongan cicilan dari pinjaman yang disetujui dan aktif untuk karyawan tertentu.
    Fungsi ini juga akan menambahkan 1 pada installments_paid untuk tiap pinjaman yang aktif.
    """
    loans = Loan.query.filter_by(employee_id=employee_id, status='approved').all()
    total_deduction = 0
    for loan in loans:
        if loan.installments_paid < loan.tenor:
            total_deduction += loan.installment
            loan.installments_paid += 1
            # Jika pinjaman sudah lunas, ubah status menjadi 'completed'
            if loan.installments_paid >= loan.tenor:
                loan.status = 'completed'
    db.session.commit()
    return total_deduction

def approved_payments(employee_id):
    """
    Mengembalikan list dict:
      { "payment": <Payment>, "loan": <Loan>, "number": <int> }
     hanya Payment ber-status 'approved'
     belum pernah dimasukkan ke payroll (Payment.status != 'posted')
    """
    items = []
    # join Loan agar bisa filter employee_id
    payments = (Payment.query
            .join(Loan)
            .filter(Loan.employee_id == employee_id,
                    Payment.status == 'approved',
                    ~Payment.id.in_(db.session.query(PayrollLoan.payment_id)))
            .order_by(Payment.payment_date)
            .all())

    # hitung urutan “cicilan ke-berapa” per loan
    per_loan_counter = {}
    for p in payments:
        per_loan_counter.setdefault(p.loan_id, 0)
        per_loan_counter[p.loan_id] += 1          # 1, 2, 3, …
        items.append({
            "payment": p,
            "loan": p.loan,
            "number": p.loan.installments_paid + per_loan_counter[p.loan_id]
        })
    return items


def remaining_installments(employee_id):
    """
    Kembalikan list dict:
    [
      {"loan": <Loan>, "number": 3, "amount": 250000},
      {"loan": <Loan>, "number": 4, "amount": 250000},
      ...
    ]
    Hanya pinjaman 'approved' yang belum lunas.
    """
    items = []
    loans = Loan.query.filter_by(employee_id=employee_id, status='approved').all()
    for loan in loans:
        for n in range(loan.installments_paid + 1, loan.tenor + 1):
            items.append({"loan": loan, "number": n, "amount": loan.installment})
    return items


def get_component_totals(employee_id, pay_period):
    """
    Hitung nilai default gaji/tunjangan/potongan berbasis master komponen
    yang aktif untuk karyawan pada periode tertentu (YYYY-MM).
    - comp_type: gaji_pokok, tunjangan, potongan
    - calc_type: fixed (nilai apa adanya), percentage (persen dari total gaji pokok)
    """
    if not pay_period:
        return {"gaji_pokok": 0, "tunjangan": 0, "potongan": 0, "items": []}

    comps = (EmployeeCompensation.query
             .join(CompensationComponent)
             .filter(EmployeeCompensation.employee_id == employee_id,
                     EmployeeCompensation.active == True,
                     CompensationComponent.active == True)
             .all())

    def period_ok(start):
        if not start:
            return True
        try:
            return start <= pay_period
        except Exception:
            return True

    filtered = [c for c in comps if period_ok(c.start_period)]
    base = 0.0
    items = []
    for c in filtered:
        comp = c.component
        val = c.value if c.value is not None else comp.default_value or 0
        if comp.comp_type == 'gaji_pokok':
            base += val

    tunjangan = 0.0
    potongan = 0.0
    for c in filtered:
        comp = c.component
        val = c.value if c.value is not None else comp.default_value or 0
        if comp.comp_type == 'gaji_pokok':
            continue
        if comp.calc_type == 'percentage':
            val = (base * val) / 100.0
        if comp.comp_type == 'tunjangan':
            tunjangan += val
        elif comp.comp_type == 'potongan':
            potongan += val
        items.append({
            "name": comp.name,
            "code": comp.code,
            "type": comp.comp_type,
            "calc": comp.calc_type,
            "value": val
        })

    return {"gaji_pokok": base, "tunjangan": tunjangan, "potongan": potongan, "items": items}


def parse_period_to_date(period_str):
    """
    Mengubah pay_period (YYYY-MM) menjadi datetime.date, 
    mis. "2025-03" -> date(2025, 3, 1).
    """
    if not period_str:
        return None
    try:
        year, month = period_str.split('-')
        return date(int(year), int(month), 1)
    except:
        return None

def months_of_service(hire_date, end_date):
    """
    Menghitung selisih bulan antara hire_date dan end_date.
    Contoh:
      hire_date=2025-01-15, end_date=2025-03-01  => 1 atau 2 bulan, 
      tergantung logika rounding. 
    Di sini kita buat simple: hitung selisih year * 12 + selisih month.
    """
    if not hire_date or not end_date:
        return 0
    diff_years = end_date.year - hire_date.year
    diff_months = end_date.month - hire_date.month
    total_months = diff_years * 12 + diff_months
    
    # Jika end_date harinya kurang dari hire_date, turunkan 1 bulan
    if end_date.day < hire_date.day:
        total_months -= 1
    
    return total_months if total_months > 0 else 0

@app.template_filter('strftime')
def strftime_filter(value, format_str="%d/%m/%Y"):
    """
    Memformat datetime/date menjadi string sesuai format_str.
    Default: dd/mm/YYYY (contoh: 10/03/2025).
    """
    if not value:
        return ""
    return value.strftime(format_str)


@app.template_filter('rupiah')
def rupiah_format(value):
    """
    Mengubah angka menjadi format rupiah Indonesia,
    misalnya 5000000 -> '5.000.000'
    Hanya menampilkan angka bulat (tanpa desimal).
    """
    if not value:
        value = 0
    try:
        # Pastikan ke float, lalu ke int agar menghilangkan desimal
        val = int(float(value))
    except:
        val = 0
    
    # Format dengan koma, lalu ganti koma jadi titik
    formatted = f"{val:,}"
    return formatted.replace(",", ".")

# ---------- CSRF sederhana ----------
def generate_csrf_token():
    token = session.get('csrf_token')
    if not token:
        token = secrets.token_hex(16)
        session['csrf_token'] = token
    return token

@app.context_processor
def inject_csrf():
    return {'csrf_token': generate_csrf_token()}

@app.before_request
def csrf_protect():
    if request.method == "POST":
        # Form bisa ditambah ke whitelist jika perlu
        token = session.get('csrf_token')
        form_token = request.form.get('csrf_token')
        if not token or not form_token or token != form_token:
            abort(400, description="CSRF token invalid")

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        fullname = request.form.get('fullname')
        nik = request.form.get('nik').strip()
        email = request.form.get('email')
        password = request.form.get('password')

        # Pastikan email belum terdaftar di tabel User
        existing_user = User.query.filter_by(email=email).first()
        if existing_user:
            flash('Email sudah terdaftar, silakan gunakan email lain.', 'danger')
            return redirect(url_for('register'))

        # Validasi domain email: hanya email dengan domain "goldenfarm99.com" yang diizinkan
        allowed_domain = "goldenfarm99.com"
        if not email.lower().endswith("@" + allowed_domain):
            flash("Registrasi hanya diperbolehkan dengan email perusahaan (@goldenfarm99.com)", "danger")
            return redirect(url_for('register'))

        # Cek apakah sudah ada record Employee dengan NIK tersebut
        existing_employee = Employee.query.filter_by(nik=nik).first()

        # Buat user baru terlebih dahulu (role 'user' untuk karyawan)
        hashed_password = generate_password_hash(password, method='pbkdf2:sha256')
        new_user = User(fullname=fullname, email=email, password=hashed_password, role='user')
        db.session.add(new_user)
        db.session.commit()

        if existing_employee:
            # Jika record Employee sudah ada
            if existing_employee.user_id:
                # Jika sudah di-link ke akun user lain, tolak registrasi
                flash('NIK sudah terdaftar, silakan gunakan NIK lain.', 'danger')
                return redirect(url_for('register'))
            else:
                # Jika belum di-link, update record Employee untuk mengaitkan dengan akun baru
                existing_employee.user_id = new_user.id
                # Opsional: update nama sesuai input registrasi
                existing_employee.name = fullname
                db.session.commit()
        else:
            # Jika record Employee tidak ada, buat record baru
            new_emp = Employee(
                user_id=new_user.id,
                nik=nik,
                name=fullname,
                position="",
                address="",
                phone="",
                hire_date=None
            )
            db.session.add(new_emp)
            db.session.commit()

        flash('Registrasi berhasil! Silakan login.', 'success')
        return redirect(url_for('login'))

    return render_template('register.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')

        user = User.query.filter_by(email=email).first()
        if user and check_password_hash(user.password, password):
            session['user_id'] = user.id
            session['user_name'] = user.fullname
            session['role'] = user.role
            flash('Login berhasil.', 'success')
            # Redirect berdasarkan role
            if user.role == 'admin':
                return redirect(url_for('dashboard'))
            else:
                return redirect(url_for('employee_dashboard'))
        else:
            flash('Email atau password salah.', 'danger')
            return redirect(url_for('login'))

    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    flash('Anda telah keluar.', 'info')
    return redirect(url_for('index'))


@app.route('/change_password', methods=['GET', 'POST'])
def change_password():
    if 'user_id' not in session:
        flash('Harap login terlebih dahulu.', 'warning')
        return redirect(url_for('login'))

    user = User.query.get(session.get('user_id'))
    if not user:
        session.clear()
        flash('Akun tidak ditemukan. Silakan login kembali.', 'danger')
        return redirect(url_for('login'))

    if request.method == 'POST':
        current_password = request.form.get('current_password') or ''
        new_password = request.form.get('new_password') or ''
        confirm_password = request.form.get('confirm_password') or ''

        if not check_password_hash(user.password, current_password):
            flash('Password saat ini salah.', 'danger')
            return redirect(url_for('change_password'))
        if not new_password:
            flash('Password baru wajib diisi.', 'danger')
            return redirect(url_for('change_password'))
        if new_password != confirm_password:
            flash('Konfirmasi password tidak cocok.', 'danger')
            return redirect(url_for('change_password'))

        user.password = generate_password_hash(new_password, method='pbkdf2:sha256')
        db.session.commit()
        flash('Password berhasil diperbarui.', 'success')

        if session.get('role') == 'admin':
            return redirect(url_for('dashboard'))
        return redirect(url_for('employee_profile'))

    return render_template('change_password.html')

@app.route('/dashboard')
def dashboard():
    # Pastikan hanya user yang sudah login bisa mengakses
    if 'user_id' not in session:
        flash('Harap login terlebih dahulu.', 'warning')
        return redirect(url_for('login'))
    if session.get('role') != 'admin':
        flash('Anda tidak memiliki hak akses ke halaman ini.', 'danger')
        return redirect(url_for('index'))

    # Total karyawan
    total_employee = Employee.query.count()
    
    # Tentukan periode bulan berjalan dalam format "YYYY-MM"
    now = datetime.now()
    current_period = now.strftime("%Y-%m")
    current_year = now.strftime("%Y")
    start_period = f"{current_year}-01"
    end_period = current_period
    current_month = now.month
    
    # Total Gaji yang Dibayarkan (misal: jumlah gaji pokok dari payroll bulan ini)
    total_gaji = db.session.query(func.sum(Payroll.gaji_pokok)).filter(Payroll.pay_period == current_period).scalar() or 0
    
    # Total Potongan (misalnya: potongan_gaji + hutang + (alpha * (gaji_pokok/30)))
    total_potongan = db.session.query(
        func.sum(Payroll.potongan_gaji + Payroll.hutang + (Payroll.alpha * (Payroll.gaji_pokok/30)))
    ).filter(Payroll.pay_period == current_period).scalar() or 0

    # Rata-rata Take Home Pay
    avg_take_home = db.session.query(func.avg(Payroll.take_home_pay)).filter(Payroll.pay_period == current_period).scalar() or 0

    # Akumulasi Januari s.d periode aktif (tahun berjalan) dengan filter tahun tegas
    total_gaji_ytd = db.session.query(func.sum(Payroll.gaji_pokok)).filter(
        Payroll.pay_period >= start_period,
        Payroll.pay_period <= end_period
    ).scalar() or 0
    total_potongan_ytd = db.session.query(
        func.sum(Payroll.potongan_gaji + Payroll.hutang + (Payroll.alpha * (Payroll.gaji_pokok/30)))
    ).filter(
        Payroll.pay_period >= start_period,
        Payroll.pay_period <= end_period
    ).scalar() or 0
    avg_take_home_ytd = db.session.query(func.avg(Payroll.take_home_pay)).filter(
        Payroll.pay_period >= start_period,
        Payroll.pay_period <= end_period
    ).scalar() or 0

    # Ringkasan per bulan (tahun berjalan)
    agg_rows = db.session.query(
        Payroll.pay_period,
        func.sum(Payroll.gaji_pokok),
        func.sum(Payroll.potongan_gaji + Payroll.hutang + (Payroll.alpha * (Payroll.gaji_pokok/30))),
        func.avg(Payroll.take_home_pay)
    ).filter(
        func.substr(Payroll.pay_period, 1, 4) == current_year
    ).group_by(Payroll.pay_period).all()

    agg_map = {row[0]: row for row in agg_rows}
    month_names = ["Januari", "Februari", "Maret", "April", "Mei", "Juni",
                   "Juli", "Agustus", "September", "Oktober", "November", "Desember"]
    monthly_data = []
    for m in range(1, current_month + 1):
        period = f"{current_year}-{m:02d}"
        row = agg_map.get(period)
        gaji = float(row[1]) if row else 0.0
        potongan = float(row[2]) if row else 0.0
        avg_thp_month = float(row[3]) if row else 0.0
        monthly_data.append({
            "period": period,
            "label": month_names[m-1],
            "gaji": gaji,
            "potongan": potongan,
            "avg_thp": avg_thp_month
        })

    return render_template('dashboard.html',
                           total_employee=total_employee,
                           total_gaji=total_gaji,
                           total_potongan=total_potongan,
                           avg_take_home=avg_take_home,
                           total_gaji_ytd=total_gaji_ytd,
                           total_potongan_ytd=total_potongan_ytd,
                           avg_take_home_ytd=avg_take_home_ytd,
                           current_period=current_period,
                           current_year=current_year,
                           monthly_data=monthly_data)


@app.route('/admin/backup/settings', methods=['GET', 'POST'])
def backup_settings():
    if 'user_id' not in session or session.get('role') != 'admin':
        flash('Anda tidak memiliki hak akses.', 'danger')
        return redirect(url_for('login'))

    settings = get_backup_settings()
    if not settings:
        flash('Pengaturan backup belum tersedia. Jalankan migrasi database.', 'danger')
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        action = request.form.get('action', 'save')
        if action == 'save':
            enabled = request.form.get('enabled') == 'on'
            previous_enabled = settings.enabled
            previous_interval = settings.interval_hours

            try:
                interval_hours = int(request.form.get('interval_hours') or settings.interval_hours or 24)
            except ValueError:
                interval_hours = settings.interval_hours or 24
            try:
                retention_count = int(request.form.get('retention_count') or settings.retention_count or 7)
            except ValueError:
                retention_count = settings.retention_count or 7

            interval_hours = max(1, min(interval_hours, 720))
            retention_count = max(1, min(retention_count, 365))

            settings.enabled = enabled
            settings.interval_hours = interval_hours
            settings.retention_count = retention_count

            if enabled:
                if (not settings.next_run_at) or (not previous_enabled) or (previous_interval != interval_hours):
                    settings.next_run_at = compute_next_run(datetime.utcnow(), interval_hours)
            else:
                settings.next_run_at = None

            db.session.commit()
            flash('Pengaturan backup berhasil disimpan.', 'success')
            return redirect(url_for('backup_settings'))

        if action == 'run_now':
            try:
                backup_path = export_database_json()
                now = datetime.utcnow()
                settings.last_run_at = now
                settings.last_status = "success"
                settings.last_error = None
                settings.last_backup_file = os.path.basename(backup_path)
                if settings.enabled:
                    settings.next_run_at = compute_next_run(now, settings.interval_hours)
                db.session.commit()
                prune_old_backups(settings.retention_count)
                flash('Backup berhasil dibuat.', 'success')
            except Exception as exc:
                db.session.rollback()
                settings = get_backup_settings(create_if_missing=False)
                if settings:
                    settings.last_run_at = datetime.utcnow()
                    settings.last_status = "failed"
                    settings.last_error = str(exc)
                    settings.next_run_at = compute_next_run(settings.last_run_at, settings.interval_hours)
                    db.session.commit()
                flash(f'Gagal membuat backup: {exc}', 'danger')
            return redirect(url_for('backup_settings'))

    backup_files = list_backup_files(limit=10)
    return render_template(
        'backup_settings.html',
        settings=settings,
        backup_files=backup_files
    )


@app.route('/admin/backup/download/<path:filename>')
def download_backup(filename):
    if 'user_id' not in session or session.get('role') != 'admin':
        flash('Anda tidak memiliki hak akses.', 'danger')
        return redirect(url_for('login'))

    safe_name = secure_filename(filename or "")
    if not safe_name:
        abort(404)

    backup_dir = ensure_backup_dir()
    path = os.path.join(backup_dir, safe_name)
    if not os.path.isfile(path):
        abort(404)

    return send_file(path, as_attachment=True, download_name=safe_name)


@app.route('/admin/backup/delete/<path:filename>', methods=['POST'])
def delete_backup(filename):
    if 'user_id' not in session or session.get('role') != 'admin':
        flash('Anda tidak memiliki hak akses.', 'danger')
        return redirect(url_for('login'))

    safe_name = secure_filename(filename or "")
    if not safe_name:
        abort(404)

    backup_dir = ensure_backup_dir()
    path = os.path.join(backup_dir, safe_name)
    if not os.path.isfile(path):
        abort(404)

    try:
        os.remove(path)
        flash('Backup berhasil dihapus.', 'success')
    except OSError as exc:
        flash(f'Gagal menghapus backup: {exc}', 'danger')

    return redirect(url_for('backup_settings'))


@app.route('/admin/backup', methods=['POST'])
def admin_backup():
    if 'user_id' not in session or session.get('role') != 'admin':
        flash('Anda tidak memiliki hak akses.', 'danger')
        return redirect(url_for('login'))

    try:
        backup_path = export_database_json()
    except Exception as exc:
        flash(f'Gagal membuat backup: {exc}', 'danger')
        return redirect(url_for('dashboard'))

    return send_file(backup_path, as_attachment=True, download_name=os.path.basename(backup_path))

@app.route('/edit_user/<int:user_id>', methods=['GET', 'POST'])
def edit_user(user_id):
    # hanya admin
    if 'user_id' not in session or session.get('role') != 'admin':
        flash('Tidak memiliki akses.', 'danger')
        return redirect(url_for('login'))

    user = User.query.get_or_404(user_id)

    if request.method == 'POST':
        # -------- update data --------
        user.fullname = request.form.get('fullname')
        user.email    = request.form.get('email')
        user.role     = request.form.get('role', 'user')

        # opsi ganti password
        new_pw = request.form.get('password')
        if new_pw:
            user.password = generate_password_hash(new_pw, method='pbkdf2:sha256')

        db.session.commit()
        flash('Data user berhasil diperbarui.', 'success')
        return redirect(url_for('loans'))     # atau url_for('users') jika ada

    # GET → tampilkan form
    return render_template('edit_user.html', user=user)


@app.route('/delete_user/<int:user_id>', methods=['POST'])
def delete_user(user_id):
    # pastikan hanya admin
    if 'user_id' not in session or session.get('role') != 'admin':
        flash('Tidak memiliki akses.', 'danger')
        return redirect(url_for('login'))

    user = User.query.get_or_404(user_id)

    # Cegah menghapus diri sendiri
    if user.id == session['user_id']:
        flash('Tidak bisa menghapus akun yang sedang login.', 'warning')
        return redirect(url_for('loans'))

    db.session.delete(user)
    db.session.commit()
    flash('User berhasil dihapus.', 'success')
    return redirect(url_for('loans'))

@app.route('/employees')
def employees():
    # Pastikan hanya user yang sudah login bisa mengakses
    if 'user_id' not in session:
        flash('Harap login terlebih dahulu.', 'warning')
        return redirect(url_for('login'))
    if session.get('role') != 'admin':
        flash('Anda tidak memiliki hak akses ke halaman ini.', 'danger')
        return redirect(url_for('index'))

    active_filter = sa.or_(Employee.status.is_(None), Employee.status == 'active')
    employees_data = Employee.query.filter(active_filter).all()
    return render_template('employees.html', employees_data=employees_data)


@app.route('/employees/archive')
def employees_archive():
    if 'user_id' not in session:
        flash('Harap login terlebih dahulu.', 'warning')
        return redirect(url_for('login'))
    if session.get('role') != 'admin':
        flash('Anda tidak memiliki hak akses ke halaman ini.', 'danger')
        return redirect(url_for('index'))

    archived = Employee.query.filter(Employee.status == 'inactive').all()
    return render_template('employees_archive.html', employees_data=archived)


@app.route('/employees/<int:employee_id>/payrolls')
def employee_payroll_history(employee_id):
    if 'user_id' not in session:
        flash('Harap login terlebih dahulu.', 'warning')
        return redirect(url_for('login'))
    if session.get('role') != 'admin':
        flash('Tidak memiliki akses.', 'danger')
        return redirect(url_for('index'))

    emp = Employee.query.get_or_404(employee_id)
    history = Payroll.query.filter_by(employee_id=employee_id).order_by(Payroll.pay_period.desc()).all()
    return render_template('employee_payroll_history.html', employee=emp, payrolls=history)

@app.route('/employees/<int:employee_id>/archive', methods=['POST'])
def set_employee_inactive(employee_id):
    if 'user_id' not in session or session.get('role') != 'admin':
        flash('Tidak memiliki akses.', 'danger')
        return redirect(url_for('login'))

    emp = Employee.query.get_or_404(employee_id)
    emp.status = 'inactive'
    db.session.commit()
    flash('Karyawan berhasil diarsipkan.', 'success')
    return redirect(url_for('employees'))


@app.route('/employees/<int:employee_id>/activate', methods=['POST'])
def set_employee_active(employee_id):
    if 'user_id' not in session or session.get('role') != 'admin':
        flash('Tidak memiliki akses.', 'danger')
        return redirect(url_for('login'))

    emp = Employee.query.get_or_404(employee_id)
    emp.status = 'active'
    db.session.commit()
    flash('Status karyawan diubah menjadi aktif.', 'success')
    return redirect(url_for('employees_archive'))


@app.route('/components', methods=['GET', 'POST'])
def components():
    if 'user_id' not in session or session.get('role') != 'admin':
        flash('Tidak memiliki akses.', 'danger')
        return redirect(url_for('login'))

    if request.method == 'POST':
        code = (request.form.get('code') or '').strip()
        name = request.form.get('name')
        comp_type = request.form.get('comp_type')
        calc_type = request.form.get('calc_type', 'fixed')
        default_value = float(request.form.get('default_value') or 0)

        if not code or not name or comp_type not in ('gaji_pokok','tunjangan','potongan'):
            flash('Data komponen tidak valid.', 'danger')
            return redirect(url_for('components'))

        exists = CompensationComponent.query.filter_by(code=code).first()
        if exists:
            flash('Kode komponen sudah ada.', 'danger')
            return redirect(url_for('components'))

        comp = CompensationComponent(
            code=code, name=name, comp_type=comp_type,
            calc_type=calc_type, default_value=default_value, active=True
        )
        db.session.add(comp)
        db.session.commit()
        flash('Komponen berhasil ditambahkan.', 'success')
        return redirect(url_for('components'))

    comps = CompensationComponent.query.order_by(CompensationComponent.created_at.desc()).all()
    return render_template('components.html', components=comps)


@app.route('/components/<int:comp_id>/toggle', methods=['POST'])
def toggle_component(comp_id):
    if 'user_id' not in session or session.get('role') != 'admin':
        flash('Tidak memiliki akses.', 'danger')
        return redirect(url_for('login'))
    comp = CompensationComponent.query.get_or_404(comp_id)
    comp.active = not comp.active
    db.session.commit()
    flash('Status komponen diperbarui.', 'success')
    return redirect(url_for('components'))


@app.route('/components/<int:comp_id>/edit', methods=['GET', 'POST'])
def edit_component(comp_id):
    if 'user_id' not in session or session.get('role') != 'admin':
        flash('Tidak memiliki akses.', 'danger')
        return redirect(url_for('login'))
    comp = CompensationComponent.query.get_or_404(comp_id)
    if request.method == 'POST':
        code = (request.form.get('code') or '').strip()
        name = request.form.get('name')
        comp_type = request.form.get('comp_type')
        calc_type = request.form.get('calc_type', 'fixed')
        default_value = float(request.form.get('default_value') or 0)

        if not code or not name or comp_type not in ('gaji_pokok','tunjangan','potongan'):
            flash('Data komponen tidak valid.', 'danger')
            return redirect(url_for('edit_component', comp_id=comp_id))

        duplicate = CompensationComponent.query.filter(
            CompensationComponent.code == code,
            CompensationComponent.id != comp_id
        ).first()
        if duplicate:
            flash('Kode komponen sudah dipakai.', 'danger')
            return redirect(url_for('edit_component', comp_id=comp_id))

        comp.code = code
        comp.name = name
        comp.comp_type = comp_type
        comp.calc_type = calc_type
        comp.default_value = default_value
        db.session.commit()
        flash('Komponen berhasil diupdate.', 'success')
        return redirect(url_for('components'))

    return render_template('edit_component.html', comp=comp)


@app.route('/components/<int:comp_id>/delete', methods=['POST'])
def delete_component(comp_id):
    if 'user_id' not in session or session.get('role') != 'admin':
        flash('Tidak memiliki akses.', 'danger')
        return redirect(url_for('login'))

    comp = CompensationComponent.query.get_or_404(comp_id)
    linked = EmployeeCompensation.query.filter_by(component_id=comp_id).first()
    if linked:
        flash('Komponen sudah dipakai karyawan, tidak dapat dihapus. Nonaktifkan saja.', 'warning')
        return redirect(url_for('components'))

    db.session.delete(comp)
    db.session.commit()
    flash('Komponen dihapus.', 'success')
    return redirect(url_for('components'))


@app.route('/employees/<int:employee_id>/components', methods=['GET', 'POST'])
def employee_components(employee_id):
    if 'user_id' not in session or session.get('role') != 'admin':
        flash('Tidak memiliki akses.', 'danger')
        return redirect(url_for('login'))

    emp = Employee.query.get_or_404(employee_id)
    comps = CompensationComponent.query.filter_by(active=True).order_by(CompensationComponent.name).all()

    if request.method == 'POST':
        comp_id = request.form.get('component_id', type=int)
        value = request.form.get('value')
        start_period = request.form.get('start_period') or None
        comp = CompensationComponent.query.get(comp_id)
        if not comp:
            flash('Komponen tidak ditemukan.', 'danger')
            return redirect(url_for('employee_components', employee_id=employee_id))

        existing = EmployeeCompensation.query.filter_by(
            employee_id=employee_id, component_id=comp_id, start_period=start_period
        ).first()
        if existing:
            flash('Komponen sudah di-assign dengan periode tersebut.', 'warning')
            return redirect(url_for('employee_components', employee_id=employee_id))

        assign = EmployeeCompensation(
            employee_id=employee_id,
            component_id=comp_id,
            value=float(value) if value else None,
            start_period=start_period,
            active=True
        )
        db.session.add(assign)
        db.session.commit()
        flash('Komponen karyawan berhasil ditambahkan.', 'success')
        return redirect(url_for('employee_components', employee_id=employee_id))

    assigned = (EmployeeCompensation.query
                .filter_by(employee_id=employee_id)
                .join(CompensationComponent)
                .order_by(CompensationComponent.comp_type, EmployeeCompensation.start_period.desc())
                .all())
    return render_template('employee_components.html',
                           employee=emp,
                           components=comps,
                           assigned=assigned)


@app.route('/employees/components/<int:assign_id>/toggle', methods=['POST'])
def toggle_employee_component(assign_id):
    if 'user_id' not in session or session.get('role') != 'admin':
        flash('Tidak memiliki akses.', 'danger')
        return redirect(url_for('login'))

    assign = EmployeeCompensation.query.get_or_404(assign_id)
    assign.active = not assign.active
    db.session.commit()
    flash('Status komponen karyawan diperbarui.', 'success')
    return redirect(url_for('employee_components', employee_id=assign.employee_id))


# Contoh Route untuk menambah karyawan baru
@app.route('/add_employee', methods=['POST'])
def add_employee():
    if 'user_id' not in session or session.get('role') != 'admin':
        flash('Tidak memiliki akses.', 'danger')
        return redirect(url_for('index'))
    
    name = request.form.get('name')
    position = request.form.get('position')
    address = request.form.get('address')
    phone = request.form.get('phone')
    no_rek = request.form.get('no_rek')  # <-- baru
    hire_date_str = request.form.get('hire_date')  # "2025-03-10" (YYYY-MM-DD)

    # 1) Dapatkan ID terakhir, siapkan auto increment
    last_employee = Employee.query.order_by(Employee.id.desc()).first()
    if last_employee:
        next_id = last_employee.id + 1
    else:
        next_id = 1
    
    # 2) Generate NIK (misal "EMP0001")
    new_nik = f"EMP{next_id:04d}"

    # 3) Parse hire_date (jika user isi)
    parsed_hire_date = None
    if hire_date_str:
        try:
            parsed_hire_date = datetime.strptime(hire_date_str, "%Y-%m-%d").date()
        except ValueError:
            # Jika format tanggal salah
            parsed_hire_date = None

    # 4) Buat objek Employee baru
    new_emp = Employee(
        nik=new_nik,
        name=name,
        position=position,
        address=address,
        phone=phone,
        no_rek=no_rek,          # <-- baru
        hire_date=parsed_hire_date
    )
    
    db.session.add(new_emp)
    db.session.commit()
    
    flash('Karyawan baru berhasil ditambahkan.', 'success')
    return redirect(url_for('employees'))


@app.route('/payrolls')
def payrolls():
    if 'user_id' not in session or session.get('role') != 'admin':
        flash('Anda tidak memiliki hak akses.', 'danger')
        return redirect(url_for('login'))

    # ------- parameter filter lama -------
    keyword   = request.args.get('keyword', '').strip()
    pay_month = request.args.get('pay_period', '').strip()

    # ------- parameter pagination baru -------
    page      = request.args.get('page', 1, type=int)
    per_page  = request.args.get('per_page', 10, type=int)
    if per_page not in (10, 50, 100):          # fallback aman
        per_page = 10

    # ------- query dasar -------
    query = Payroll.query.join(Employee)
    if keyword:
        query = query.filter(Employee.name.ilike(f"%{keyword}%"))
    if pay_month:
        query = query.filter(Payroll.pay_period == pay_month)

    draft_count = query.filter(Payroll.status != 'approved').count()

    pagination   = query.order_by(Payroll.id.desc()).paginate(
                       page=page, per_page=per_page, error_out=False)
    payrolls_pag = pagination.items

    return render_template('payrolls.html',
                           payrolls   = payrolls_pag,
                           pagination = pagination,
                           per_page   = per_page,
                           draft_count = draft_count)



# === ADD PAYROLL BARU === ----------------------------------------------------
@app.route('/add_payroll', methods=['GET', 'POST'])
def add_payroll():
    # ---------- otorisasi ----------
    if 'user_id' not in session:
        flash('Harap login terlebih dahulu.', 'warning')
        return redirect(url_for('login'))
    if session.get('role') != 'admin':
        flash('Anda tidak memiliki hak akses.', 'danger')
        return redirect(url_for('index'))

    # ============================= POST (submit form) =========================
    if request.method == 'POST':
        # helper rupiah → float
        parse_currency = lambda v: float((v or '0').replace('.', ''))

        # nilai form dasar
        employee_id  = int(request.form['employee_id'])
        pay_period   = request.form['pay_period']
        end_date     = parse_period_to_date(pay_period)

        gaji_pokok           = parse_currency(request.form.get('gaji_pokok'))
        bpjs_ketenagakerjaan = parse_currency(request.form.get('bpjs_ketenagakerjaan'))
        tunjangan_makan      = parse_currency(request.form.get('tunjangan_makan'))
        tunjangan_transport  = parse_currency(request.form.get('tunjangan_transport'))
        tunjangan_lainnya    = parse_currency(request.form.get('tunjangan_lainnya'))
        potongan_gaji        = parse_currency(request.form.get('potongan_gaji'))
        upah_lembur          = parse_currency(request.form.get('upah_lembur'))
        alpha                = int(request.form.get('alpha') or 0)

        # -------- THR --------
        emp       = Employee.query.get(employee_id)
        thr_value = 0.0
        if request.form.get('is_thr') == 'on' and emp and end_date:
            months = months_of_service(emp.hire_date, end_date)
            thr_value = gaji_pokok if months >= 12 else (months / 12.0) * gaji_pokok

        # -------- proses pembayaran (angsuran) yang dipilih --------
        selected_payments = request.form.getlist('payments')            # ["12", "18", ...]
        total_hutang = 0.0
        rows_to_link = []                                               # simpan detail

        for pid in selected_payments:
            payment = Payment.query.get(int(pid))
            if not payment or payment.status != 'approved':
                continue
            loan = payment.loan
            if loan.employee_id != employee_id:
                continue

            inst_no = loan.installments_paid + 1        # cicilan berikutnya
            amt     = payment.payment_amount

            total_hutang += amt
            rows_to_link.append({
                'loan': loan,
                'payment': payment,
                'number': inst_no,
                'amount': amt
            })

        # -------- validasi duplikasi payroll periode --------
        existing = Payroll.query.filter_by(employee_id=employee_id, pay_period=pay_period).first()
        if existing:
            flash('Payroll untuk karyawan dan periode tersebut sudah ada.', 'danger')
            return redirect(url_for('add_payroll', employee_id=employee_id))

        # -------- buat payroll --------
        payroll = Payroll(
            employee_id          = employee_id,
            pay_period           = pay_period,
            gaji_pokok           = gaji_pokok,
            bpjs_ketenagakerjaan = bpjs_ketenagakerjaan,
            tunjangan_makan      = tunjangan_makan,
            tunjangan_transport  = tunjangan_transport,
            tunjangan_lainnya    = tunjangan_lainnya,
            potongan_gaji        = potongan_gaji,
            alpha                = alpha,
            hutang               = total_hutang,        # total cicilan dipilih
            upah_lembur          = upah_lembur,
            thr                  = thr_value,
            loan_deduction       = 0,                   # sudah tidak dipakai
            status               = 'draft'
        )
        db.session.add(payroll)
        db.session.flush()  # dapatkan payroll.id sebelum insert junction

        # -------- link ke PayrollLoan + update status --------
        for r in rows_to_link:
            db.session.add(PayrollLoan(
                payroll_id         = payroll.id,
                loan_id            = r['loan'].id,
                payment_id         = r['payment'].id,      # <– tambahkan
                installment_number = r['number'],
                amount             = r['amount']
            ))

            # tandai payment sudah diposting ke payroll
            r['payment'].status = 'posted'

            # update progress pinjaman
            r['loan'].installments_paid += 1
            if r['loan'].installments_paid >= r['loan'].tenor:
                r['loan'].status = 'completed'

        db.session.commit()
        log_action('create_payroll', 'payroll', payroll.id, f'periode={pay_period}')
        flash('Data payroll berhasil ditambahkan.', 'success')
        return redirect(url_for('payrolls'))

    # ============================= GET (form awal / ganti karyawan) ===========
    active_filter = sa.or_(Employee.status.is_(None), Employee.status == 'active')
    employees       = Employee.query.filter(active_filter).all()
    selected_emp_id = request.args.get('employee_id', type=int)
    payment_list    = approved_payments(selected_emp_id) if selected_emp_id else []
    master_default  = get_component_totals(selected_emp_id, request.args.get('pay_period') or '') if selected_emp_id else None

    return render_template(
        'add_payroll.html',
        employees        = employees,
        payment_choices  = payment_list,
        selected_emp_id  = selected_emp_id,
        master_default   = master_default
    )


# ----------------------------------------------------------------------------- 



@app.route('/edit_payroll/<int:payroll_id>', methods=['GET', 'POST'])
def edit_payroll(payroll_id):
    if 'user_id' not in session or session.get('role') != 'admin':
        flash('Tidak memiliki akses.', 'danger')
        return redirect(url_for('login'))
    
    payroll = Payroll.query.get_or_404(payroll_id)

    if payroll.status == 'approved':
        flash('Payroll yang sudah disetujui/terkunci tidak dapat diedit.', 'warning')
        return redirect(url_for('payslip', payroll_id=payroll.id))
    
    if request.method == 'GET':
        active_filter = sa.or_(Employee.status.is_(None), Employee.status == 'active', Employee.id == payroll.employee_id)
        employees = Employee.query.filter(active_filter).all()
        return render_template('edit_payroll.html', payroll=payroll, employees=employees)
    
    # POST: proses data form
    employee_id = request.form.get('employee_id')
    pay_period = request.form.get('pay_period')  # misal "2025-03"

    dup = Payroll.query.filter(
        Payroll.employee_id == employee_id,
        Payroll.pay_period == pay_period,
        Payroll.id != payroll.id
    ).first()
    if dup:
        flash('Payroll untuk karyawan dan periode tersebut sudah ada.', 'danger')
        return redirect(url_for('edit_payroll', payroll_id=payroll.id))

    # Konversi pay_period -> end_date
    end_date = parse_period_to_date(pay_period)  # date(2025, 3, 1) atau None

    # Bersihkan format rp -> hilangkan titik sebelum float()
    gaji_pokok_str = request.form.get('gaji_pokok') or '0'
    gaji_pokok_str = gaji_pokok_str.replace('.', '')
    gaji_pokok = float(gaji_pokok_str)
    
    # Ambil data karyawan
    emp = Employee.query.get(employee_id)

    # THR logic
    is_thr = request.form.get('is_thr')  # "on" or None
    thr_value = 0.0
    if is_thr == "on" and emp and end_date:
        # Hitung masa kerja
        masa_kerja = months_of_service(emp.hire_date, end_date)
        if masa_kerja >= 12:
            thr_value = gaji_pokok
        else:
            thr_value = (masa_kerja / 12.0) * gaji_pokok
    
    # Update payroll
    payroll.employee_id = employee_id
    payroll.pay_period = pay_period
    payroll.gaji_pokok = gaji_pokok
    payroll.thr = thr_value  # simpan hasil THR

    # BPJS
    bpjs_str = request.form.get('bpjs_ketenagakerjaan') or '0'
    bpjs_str = bpjs_str.replace('.', '')
    bpjs_ketenagakerjaan = float(bpjs_str)
    payroll.bpjs_ketenagakerjaan = bpjs_ketenagakerjaan

    # Tunjangan makan
    tunj_makan_str = request.form.get('tunjangan_makan') or '0'
    tunj_makan_str = tunj_makan_str.replace('.', '')
    tunjangan_makan = float(tunj_makan_str)
    payroll.tunjangan_makan = tunjangan_makan

    # Tunjangan transport
    tunj_transport_str = request.form.get('tunjangan_transport') or '0'
    tunj_transport_str = tunj_transport_str.replace('.', '')
    tunjangan_transport = float(tunj_transport_str)
    payroll.tunjangan_transport = tunjangan_transport

    # Tunjangan lainnya
    tunj_lain_str = request.form.get('tunjangan_lainnya') or '0'
    tunj_lain_str = tunj_lain_str.replace('.', '')
    tunjangan_lainnya = float(tunj_lain_str)
    payroll.tunjangan_lainnya = tunjangan_lainnya

    # Potongan gaji
    pot_gaji_str = request.form.get('potongan_gaji') or '0'
    pot_gaji_str = pot_gaji_str.replace('.', '')
    potongan_gaji = float(pot_gaji_str)
    payroll.potongan_gaji = potongan_gaji

    # Alpha
    alpha = int(request.form.get('alpha') or 0)
    payroll.alpha = alpha

    # Upah lembur
    lembur_str = request.form.get('upah_lembur') or '0'
    lembur_str = lembur_str.replace('.', '')
    upah_lembur = float(lembur_str)
    payroll.upah_lembur = upah_lembur

    db.session.commit()
    log_action('update_payroll', 'payroll', payroll.id, f'periode={pay_period}')
    flash('Data payroll berhasil diupdate.', 'success')
    return redirect(url_for('payslip', payroll_id=payroll.id))



@app.route('/delete_payroll/<int:payroll_id>')
def delete_payroll(payroll_id):
    if 'user_id' not in session or session.get('role') != 'admin':
        flash('Tidak memiliki akses.', 'danger')
        return redirect(url_for('login'))

    payroll = Payroll.query.get_or_404(payroll_id)

    if payroll.status == 'approved':
        flash('Payroll yang sudah disetujui/terkunci tidak dapat dihapus.', 'warning')
        return redirect(url_for('payrolls'))

    # --- kembalikan status tiap angsuran ---
    for pl in payroll.installments:
        pay  = pl.payment
        loan = pl.loan

        # 1) balikan Payment             → approved
        pay.status = 'approved'

        # 2) mundurkan progres pinjaman
        if loan.installments_paid > 0:
            loan.installments_paid -= 1
        if loan.status == 'completed':
            loan.status = 'approved'  # aktif lagi kalau belum lunas

    db.session.delete(payroll)
    db.session.commit()
    log_action('delete_payroll', 'payroll', payroll.id)
    flash('Payroll dihapus & angsuran dikembalikan.', 'success')
    return redirect(url_for('payrolls'))


@app.route('/edit_employee/<int:employee_id>', methods=['GET', 'POST'])
def edit_employee(employee_id):
    if 'user_id' not in session or session.get('role') != 'admin':
        flash('Tidak memiliki akses.', 'danger')
        return redirect(url_for('login'))

    emp = Employee.query.get_or_404(employee_id)

    if request.method == 'GET':
        return render_template('edit_employee.html', emp=emp)

    # POST: update data
    emp.name = request.form.get('name')
    emp.position = request.form.get('position')
    emp.phone = request.form.get('phone')
    emp.address = request.form.get('address')
    emp.no_rek = request.form.get('no_rek')  # <-- baru

    hire_date_str = request.form.get('hire_date')
    if hire_date_str:
        try:
            emp.hire_date = datetime.strptime(hire_date_str, "%Y-%m-%d").date()
        except ValueError:
            emp.hire_date = None

    # NIK bisa dibiarkan read-only, jadi tidak diupdate
    # Jika ingin diupdate, lakukan:
    # new_nik = request.form.get('nik')
    # emp.nik = new_nik or emp.nik

    db.session.commit()
    flash('Data karyawan berhasil diperbarui.', 'success')
    return redirect(url_for('employees'))


@app.route('/delete_employee/<int:employee_id>')
def delete_employee(employee_id):
    if 'user_id' not in session or session.get('role') != 'admin':
        flash('Tidak memiliki akses.', 'danger')
        return redirect(url_for('login'))

    emp = Employee.query.get_or_404(employee_id)

    has_approved = Payroll.query.filter_by(employee_id=employee_id, status='approved').first()
    if has_approved:
        flash('Tidak dapat menghapus karyawan dengan payroll yang sudah disetujui.', 'warning')
        return redirect(url_for('employees'))
    
    # Hapus terlebih dahulu semua data payroll yang terkait dengan karyawan ini
    payrolls = Payroll.query.filter_by(employee_id=employee_id).all()
    for payroll in payrolls:
        db.session.delete(payroll)
    
    # Setelah payroll terkait dihapus, hapus data karyawan
    db.session.delete(emp)
    db.session.commit()
    
    flash('Data karyawan beserta payroll terkait berhasil dihapus.', 'success')
    return redirect(url_for('employees'))


@app.route('/payslip/<int:payroll_id>')
def payslip(payroll_id):
    # Pastikan hanya admin/login user
    if 'user_id' not in session:
        flash('Tidak memiliki akses.', 'danger')
        return redirect(url_for('login'))

    payroll = Payroll.query.get_or_404(payroll_id)
    # admin selalu boleh, user hanya jika payroll milik dirinya
    if session.get('role') != 'admin':
        emp = Employee.query.filter_by(user_id=session.get('user_id')).first()
        if not emp or payroll.employee_id != emp.id:
            flash('Tidak memiliki akses ke slip ini.', 'danger')
            return redirect(url_for('employee_dashboard'))

    return render_template('payslip.html', payroll=payroll)

@app.route('/payrolls/<int:payroll_id>/approve', methods=['POST'])
def approve_payroll(payroll_id):
    if 'user_id' not in session or session.get('role') != 'admin':
        flash('Tidak memiliki akses.', 'danger')
        return redirect(url_for('login'))

    payroll = Payroll.query.get_or_404(payroll_id)
    if payroll.status == 'approved':
        flash('Payroll sudah disetujui.', 'info')
        return redirect(url_for('payrolls'))

    payroll.status = 'approved'
    payroll.approved_by = session.get('user_id')
    payroll.approved_at = datetime.now(timezone.utc)
    db.session.commit()
    log_action('approve_payroll', 'payroll', payroll.id, f'approved_by={payroll.approved_by}')
    flash('Payroll telah disetujui dan dikunci.', 'success')
    return redirect(url_for('payrolls'))


@app.route('/payrolls/bulk_approve', methods=['POST'])
def bulk_approve_payrolls():
    if 'user_id' not in session or session.get('role') != 'admin':
        flash('Tidak memiliki akses.', 'danger')
        return redirect(url_for('login'))

    ids = request.form.getlist('payroll_ids')
    if not ids:
        flash('Tidak ada payroll yang dipilih.', 'warning')
        return redirect(url_for('payrolls'))

    # hanya approve yang masih draft
    to_approve = Payroll.query.filter(Payroll.id.in_(ids), Payroll.status != 'approved').all()
    for p in to_approve:
        p.status = 'approved'
        p.approved_by = session.get('user_id')
        p.approved_at = datetime.now(timezone.utc)
        log_action('approve_payroll', 'payroll', p.id, 'bulk')
    db.session.commit()

    flash(f'{len(to_approve)} payroll berhasil disetujui.', 'success')
    return redirect(url_for('payrolls'))

# Export Payroll
@app.route('/export/payrolls/<string:file_format>')
def export_payrolls(file_format):
    # --- otorisasi ---
    if 'user_id' not in session or session.get('role') != 'admin':
        flash('Tidak memiliki akses.', 'danger')
        return redirect(url_for('login'))

    # --- ambil filter yg sama dgn halaman /payrolls ---
    keyword          = request.args.get('keyword', '').strip()
    pay_period       = request.args.get('pay_period', '').strip()

    query = Payroll.query.join(Employee)
    if keyword:
        query = query.filter(Employee.name.ilike(f"%{keyword}%"))
    if pay_period:
        query = query.filter(Payroll.pay_period == pay_period)

    payrolls = query.all()          # <-- sekarang sudah ter-filter

    # --- siapkan data dict - sama seperti sebelumnya ---
    data = [{
        'ID'                    : p.id,
        'Karyawan'              : p.employee.name,
        'Periode'               : p.pay_period,
        'Gaji Pokok'            : p.gaji_pokok,
        'BPJS Ketenagakerjaan'  : p.bpjs_ketenagakerjaan,
        'Tunjangan Makan'       : p.tunjangan_makan,
        'Tunjangan Transport'   : p.tunjangan_transport,
        'Tunjangan Lainnya'     : p.tunjangan_lainnya,
        'THR'                   : p.thr,
        'Potongan Gaji'         : p.potongan_gaji,
        'Alpha'                 : p.alpha,
        'Total Potongan'        : p.total_deductions,
        'Upah Lembur'           : p.upah_lembur,
        'Take Home Pay'         : p.take_home_pay,
    } for p in payrolls]

    # --- EXPORT EXCEL ---
    if file_format == 'excel':
        df      = pd.DataFrame(data)
        output  = io.BytesIO()
        with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
            df.to_excel(writer, index=False, sheet_name='Payrolls')
        output.seek(0)
        response = make_response(output.read())
        response.headers['Content-Disposition'] = 'attachment; filename=payrolls.xlsx'
        response.headers['Content-Type']        = ('application/vnd.openxmlformats-'
                                                   'officedocument.spreadsheetml.sheet')
        return response

    # --- EXPORT PDF ---
    elif file_format == 'pdf':
        rendered = render_template('export_payrolls_pdf.html', payrolls=payrolls)
        config = get_pdfkit_config()
        if not config:
            flash('Export PDF gagal: wkhtmltopdf tidak ditemukan. '
                  'Install wkhtmltopdf dan/atau set environment WKHTMLTOPDF_PATH.', 'danger')
            return redirect(url_for('payrolls',
                                    keyword=keyword,
                                    pay_period=pay_period))
        pdf      = pdfkit.from_string(rendered, False, configuration=config)
        response = make_response(pdf)
        response.headers['Content-Disposition'] = 'attachment; filename=payrolls.pdf'
        response.headers['Content-Type']        = 'application/pdf'
        return response

    flash('Format tidak didukung.', 'warning')
    return redirect(url_for('payrolls'))


@app.route('/reports/compliance')
def compliance_report():
    if 'user_id' not in session or session.get('role') != 'admin':
        flash('Tidak memiliki akses.', 'danger')
        return redirect(url_for('login'))

    pay_period = request.args.get('pay_period', '').strip()
    if not pay_period:
        flash('Periode wajib diisi untuk laporan kepatuhan.', 'warning')
        return redirect(url_for('payrolls'))

    payrolls = Payroll.query.join(Employee).filter(Payroll.pay_period == pay_period).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Karyawan', 'NIK', 'Periode', 'Gaji Pokok', 'BPJS', 'Tunjangan', 'Potongan', 'THP'])
    for p in payrolls:
        tunj = (p.tunjangan_makan + p.tunjangan_transport + p.tunjangan_lainnya + p.thr + p.upah_lembur)
        pot = p.total_deductions
        writer.writerow([
            p.employee.name,
            p.employee.nik,
            p.pay_period,
            int(p.gaji_pokok or 0),
            int(p.bpjs_ketenagakerjaan or 0),
            int(tunj),
            int(pot),
            int(p.take_home_pay or 0)
        ])
    resp = make_response(output.getvalue())
    resp.headers['Content-Disposition'] = f'attachment; filename=compliance_{pay_period}.csv'
    resp.headers['Content-Type'] = 'text/csv'
    return resp


@app.route('/reports/bank_export')
def bank_export():
    if 'user_id' not in session or session.get('role') != 'admin':
        flash('Tidak memiliki akses.', 'danger')
        return redirect(url_for('login'))

    pay_period = request.args.get('pay_period', '').strip()
    if not pay_period:
        flash('Periode wajib diisi untuk ekspor bank.', 'warning')
        return redirect(url_for('payrolls'))

    payrolls = Payroll.query.join(Employee).filter(Payroll.pay_period == pay_period).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Nama', 'NIK', 'No Rekening', 'Jumlah Transfer'])
    for p in payrolls:
        writer.writerow([
            p.employee.name,
            p.employee.nik,
            p.employee.no_rek or '',
            int(p.take_home_pay or 0)
        ])
    resp = make_response(output.getvalue())
    resp.headers['Content-Disposition'] = f'attachment; filename=bank_export_{pay_period}.csv'
    resp.headers['Content-Type'] = 'text/csv'
    return resp


# Export Employee
@app.route('/export/employees/<string:file_format>')
def export_employees(file_format):
    if 'user_id' not in session or session.get('role') != 'admin':
        flash('Tidak memiliki akses.', 'danger')
        return redirect(url_for('login'))
    
    employees = Employee.query.all()
    data = []
    for emp in employees:
        data.append({
            'ID': emp.id,
            'NIK': emp.nik,
            'Nama': emp.name,
            'Jabatan': emp.position,
            'Alamat': emp.address,
            'Telepon': emp.phone,
            'No. Rekening'  : emp.no_rek,  # <-- baru
            'Tanggal Masuk': emp.hire_date.strftime("%d/%m/%Y") if emp.hire_date else ''
        })
    
    if file_format == 'excel':
        df = pd.DataFrame(data)
        output = io.BytesIO()
        writer = pd.ExcelWriter(output, engine='xlsxwriter')
        df.to_excel(writer, index=False, sheet_name='Employees')
        writer.save()
        output.seek(0)
        response = make_response(output.read())
        response.headers['Content-Disposition'] = 'attachment; filename=employees.xlsx'
        response.headers['Content-Type'] = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        return response

    elif file_format == 'pdf':
        # Render template khusus untuk export PDF karyawan (buat file export_employees_pdf.html)
        rendered = render_template('export_employees_pdf.html', employees=employees)
        config = get_pdfkit_config()
        if not config:
            flash('Export PDF gagal: wkhtmltopdf tidak ditemukan. '
                  'Install wkhtmltopdf dan/atau set environment WKHTMLTOPDF_PATH.', 'danger')
            return redirect(url_for('employees'))
        pdf = pdfkit.from_string(rendered, False, configuration=config)
        response = make_response(pdf)
        response.headers['Content-Disposition'] = 'attachment; filename=employees.pdf'
        response.headers['Content-Type'] = 'application/pdf'
        return response

    else:
        flash('Format tidak didukung.', 'warning')
        return redirect(url_for('employees'))

# === ROUTE UNTUK PENGAJUAN PINJAMAN (Karyawan) ===
@app.route('/apply_loan', methods=['GET', 'POST'])
def apply_loan():
    if 'user_id' not in session:
        flash('Harap login terlebih dahulu.', 'warning')
        return redirect(url_for('login'))
    
    # Ambil record Employee berdasarkan user_id
    employee = Employee.query.filter_by(user_id=session.get('user_id')).first()
    if not employee:
        flash('Anda belum terdaftar sebagai karyawan. Silakan hubungi admin.', 'danger')
        return redirect(url_for('employee_dashboard'))
    
    # Cek apakah terdapat pinjaman aktif (status selain 'completed')
    active_loan = Loan.query.filter(
        Loan.employee_id == employee.id,
        Loan.status.in_(('pending', 'approved'))
    ).first()
    if active_loan:
        flash('Anda masih memiliki pinjaman yang belum lunas. Harap lunasi pinjaman sebelumnya sebelum mengajukan pinjaman baru.', 'warning')
        return redirect(url_for('loans'))
    
    if request.method == 'POST':
        try:
            amount = float(request.form.get('amount'))
            tenor = int(request.form.get('tenor'))
            interest_rate = float(request.form.get('interest_rate', 0))
        except (ValueError, TypeError):
            flash('Data yang dimasukkan tidak valid.', 'danger')
            return redirect(url_for('apply_loan'))
        
        reason = request.form.get('reason')
        
        # Hitung total pinjaman dan cicilan per bulan
        total_amount = amount + (amount * (interest_rate / 100))
        installment = total_amount / tenor
        
        new_loan = Loan(
            employee_id=employee.id,  # gunakan Employee.id, bukan session.get('user_id')
            amount=amount,
            tenor=tenor,
            interest_rate=interest_rate,
            installment=installment,
            reason=reason,
            status="pending"
        )
        db.session.add(new_loan)
        db.session.commit()
        log_action('apply_loan', 'loan', new_loan.id)
        flash('Pengajuan pinjaman berhasil diajukan dan menunggu persetujuan.', 'success')
        return redirect(url_for('loans'))
    
    # Kirim data employee ke template agar nama karyawan bisa ditampilkan
    return render_template('apply_loan.html', employee=employee)

@app.route('/pay_loan/<int:loan_id>', methods=['POST'])
def pay_loan(loan_id):
    if 'user_id' not in session:
        flash('Harap login terlebih dahulu.', 'warning')
        return redirect(url_for('login'))
    
    employee = Employee.query.filter_by(user_id=session.get('user_id')).first()
    if not employee:
        flash('Data karyawan tidak ditemukan.', 'danger')
        return redirect(url_for('employee_dashboard'))
    
    loan = Loan.query.get_or_404(loan_id)
    if loan.employee_id != employee.id:
        flash('Anda tidak memiliki akses ke pinjaman ini.', 'danger')
        return redirect(url_for('employee_dashboard'))
    
    try:
        # Hilangkan titik pemisah ribuan agar konversi float benar
        payment_input = request.form.get('payment_amount')
        payment_amount = float(payment_input.replace('.', ''))
    except (TypeError, ValueError):
        flash('Jumlah pembayaran tidak valid.', 'danger')
        return redirect(url_for('employee_dashboard'))
    
    if payment_amount <= 0:
        flash('Jumlah pembayaran harus lebih dari 0.', 'danger')
        return redirect(url_for('employee_dashboard'))
    
    total_loan = loan.amount + (loan.amount * loan.interest_rate / 100)
    # Sertakan jumlah yang sudah disubmit (baik pending maupun approved) agar tidak terjadi double submission
    total_submitted = sum(p.payment_amount for p in loan.payments if p.status in ['approved','pending'])
    remaining = total_loan - total_submitted

    if payment_amount > remaining:
        flash('Jumlah pembayaran melebihi sisa hutang.', 'danger')
        return redirect(url_for('employee_dashboard'))
    
    # Simpan pembayaran dengan status pending
    new_payment = Payment(
        loan_id=loan.id,
        payment_amount=payment_amount,
        status='pending'
    )
    db.session.add(new_payment)
    db.session.commit()
    
    flash('Pembayaran telah diajukan, menunggu persetujuan admin.', 'success')
    return redirect(url_for('employee_dashboard'))


@app.route('/approve_payment/<int:payment_id>')
def approve_payment(payment_id):
    if 'user_id' not in session or session.get('role') != 'admin':
        flash('Tidak memiliki akses.', 'danger')
        return redirect(url_for('login'))
    
    payment = Payment.query.get_or_404(payment_id)
    payment.status = 'approved'
    db.session.commit()
    log_action('approve_payment', 'payment', payment.id)
    
    # Hitung total pembayaran yang sudah disetujui untuk pinjaman ini
    loan = payment.loan
    total_approved = sum(p.payment_amount for p in loan.payments if p.status in ('approved', 'posted'))
    total_loan = loan.amount + (loan.amount * loan.interest_rate / 100)
    if total_approved >= total_loan:
        loan.status = 'completed'
    db.session.commit()
    
    flash('Pembayaran telah disetujui.', 'success')
    return redirect(url_for('loans'))

@app.route('/reject_payment/<int:payment_id>')
def reject_payment(payment_id):
    if 'user_id' not in session or session.get('role') != 'admin':
        flash('Tidak memiliki akses.', 'danger')
        return redirect(url_for('login'))
    
    payment = Payment.query.get_or_404(payment_id)
    payment.status = 'rejected'
    db.session.commit()
    
    flash('Pembayaran telah ditolak. Silakan minta user untuk melakukan pembayaran ulang.', 'warning')
    return redirect(url_for('loans'))

@app.route('/loan_payments/<int:loan_id>')
def loan_payments(loan_id):
    if 'user_id' not in session:
        flash('Harap login terlebih dahulu.', 'warning')
        return redirect(url_for('login'))
    loan = Loan.query.get_or_404(loan_id)
    # Jika pengguna bukan admin, pastikan pinjaman tersebut milik karyawan yang sedang login
    if session.get('role') != 'admin':
        employee = Employee.query.filter_by(user_id=session.get('user_id')).first()
        if loan.employee_id != employee.id:
            flash('Anda tidak memiliki akses ke data ini.', 'danger')
            return redirect(url_for('employee_dashboard'))
    return render_template('loan_payments.html', loan=loan)



@app.route('/delete_loan/<int:loan_id>')
def delete_loan(loan_id):
    # Hanya admin yang dapat menghapus data pinjaman
    if 'user_id' not in session or session.get('role') != 'admin':
        flash('Tidak memiliki akses.', 'danger')
        return redirect(url_for('login'))
    
    loan = Loan.query.get_or_404(loan_id)
    if loan.status in ('approved', 'completed'):
        flash('Pinjaman yang sudah disetujui/lunas tidak bisa dihapus. Tolong nonaktifkan atau biarkan sebagai arsip.', 'warning')
        return redirect(url_for('loans'))

    for pay in list(loan.payments):
        db.session.delete(pay)

    db.session.delete(loan)
    db.session.commit()
    flash('Data pinjaman berhasil dihapus.', 'success')
    return redirect(url_for('loans'))


# === ROUTE UNTUK MELIHAT PENGAJUAN PINJAMAN ===
@app.route('/loans')
def loans():
    if 'user_id' not in session:
        flash('Harap login terlebih dahulu.', 'warning')
        return redirect(url_for('login'))

    # ==== ADMIN ====
    if session.get('role') == 'admin':
        loan_list        = Loan.query.order_by(Loan.application_date.desc()).all()
        pending_payments = Payment.query.filter_by(status='pending')\
                                        .order_by(Payment.payment_date.desc()).all()
        users_list       = User.query.order_by(User.id).all()          # ← ambil data user

        return render_template('loans.html',
                               loans=loan_list,
                               pending_payments=pending_payments,
                               users=users_list)                      # ← kirim ke template

    # ==== USER (karyawan) ====
    employee = Employee.query.filter_by(user_id=session.get('user_id')).first()
    if not employee:
        flash('Data karyawan tidak ditemukan.', 'danger')
        return redirect(url_for('employee_dashboard'))

    loan_list = Loan.query.filter_by(employee_id=employee.id)\
                          .order_by(Loan.application_date.desc()).all()
    return render_template('loans.html', loans=loan_list, employee=employee)


@app.route('/audit_logs')
def audit_logs():
    if 'user_id' not in session or session.get('role') != 'admin':
        flash('Tidak memiliki akses.', 'danger')
        return redirect(url_for('login'))

    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 50, type=int)
    if per_page not in (50, 100, 200):
        per_page = 50

    pagination = AuditLog.query.order_by(AuditLog.created_at.desc()).paginate(
        page=page, per_page=per_page, error_out=False
    )
    logs = pagination.items
    return render_template('audit_logs.html', logs=logs, pagination=pagination, per_page=per_page)





# === ROUTE UNTUK MENYETUJUI/PENOLAKAN PINJAMAN (Admin) ===
@app.route('/approve_loan/<int:loan_id>')
def approve_loan(loan_id):
    if 'user_id' not in session or session.get('role') != 'admin':
        flash('Tidak memiliki akses.', 'danger')
        return redirect(url_for('login'))
    
    loan = Loan.query.get_or_404(loan_id)
    loan.status = 'approved'
    loan.approval_date = datetime.now(timezone.utc)   # timezone-aware
    db.session.commit()
    log_action('approve_loan', 'loan', loan.id)
    flash('Pinjaman disetujui.', 'success')
    return redirect(url_for('loans'))

@app.route('/reject_loan/<int:loan_id>')
def reject_loan(loan_id):
    if 'user_id' not in session or session.get('role') != 'admin':
        flash('Tidak memiliki akses.', 'danger')
        return redirect(url_for('login'))
    
    loan = Loan.query.get_or_404(loan_id)
    loan.status = 'rejected'
    loan.approval_date = datetime.now(timezone.utc)
    db.session.commit()
    log_action('reject_loan', 'loan', loan.id)
    flash('Pinjaman ditolak.', 'warning')
    return redirect(url_for('loans'))




@app.route('/employee_dashboard')
def employee_dashboard():
    if 'user_id' not in session:
        flash('Harap login terlebih dahulu.', 'warning')
        return redirect(url_for('login'))
    if session.get('role') == 'admin':
        return redirect(url_for('dashboard'))

    employee = Employee.query.filter_by(user_id=session.get('user_id')).first()
    if not employee:
        flash('Data karyawan tidak ditemukan. Silakan hubungi admin.', 'danger')
        return redirect(url_for('login'))
    
    # Ambil semua pinjaman karyawan ini
    loans = Loan.query.filter_by(employee_id=employee.id).order_by(Loan.application_date.desc()).all()
    
    # Pembayaran untuk pinjaman yang statusnya masih aktif (tidak "completed")
    active_payments = Payment.query.join(Loan).filter(
        Loan.employee_id == employee.id,
        Loan.status != 'completed'
    ).order_by(Payment.payment_date.desc()).all()
    
    # ====== PAGINATION UNTUK ARSIP ANGSURAN ======
    # Tangkap parameter "page" dari URL, default 1
    page = request.args.get('page', 1, type=int)
    per_page = 5  # jumlah baris per halaman, silakan sesuaikan

    # Query pembayaran untuk pinjaman yang statusnya "completed"
    archived_payments_query = Payment.query.join(Loan).filter(
        Loan.employee_id == employee.id,
        Loan.status == 'completed'
    ).order_by(Payment.payment_date.desc())

    # Gunakan paginate() bawaan Flask-SQLAlchemy
    archived_payments_paginate = archived_payments_query.paginate(page=page, per_page=per_page, error_out=False)
    # .items mengambil list data di halaman tersebut
    archived_payments = archived_payments_paginate.items
    
    # Kirim ke template
    return render_template(
        'employee_dashboard.html',
        employee=employee,
        loans=loans,
        active_payments=active_payments,  # Bisa langsung kirim data list
        archived_payments=archived_payments,  # Data di halaman sekarang
        archived_pagination=archived_payments_paginate  # Objek untuk bikin link Next/Prev
    )


@app.route('/employee_profile')
def employee_profile():
    if 'user_id' not in session:
        flash("Harap login terlebih dahulu.", "danger")
        return redirect(url_for("login"))
    # Cari record Employee berdasarkan user_id
    employee = Employee.query.filter_by(user_id=session.get("user_id")).first()
    if not employee:
        flash("Data karyawan tidak ditemukan. Silakan hubungi admin.", "danger")
        return redirect(url_for("employee_dashboard"))
    return render_template("employee_profile.html", employee=employee)

@app.route('/update_profile', methods=['GET', 'POST'])
def update_profile():
    if 'user_id' not in session:
        flash("Harap login terlebih dahulu.", "danger")
        return redirect(url_for("login"))
    
    employee = Employee.query.filter_by(user_id=session.get("user_id")).first()
    if not employee:
        flash("Data karyawan tidak ditemukan.", "danger")
        return redirect(url_for("employee_dashboard"))
    
    if request.method == 'POST':
        # Update data profil
        employee.name = request.form.get('name')
        employee.position = request.form.get('position')
        employee.address = request.form.get('address')
        employee.phone = request.form.get('phone')
        hire_date_str = request.form.get('hire_date')
        if hire_date_str:
            try:
                employee.hire_date = datetime.strptime(hire_date_str, "%Y-%m-%d").date()
            except ValueError:
                employee.hire_date = None

        # Proses file upload foto
        if 'photo' in request.files:
            file = request.files['photo']
            if file and file.filename:
                filename = secure_filename(file.filename)
                upload_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                file.save(upload_path)
                employee.photo = filename  # simpan nama file ke database

        db.session.commit()
        flash("Profil berhasil diperbarui.", "success")
        return redirect(url_for("employee_profile"))
    
    return render_template("update_profile.html", employee=employee)


if __name__ == "__main__":
    # Pastikan semua tabel dibuat (hanya berjalan saat app dijalankan langsung)
    with app.app_context():
        db.create_all()

    app.run(
        debug=True,
        host="127.0.0.1",  # hanya bisa diakses dari laptop sendiri
        port=5001          # pakai port aman, kecil kemungkinan konflik
    )
