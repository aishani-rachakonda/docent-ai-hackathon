"""AI reconstructions: how a damaged object may have looked when it was complete.

Two steps, both through the Gemini API:
1. The Restorer (a Gemini text model) looks at The Met's photo and the object's record and says what is
   actually missing, or that the object survives whole. Nothing is generated for a whole object.
2. An image model (Gemini image) makes one picture from The Met's photo: everything that survives kept as
   it is, only the missing parts added, in the same material and style.

Every result is stamped "AI RECONSTRUCTION" on the image itself, so it stays labeled if it is saved or
shared, and the page labels it again beside The Met's photo. Results are kept on disk (reconstructions/),
so an object is only reconstructed once.
"""
import asyncio
import base64
import io
import json
import os
import re
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, ImageFont

import met_tools

API = "https://generativelanguage.googleapis.com/v1beta/models"
RESTORER_MODEL = os.environ.get("RESTORER_MODEL", "gemini-4-hydrogen")
# The most advanced image model first (Nano Banana 2.1); the next is used if it fails or is unavailable
IMAGE_MODELS = [m for m in [os.environ.get("IMAGE_MODEL"), "gemini-nano-banana-2.1", "gemini-3-pro-image"] if m]
OUT = Path(__file__).resolve().parent / "reconstructions"
MET_HEADERS = {"User-Agent": "docent-ai/0.1 (educational, non-commercial)"}
_locks: dict[int, asyncio.Lock] = {}
# A tour can start reconstructions for every stop at once: a few at a time keeps the image model
# within its rate limit, and a busy answer waits and tries again
_gate = asyncio.Semaphore(int(os.environ.get("RECONSTRUCT_CONCURRENCY", "3")))


async def _post(client: httpx.AsyncClient, url: str, body: dict) -> httpx.Response:
    for attempt in range(4):
        r = await client.post(url, json=body, headers=_key())
        if r.status_code not in (429, 500, 503) or attempt == 3:
            return r
        await asyncio.sleep(3 * 2 ** attempt)  # 3, 6, 12 s
    return r

RESTORER = """\
You are a museum conservator. Below is The Met's photograph of one object and its catalog record.
Say whether the object, as it survives, looks different from how it looked when it was made: broken-off
parts, missing limbs or head, a fragment of something larger, or lost paint and gilding (ancient marble
sculpture, for example, was usually painted). If so, say exactly what is missing or changed.

Record:
{record}

Answer with JSON only:
{{"incomplete": true or false,
  "survives": "one sentence on what survives and must be kept exactly as it is",
  "missing": ["each missing part, a few words each"],
  "restore": "two or three sentences describing only the missing or changed parts as they most likely
              were, in the same material and style, based on the record and on what the object is (e.g.
              the usual attributes of a god in this type of statue, and the restrained colors such works
              usually carried). Plain and factual."}}"""

PAINT = """\
This is The Met's photograph of {title} ({culture}, {date}; {medium}). {survives}

Make one photograph of how this same object may have looked when it was new and complete. Keep everything that
survives exactly as it is: the same face, pose, carving and folds. Add or restore only this:
{restore}
Keep any figure fully clothed as in the original. The same material, lighting, plinth and plain museum
backdrop as the photograph, the whole object in view. No text, no people, no frame."""


def _key() -> dict:
    return {"x-goog-api-key": os.environ.get("GEMINI_API_KEY", "")}


def _record(o: dict) -> dict:
    keep = ("title", "object_name", "culture", "period", "object_date", "medium", "classification",
            "department", "dimensions", "tags")
    return {k: o.get(k) for k in keep if o.get(k)}


async def _photo(o: dict, client: httpx.AsyncClient) -> bytes | None:
    for url in (o.get("image_small_url"), o.get("image_url")):
        if not url:
            continue
        r = await client.get(url, headers=MET_HEADERS)
        if r.status_code == 200 and r.headers.get("content-type", "").startswith("image/"):
            return r.content
    return None


async def _restorer(o: dict, photo: bytes, client: httpx.AsyncClient) -> dict:
    body = {"contents": [{"role": "user", "parts": [
                {"inlineData": {"mimeType": "image/jpeg", "data": base64.b64encode(photo).decode()}},
                {"text": RESTORER.format(record=json.dumps(_record(o), ensure_ascii=False, default=str))}]}],
            "generationConfig": {"responseMimeType": "application/json"}}
    r = await _post(client, f"{API}/{RESTORER_MODEL}:generateContent", body)
    r.raise_for_status()
    text = "".join(p.get("text", "") for p in r.json()["candidates"][0]["content"]["parts"])
    m = re.search(r"\{.*\}", text, re.S)
    return json.loads(m.group(0)) if m else {"incomplete": False}


async def _paint(prompt: str, photo: bytes, client: httpx.AsyncClient) -> tuple[bytes, str]:
    body = {"contents": [{"role": "user", "parts": [
                {"inlineData": {"mimeType": "image/jpeg", "data": base64.b64encode(photo).decode()}},
                {"text": prompt}]}],
            "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]}}
    last = ""
    for model in IMAGE_MODELS:
        r = await _post(client, f"{API}/{model}:generateContent", body)
        if r.status_code in (403, 404):  # this key has no access to that model: try the next
            last = f"{model}: {r.status_code}"
            continue
        r.raise_for_status()
        cand = (r.json().get("candidates") or [{}])[0]
        for part in (cand.get("content") or {}).get("parts", []):
            if "inlineData" in part:
                return base64.b64decode(part["inlineData"]["data"]), model
        last = f"{model}: no image ({cand.get('finishReason', 'unknown')})"  # e.g. declined: try the next model
    raise RuntimeError(f"No image model returned an image ({last})")


def _stamp(png: bytes) -> bytes:
    """A small, quiet tag in the lower-left corner, on the image itself, so it stays labeled if it is
    saved or shared: a translucent dark pill reading "AI reconstruction"."""
    img = Image.open(io.BytesIO(png)).convert("RGBA")
    w, h = img.size
    size = max(14, w // 42)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Georgia.ttf", size)
    except OSError:
        font = ImageFont.load_default()
    text = "AI reconstruction"
    box = ImageDraw.Draw(img).textbbox((0, 0), text, font=font)
    tw, th = box[2] - box[0], box[3] - box[1]
    pad_x, pad_y, margin = size, size // 2, size
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    x0, y1 = margin, h - margin
    x1, y0 = x0 + tw + 2 * pad_x, y1 - th - 2 * pad_y
    d.rounded_rectangle([x0, y0, x1, y1], radius=(y1 - y0) // 2, fill=(20, 18, 16, 150))
    d.text(((x0 + x1) / 2, (y0 + y1) / 2), text, font=font, fill=(246, 242, 234, 235), anchor="mm")
    out = Image.alpha_composite(img, layer).convert("RGB")
    buf = io.BytesIO()
    out.save(buf, "JPEG", quality=92)
    return buf.getvalue()


async def reconstruct(object_id: int) -> dict:
    """Reconstruct one object, or say why not. Returns {status, image?, added?, note?, model?}."""
    OUT.mkdir(exist_ok=True)
    meta = OUT / f"{object_id}.json"
    async with _locks.setdefault(object_id, asyncio.Lock()):
        if meta.exists():
            return json.loads(meta.read_text())
        async with _gate:
            return await _make(object_id, meta)


async def _make(object_id: int, meta: Path) -> dict:
    o = met_tools.get_object(object_id)
    if "error" in o:
        meta.write_text(json.dumps({"status": "error", "note": o["error"]}))
        return json.loads(meta.read_text())
    async with httpx.AsyncClient(timeout=180) as client:
        photo = await _photo(o, client)
        if not photo:
            result = {"status": "no_image", "note": "The Met shares no open-access photo of this object to work from."}
            meta.write_text(json.dumps(result))
            return result
        plan = await _restorer(o, photo, client)
        if not plan.get("incomplete") or not plan.get("restore"):
            result = {"status": "whole", "note": "This object survives essentially whole, so there is nothing to reconstruct."}
        else:
            prompt = PAINT.format(title=o.get("title") or o.get("object_name") or "an object",
                                  culture=o.get("culture") or o.get("department") or "",
                                  date=o.get("object_date") or "", medium=o.get("medium") or "",
                                  survives=plan.get("survives", ""), restore=plan["restore"])
            png, model = await _paint(prompt, photo, client)
            (OUT / f"{object_id}.jpg").write_bytes(_stamp(png))
            result = {"status": "ok", "image": f"/reconstruction/{object_id}.jpg",
                      "added": plan.get("missing", []), "model": model}
    meta.write_text(json.dumps(result))
    return result


# --- Without waiting: tours start reconstructions in the background and the page asks how they are ---
_running: dict[int, asyncio.Task] = {}
_failed: set[int] = set()  # failed this session (e.g. the API was busy): not retried on every check


def start(object_id: int) -> None:
    """Begin a reconstruction unless it is done or already being made."""
    if (OUT / f"{object_id}.json").exists() or object_id in _running or object_id in _failed:
        return
    task = asyncio.create_task(reconstruct(object_id))
    _running[object_id] = task

    def done(t: asyncio.Task) -> None:
        _running.pop(object_id, None)
        if t.cancelled() or t.exception():
            _failed.add(object_id)
    task.add_done_callback(done)


def status(object_id: int) -> dict:
    """The finished result, or {"status": "pending"} while it is being made (starting it if needed)."""
    meta = OUT / f"{object_id}.json"
    if meta.exists():
        return json.loads(meta.read_text())
    if object_id in _failed:
        return {"status": "error", "note": "Couldn't make a reconstruction this time."}
    start(object_id)
    return {"status": "pending"}
