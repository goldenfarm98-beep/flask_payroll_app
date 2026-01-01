def test_home_page(client):
    resp = client.get("/")
    assert resp.status_code == 200


def test_login_page(client):
    resp = client.get("/login")
    assert resp.status_code == 200


def test_register_page(client):
    resp = client.get("/register")
    assert resp.status_code == 200


def test_dashboard_requires_login(client):
    resp = client.get("/dashboard")
    assert resp.status_code == 302
    assert "/login" in resp.location


def test_payrolls_requires_admin(client):
    resp = client.get("/payrolls")
    assert resp.status_code == 302
    assert "/login" in resp.location


def test_dashboard_admin_access(client):
    with client.session_transaction() as sess:
        sess["user_id"] = 1
        sess["role"] = "admin"
        sess["user_name"] = "Admin"

    resp = client.get("/dashboard")
    assert resp.status_code == 200


def test_payrolls_admin_access(client):
    with client.session_transaction() as sess:
        sess["user_id"] = 1
        sess["role"] = "admin"
        sess["user_name"] = "Admin"

    resp = client.get("/payrolls")
    assert resp.status_code == 200
