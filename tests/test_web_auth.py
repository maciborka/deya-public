import base64

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import config, main


def _app():
    app = FastAPI()
    app.middleware("http")(main.basic_auth)

    @app.get("/api/health")
    def health():
        return {"ok": True}

    @app.get("/api/latest")
    def latest():
        return {"ok": True}

    return app


def _hdr(user, password):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


def test_no_password_configured_open(monkeypatch):
    monkeypatch.setattr(config, "WEB_PASSWORD", "")
    assert TestClient(_app()).get("/api/latest").status_code == 200


def test_password_required(monkeypatch):
    monkeypatch.setattr(config, "WEB_USER", "deya")
    monkeypatch.setattr(config, "WEB_PASSWORD", "s3cret")
    c = TestClient(_app())
    r = c.get("/api/latest")
    assert r.status_code == 401 and "Basic" in r.headers["www-authenticate"]
    assert c.get("/api/latest", headers=_hdr("deya", "wrong")).status_code == 401
    assert c.get("/api/latest", headers={"Authorization": "Basic !!!"}).status_code == 401
    assert c.get("/api/latest", headers=_hdr("deya", "s3cret")).status_code == 200
    # TestClient приходит с host "testclient", не 127.0.0.1 — health тоже закрыт
    assert c.get("/api/health").status_code == 401
