from conftest import register, user_headers


def test_dashboard_page_is_served_without_token_and_api_still_requires_one(client):
    page = client.get("/dashboard")
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert "<title>LabFlux</title>" in page.text
    assert "/api/nodes" in page.text and "/api/jobs" in page.text
    assert client.get("/api/nodes").status_code == 401


def test_gpu_axis_is_hidden_until_cards_are_reported(client):
    page = client.get("/dashboard").text
    assert '<button data-axis="gpu" aria-pressed="false" hidden>GPU 卡</button>' in page
    assert '<span id="lgExternal" hidden>' in page
    # The page reads exactly these fields; a CPU-only node still reports an empty card list.
    for field in ("capacity.gpus", "observed.gpus", "allocated.gpu_uuids", "gpu_uuids"):
        assert field.split(".")[-1] in page
    register(client, "cpu-only")
    node = client.get("/api/nodes", headers=user_headers()).json()[0]
    assert node["capacity"]["gpus"] == [] and node["allocated"]["gpu_uuids"] == []


def test_dashboard_reads_scheduler_explanations_instead_of_recomputing_them(client):
    page = client.get("/dashboard").text
    # M12: queue order, blockers, reservations and placements come from the Scheduler.
    for path in ("/api/queue", "/api/jobs?scope=all", "/api/whoami", "/api/models"):
        assert path in page
    assert "allocations" in page and 'data-scope="all"' in page
    assert "function queuePositions" not in page and "function blocker(" not in page


def test_dashboard_uses_one_24_hour_clock_and_names_model_reclaims(client):
    page = client.get("/dashboard").text
    # Every clock is 24-hour zh-TW whatever the browser's locale (the projector truncated "上午").
    assert 'hourCycle: "h23"' in page and "toLocaleTimeString()" not in page
    # An idle on-demand model's release, as a feed line and as a stop reason, reads in Chinese.
    assert 'case "model_stopping"' in page and "提前回收" in page and "逾時自動釋放" in page
