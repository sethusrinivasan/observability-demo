from app import app, get_home_html


def test_get_home_html_contains_compute_links():
    html = get_home_html()
    assert "Hello from Observability Lab!" in html
    assert "/compute/5" in html
    assert "/compute/10" in html
    assert "/compute/20" in html
    assert "id='compute-value'" in html


def test_home_route_returns_html():
    with app.test_client() as client:
        resp = client.get("/")

    assert resp.status_code == 200
    assert resp.headers["Content-Type"].startswith("text/html")
    assert "Compute 5" in resp.get_data(as_text=True)
