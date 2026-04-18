import re
import urllib.parse
import uuid
from pathlib import Path
from environs import Env

env = Env()
env.read_env()

import qrcode
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse

app = FastAPI(title="Aniq AR · Model Manager")

BASE_DIR = Path(__file__).parent
MODELS_DIR = BASE_DIR / "models"
MODELS_DIR.mkdir(exist_ok=True)
QR_DIR = BASE_DIR / "qrcodes"
QR_DIR.mkdir(exist_ok=True)

PUBLIC_BASE_URL = env.str("PUBLIC_BASE_URL", default="")


def _base_url(request: Request) -> str:
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL.rstrip("/")
    return str(request.base_url).rstrip("/")


def _read_meta(product_id: str):
    meta = MODELS_DIR / f"{product_id}.txt"
    if not meta.exists():
        return None
    lines = meta.read_text().splitlines()
    name = lines[0] if lines else product_id
    filename = lines[1] if len(lines) > 1 else ""
    return {"id": product_id, "name": name, "filename": filename}


def _find_model_file(product_id: str):
    for p in MODELS_DIR.glob(f"{product_id}.*"):
        if p.suffix != ".txt":
            return p
    return None


def _qr_path(product_id: str) -> Path:
    return QR_DIR / f"{product_id}.png"


def _save_qr(product_id: str, ar_url: str) -> Path:
    path = _qr_path(product_id)
    img = qrcode.make(ar_url)
    img.save(path, format="PNG")
    return path


def _ar_url(base: str, product_id: str) -> str:
    return f"{base}/ar/{product_id}"


def _product_payload(info: dict, base: str) -> dict:
    pid = info["id"]
    return {
        "id": pid,
        "name": info["name"],
        "ar_url": _ar_url(base, pid),
        "view_url": f"{base}/view?id={pid}",
        "model_url": f"/api/products/{pid}/model",
        "qr_url": f"/api/products/{pid}/qr",
    }


def _detect_platform(user_agent: str) -> str:
    ua = (user_agent or "").lower()
    if re.search(r"iphone|ipad|ipod", ua):
        return "ios"
    if "android" in ua:
        return "android"
    return "other"


def _scene_viewer_intent(model_url: str, title: str, fallback_url: str) -> str:
    query = urllib.parse.urlencode({
        "file": model_url,
        "mode": "ar_only",
        "title": title,
    })
    params = ";".join([
        "scheme=https",
        "package=com.google.ar.core",
        "action=android.intent.action.VIEW",
        f"S.browser_fallback_url={urllib.parse.quote(fallback_url, safe='')}",
        "end",
    ])
    return f"intent://arvr.google.com/scene-viewer/1.0?{query}#Intent;{params};"


@app.get("/api/products")
async def list_products(request: Request):
    base = _base_url(request)
    items = []
    for meta in sorted(MODELS_DIR.glob("*.txt")):
        info = _read_meta(meta.stem)
        if not info:
            continue
        items.append(_product_payload(info, base))
    return items


@app.get("/api/products/{product_id}")
async def get_product(product_id: str, request: Request):
    info = _read_meta(product_id)
    if not info:
        raise HTTPException(status_code=404, detail="Product not found")
    return _product_payload(info, _base_url(request))


@app.post("/api/products")
async def create_product(
    request: Request,
    name: str = Form(...),
    model: UploadFile = File(...),
):
    product_id = str(uuid.uuid4())
    suffix = Path(model.filename).suffix.lower() or ".glb"
    if suffix not in (".glb", ".gltf"):
        raise HTTPException(status_code=400, detail="Only .glb or .gltf files are allowed")

    file_path = MODELS_DIR / f"{product_id}{suffix}"
    with open(file_path, "wb") as f:
        f.write(await model.read())

    (MODELS_DIR / f"{product_id}.txt").write_text(f"{name}\n{file_path.name}")

    base = _base_url(request)
    _save_qr(product_id, _ar_url(base, product_id))

    return _product_payload(
        {"id": product_id, "name": name, "filename": file_path.name},
        base,
    )


@app.put("/api/products/{product_id}")
async def update_product(
    product_id: str,
    request: Request,
    name: str = Form(None),
    model: UploadFile = File(None),
):
    info = _read_meta(product_id)
    if not info:
        raise HTTPException(status_code=404, detail="Product not found")

    new_name = name.strip() if name and name.strip() else info["name"]
    new_filename = info["filename"]

    if model is not None and model.filename:
        suffix = Path(model.filename).suffix.lower() or ".glb"
        if suffix not in (".glb", ".gltf"):
            raise HTTPException(status_code=400, detail="Only .glb or .gltf files are allowed")
        old = _find_model_file(product_id)
        if old:
            old.unlink()
        file_path = MODELS_DIR / f"{product_id}{suffix}"
        with open(file_path, "wb") as f:
            f.write(await model.read())
        new_filename = file_path.name

    (MODELS_DIR / f"{product_id}.txt").write_text(f"{new_name}\n{new_filename}")

    return _product_payload(
        {"id": product_id, "name": new_name, "filename": new_filename},
        _base_url(request),
    )


@app.delete("/api/products/{product_id}")
async def delete_product(product_id: str):
    meta = MODELS_DIR / f"{product_id}.txt"
    if not meta.exists():
        raise HTTPException(status_code=404, detail="Product not found")
    model_file = _find_model_file(product_id)
    if model_file:
        model_file.unlink()
    qr_file = _qr_path(product_id)
    if qr_file.exists():
        qr_file.unlink()
    meta.unlink()
    return {"ok": True}


@app.get("/api/products/{product_id}/model")
async def get_model(product_id: str):
    file_path = _find_model_file(product_id)
    if not file_path:
        raise HTTPException(status_code=404, detail="Model not found")
    media_type = "model/gltf-binary" if file_path.suffix.lower() == ".glb" else "model/gltf+json"
    return FileResponse(file_path, media_type=media_type, filename=file_path.name)


@app.get("/api/products/{product_id}/qr")
async def get_qr(product_id: str, request: Request):
    if not _read_meta(product_id):
        raise HTTPException(status_code=404, detail="Product not found")
    ar_url = _ar_url(_base_url(request), product_id)
    path = _qr_path(product_id)
    if not path.exists():
        _save_qr(product_id, ar_url)
    return FileResponse(
        path,
        media_type="image/png",
        headers={"X-Ar-Url": ar_url},
    )


@app.get("/ar/{product_id}")
async def ar_launch(product_id: str, request: Request):
    info = _read_meta(product_id)
    if not info:
        raise HTTPException(status_code=404, detail="Product not found")

    base = _base_url(request)
    model_url = f"{base}/api/products/{product_id}/model"
    fallback_url = f"{base}/view?id={product_id}&autoar=1"
    platform = _detect_platform(request.headers.get("user-agent", ""))

    if platform == "android":
        intent_url = _scene_viewer_intent(model_url, info["name"], fallback_url)
        return RedirectResponse(intent_url, status_code=302)

    return RedirectResponse(fallback_url, status_code=302)


@app.get("/")
async def landing_page():
    return FileResponse(BASE_DIR / "landing.html")


@app.get("/view")
async def viewer_page():
    return FileResponse(BASE_DIR / "index.html")


@app.get("/admin")
async def admin_page():
    return FileResponse(BASE_DIR / "admin.html")
