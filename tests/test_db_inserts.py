from datetime import datetime, timezone


def test_insert_basic_models(app_instance):
    from app import db, Employee, Payroll, User

    with app_instance.app_context():
        user = User(
            fullname="Test Admin",
            email="admin_test@example.com",
            password="hashed",
            role="admin",
        )
        db.session.add(user)
        db.session.flush()

        employee = Employee(
            user_id=user.id,
            nik="EMP-TEST-001",
            name="Test Employee",
            position="QA",
            address="Jakarta",
            phone="08123456789",
            hire_date=datetime(2024, 1, 1, tzinfo=timezone.utc).date(),
        )
        db.session.add(employee)
        db.session.flush()

        payroll = Payroll(
            employee_id=employee.id,
            pay_period="2025-01",
            gaji_pokok=5_000_000,
            bpjs_ketenagakerjaan=0,
            tunjangan_makan=0,
            tunjangan_transport=0,
            tunjangan_lainnya=0,
            potongan_gaji=0,
            alpha=0,
            hutang=0,
            upah_lembur=0,
            thr=0,
            status="draft",
        )
        db.session.add(payroll)
        db.session.commit()

        stored = Payroll.query.one()
        assert stored.employee_id == employee.id
        assert stored.take_home_pay == 5_000_000


def test_loan_remaining_and_audit_log(app_instance):
    from app import db, AuditLog, Employee, Loan, Payment, User, log_action

    with app_instance.app_context():
        user = User(
            fullname="Test User",
            email="user_test@example.com",
            password="hashed",
            role="user",
        )
        db.session.add(user)
        db.session.flush()

        employee = Employee(
            user_id=user.id,
            nik="EMP-TEST-002",
            name="Loan Tester",
            position="Finance",
        )
        db.session.add(employee)
        db.session.flush()

        loan = Loan(
            employee_id=employee.id,
            amount=1_000_000,
            tenor=10,
            interest_rate=10,
            installment=110_000,
            status="approved",
        )
        db.session.add(loan)
        db.session.flush()

        payment = Payment(
            loan_id=loan.id,
            payment_amount=100_000,
            status="approved",
        )
        db.session.add(payment)
        db.session.commit()

        reloaded = Loan.query.get(loan.id)
        assert reloaded.remaining == 1_000_000

    with app_instance.test_request_context():
        log_action("test_action", "loan", loan.id, "test details")

    with app_instance.app_context():
        assert AuditLog.query.count() == 1
