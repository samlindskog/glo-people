from app import auth


async def test_login_me_logout(client_factory, writer):
    r = await writer.get("/api/auth/me")
    assert r.status_code == 200
    me = r.json()
    assert me["username"] == "wendy" and me["role"] == "writer"
    assert "people:write" in me["permissions"]

    assert (await writer.post("/api/auth/logout")).status_code == 204
    assert (await writer.get("/api/auth/me")).status_code == 401


async def test_bad_password_and_throttle(client_factory, monkeypatch):
    c = await client_factory()
    from tests.conftest import make_user
    await make_user("bob", "reader")
    r = await c.post("/api/auth/login", json={"username": "bob", "password": "nope"})
    assert r.status_code == 401
    for _ in range(12):
        await c.post("/api/auth/login", json={"username": "bob", "password": "nope"})
    r = await c.post("/api/auth/login", json={"username": "bob", "password": "nope"})
    assert r.status_code == 429


async def test_unauthenticated(client_factory):
    c = await client_factory()
    for path in ("/api/fields", "/api/searches", "/api/activity", "/api/users", "/api/api-keys"):
        assert (await c.get(path)).status_code == 401, path


async def test_reader_cannot_write(reader):
    r = await reader.post("/api/people", json={"tier": "member", "fields": {"name": "X"}})
    assert r.status_code == 403
    assert (await reader.get("/api/fields")).status_code == 200
    assert (await reader.get("/api/activity")).status_code == 200
    assert (await reader.get("/api/searches?all=1")).status_code == 200


async def test_reader_cannot_grant_writer(reader):
    r = await reader.post("/api/users", json={"username": "eve", "password": "x" * 12,
                                               "role": "writer"})
    assert r.status_code == 403
    r = await reader.post("/api/users", json={"username": "eve", "password": "x" * 12,
                                               "role": "reader"})
    assert r.status_code == 201


async def test_cannot_change_self(writer):
    me = (await writer.get("/api/auth/me")).json()
    r = await writer.patch(f"/api/users/{me['id']}", json={"is_active": False})
    assert r.status_code == 403


async def test_deactivate_kills_sessions(writer, client_factory):
    other = await client_factory("olga", "reader")
    users = (await writer.get("/api/users")).json()
    olga = next(u for u in users if u["username"] == "olga")
    r = await writer.patch(f"/api/users/{olga['id']}", json={"is_active": False})
    assert r.status_code == 200 and r.json()["is_active"] is False
    assert (await other.get("/api/auth/me")).status_code == 401


async def test_api_keys_lifecycle(writer, client_factory):
    r = await writer.post("/api/api-keys", json={"name": "laptop"})
    assert r.status_code == 201
    key = r.json()["key"]
    assert key.startswith("glo_")

    bare = await client_factory()
    h = {"Authorization": f"Bearer {key}"}
    me = (await bare.get("/api/auth/me", headers=h)).json()
    assert me["via_api_key"] and "accounts:manage" not in me["permissions"]
    assert (await bare.get("/api/fields", headers=h)).status_code == 200
    # keys cannot administer accounts or mint keys
    assert (await bare.get("/api/users", headers=h)).status_code == 403
    assert (await bare.post("/api/api-keys", json={"name": "x"}, headers=h)).status_code == 403

    keys = (await writer.get("/api/api-keys")).json()
    assert keys[0]["last_used_at"] is not None and "key_hash" not in keys[0]
    assert (await writer.delete(f"/api/api-keys/{keys[0]['id']}")).status_code == 204
    assert (await bare.get("/api/fields", headers=h)).status_code == 401
    assert (await bare.get("/api/fields", headers={"Authorization": "Bearer glo_bad_x"})
            ).status_code == 401


async def test_manage_other_users_keys(writer, client_factory):
    await client_factory("rex", "reader")
    rex = next(u for u in (await writer.get("/api/users")).json() if u["username"] == "rex")
    r = await writer.post(f"/api/users/{rex['id']}/api-keys", json={"name": "for rex"})
    assert r.status_code == 201
    keys = (await writer.get(f"/api/users/{rex['id']}/api-keys")).json()
    assert len(keys) == 1
    r = await writer.delete(f"/api/users/{rex['id']}/api-keys/{keys[0]['id']}")
    assert r.status_code == 204


async def test_activity_logged(writer):
    items = (await writer.get("/api/activity")).json()["items"]
    assert any(i["action"] == "auth.login" and i["username"] == "wendy" for i in items)


def test_key_format():
    key, prefix, h = auth.new_api_key()
    assert key.startswith(f"glo_{prefix}_") and h == auth.sha256(key)
