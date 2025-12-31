# seed_admin.py
from app import app, db, User, generate_password_hash

def seed_admin():
    admin_email = "cs@goldenfarm99.com"
    existing_admin = User.query.filter_by(email=admin_email).first()
    if not existing_admin:
        admin = User(
            fullname="Administrator",
            email=admin_email,
            password=generate_password_hash("123", method='pbkdf2:sha256'),
            role="admin"
        )
        db.session.add(admin)
        db.session.commit()
        print("Admin user telah dibuat.")
    else:
        print("Admin user sudah ada.")

if __name__ == '__main__':
    with app.app_context():
        seed_admin()
