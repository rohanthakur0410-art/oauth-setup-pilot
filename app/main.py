"""OAuth Setup Pilot - FastAPI app.

Routes:
  /                  library of pre-built, source-validated guides
  /guides/{slug}     a single guide page
  /api/generate      POST {query} -> run the investigation agent live
  /generated/{slug}  view a generated guide
  /healthz           health check
"""
import json
import os
import re
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from .agent import investigate

BASE = Path(__file__).resolve().parent.parent
GUIDES_DIR = Path(os.environ.get("GUIDES_DIR", BASE / "guides"))
GENERATED_DIR = Path(os.environ.get("GENERATED_DIR", BASE / "generated"))
GENERATED_DIR.mkdir(exist_ok=True)

app = FastAPI(title="OAuth Setup Pilot")
app.mount("/static", StaticFiles(directory=BASE / "app" / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "app" / "templates")


def load_library() -> list[dict]:
    guides = []
    for f in sorted(GUIDES_DIR.glob("*.json")):
        guides.append(json.loads(f.read_text()))
    return guides


def load_guide(slug: str) -> dict | None:
    for d in (GUIDES_DIR, GENERATED_DIR):
        p = d / f"{slug}.json"
        if p.exists():
            return json.loads(p.read_text())
    return None


@app.get("/healthz")
def healthz():
    return {"ok": True, "llm_configured": bool(
        os.environ.get("GEMINI_API_KEY") or os.environ.get("OPENAI_API_KEY"))}


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    guides = load_library()
    return templates.TemplateResponse(request=request, name="index.html", context={"guides": guides})


@app.get("/guides/{slug}", response_class=HTMLResponse)
def guide_page(request: Request, slug: str):
    g = load_guide(slug)
    if not g:
        raise HTTPException(404, "guide not found")
    return templates.TemplateResponse(request=request, name="guide.html", context={"g": g})


class GenRequest(BaseModel):
    query: str


@app.post("/api/generate")
def generate(req: GenRequest):
    q = req.query.strip()
    if not q:
        raise HTTPException(400, "query required")
    if not re.match(r"^[\w .:/-]{2,120}$", q):
        raise HTTPException(400, "query has unsupported characters")
    try:
        guide = investigate(q)
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=503)
    except Exception as e:
        return JSONResponse({"error": f"investigation failed: {e}"}, status_code=500)
    (GENERATED_DIR / f"{guide['slug']}.json").write_text(json.dumps(guide, indent=2))
    return {"slug": guide["slug"], "url": f"/guides/{guide['slug']}", "guide": guide}
