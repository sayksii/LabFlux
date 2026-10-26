def test_dashboard_page_is_served_without_token_and_api_still_requires_one(client):
    page = client.get("/dashboard")
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert "<title>LabFlux</title>" in page.text
    assert "/api/nodes" in page.text and "/api/jobs" in page.text
    assert client.get("/api/nodes").status_code == 401
