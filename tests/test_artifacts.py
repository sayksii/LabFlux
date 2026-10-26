import hashlib

from conftest import register, submit, user_headers, worker_headers


def test_source_upload_is_owned_and_content_deduplicated(client):
    content = b"source snapshot contents"
    first = client.post("/api/artifacts/sources", headers=user_headers(), content=content)
    assert first.status_code == 201
    value = first.json()
    assert value["sha256"] == hashlib.sha256(content).hexdigest()
    assert value["size"] == len(content)
    assert "relative_path" not in value
    assert (
        client.post("/api/artifacts/sources", headers=user_headers(), content=content).json()["id"]
        == value["id"]
    )
    assert client.get(f"/api/artifacts/{value['id']}", headers=user_headers()).content == content
    assert (
        client.get(f"/api/artifacts/{value['id']}", headers=user_headers("bob")).status_code == 404
    )
    response = client.post(
        "/api/jobs",
        headers=user_headers("bob"),
        json={"image": "python:3.12-slim", "argv": ["true"], "source_id": value["id"]},
    )
    assert response.status_code == 404


def test_upload_limit_removes_partial_file(client, settings):
    settings.max_source_bytes = 10
    assert (
        client.post("/api/artifacts/sources", headers=user_headers(), content=b"x" * 11).status_code
        == 413
    )
    assert not list((settings.data_dir / "artifacts").glob("incoming-*"))


def test_worker_source_only_available_to_assigned_worker(client):
    first = register(client, "vm-a", cpu=1)
    other = register(client, "vm-b", cpu=4)
    artifact = client.post(
        "/api/artifacts/sources", headers=user_headers(), content=b"snapshot"
    ).json()
    job = submit(client, source_id=artifact["id"])
    attempt_id = job["attempts"][0]["id"]
    path = f"/internal/workers/vm-a/attempts/{attempt_id}/source"
    assert (
        client.get(path, headers=worker_headers(), params={"instance_id": first}).content
        == b"snapshot"
    )
    assert (
        client.get(
            path.replace("vm-a", "vm-b"), headers=worker_headers(), params={"instance_id": other}
        ).status_code
        == 404
    )


def test_output_upload_is_idempotent_and_downloadable(client):
    instance = register(client)
    job = submit(client)
    attempt_id = job["attempts"][0]["id"]
    path = f"/internal/workers/vm-a/attempts/{attempt_id}/output"
    response = client.put(
        path, headers=worker_headers(), params={"instance_id": instance}, content=b"output"
    )
    assert response.status_code == 200
    artifact = response.json()
    assert (
        client.put(
            path, headers=worker_headers(), params={"instance_id": instance}, content=b"output"
        ).json()
        == artifact
    )
    assert (
        client.put(
            path, headers=worker_headers(), params={"instance_id": instance}, content=b"different"
        ).status_code
        == 409
    )
    value = client.get(f"/api/jobs/{job['id']}", headers=user_headers()).json()
    assert value["outputs"][0]["id"] == artifact["id"]
    assert (
        client.get(f"/api/artifacts/{artifact['id']}", headers=user_headers()).content == b"output"
    )
