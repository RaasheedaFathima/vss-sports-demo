#!/usr/bin/env python3
"""VSS2 FastAPI Backend — OCI GenAI + pgvector + fastembed

This service is stateless with respect to analysis — video files are written to
shared storage (OCI FSS), a DB row is created, and the analysis is picked up and
run by one or more worker pods.  The API handles upload, status polling, semantic
search, and video streaming only.
"""
import base64
import json
import os
import subprocess
import tempfile
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import oci
import psycopg2
import requests
import psycopg2.extras
from fastembed import TextEmbedding
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from oci.auth.signers import InstancePrincipalsSecurityTokenSigner
from oci.generative_ai_inference import GenerativeAiInferenceClient
from oci.generative_ai_inference.models import (
    ChatDetails, GenericChatRequest, ImageContent, ImageUrl,
    OnDemandServingMode, SystemMessage, TextContent, UserMessage,
)
from pydantic import BaseModel

# ── Environment ───────────────────────────────────────────────────────────────
PG_HOST             = os.environ["PG_HOST"]
PG_PORT             = int(os.environ.get("PG_PORT", 5432))
PG_DB               = os.environ["PG_DB"]
PG_USER             = os.environ["PG_USER"]
PG_PASSWORD         = os.environ["PG_PASSWORD"]
UPLOADS_DIR         = Path(os.environ.get("UPLOADS_DIR", "/mnt/fss/vss2/uploads"))
FASTEMBED_CACHE_DIR = os.environ.get("FASTEMBED_CACHE_DIR", "/mnt/fss/vss2/fastembed-cache")
COMPARTMENT_ID      = os.environ["COMPARTMENT_ID"]

GEMINI_25_PRO_OCID   = os.environ["GEMINI_25_PRO_OCID"]
GEMINI_25_FLASH_OCID = os.environ["GEMINI_25_FLASH_OCID"]
COHERE_CMD_A_VISION  = os.environ["COHERE_CMD_A_VISION"]
COHERE_CMD_A_TEXT    = os.environ["COHERE_CMD_A_TEXT"]

NVIDIA_API_KEY        = os.environ.get("NVIDIA_API_KEY", "")
COSMOS_REASON2_MODEL  = os.environ.get("COSMOS_REASON2_MODEL", "nvidia/cosmos-reason2-8b")
NVIDIA_NIM_ENDPOINT   = os.environ.get(
    "NVIDIA_NIM_ENDPOINT",
    "https://integrate.api.nvidia.com/v1/chat/completions",
)

LOCAL_COSMOS_ENDPOINT = os.environ.get("LOCAL_COSMOS_ENDPOINT", "")
LOCAL_COSMOS_API_KEY  = os.environ.get("LOCAL_COSMOS_API_KEY", "")
LOCAL_COSMOS_MODEL    = os.environ.get("LOCAL_COSMOS_MODEL", "nvidia/cosmos-reason2-8b")

OCI_INFERENCE_ENDPOINT = "https://inference.generativeai.us-phoenix-1.oci.oraclecloud.com"

# ── Model catalogue ───────────────────────────────────────────────────────────
VLM_MODELS = [
    {"id": GEMINI_25_PRO_OCID,    "label": "Google Gemini 2.5 Pro (OCI GenAI)"},
    {"id": GEMINI_25_FLASH_OCID,  "label": "Google Gemini 2.5 Flash (OCI GenAI)"},
    {"id": COHERE_CMD_A_VISION,   "label": "Cohere Command A Vision (OCI GenAI)"},
    {"id": COSMOS_REASON2_MODEL,  "label": "NVIDIA Cosmos-Reason2-8b (Public NIM)"},
    {"id": "local/cosmos-reason2","label": "NVIDIA Cosmos-Reason2-8b (Local NIM)"},
]
LLM_MODELS = [
    {"id": GEMINI_25_FLASH_OCID, "label": "Google Gemini 2.5 Flash (OCI GenAI)"},
    {"id": GEMINI_25_PRO_OCID,   "label": "Google Gemini 2.5 Pro (OCI GenAI)"},
    {"id": COHERE_CMD_A_TEXT,    "label": "Cohere Command A (OCI GenAI)"},
]
DEFAULT_VLM = GEMINI_25_PRO_OCID
DEFAULT_LLM = GEMINI_25_FLASH_OCID

# ── Analysis scenarios ────────────────────────────────────────────────────────
SCENARIO_PROMPTS: Dict[str, str] = {
    "general": (
        "Analyze this video segment. For every frame describe: all people present and their "
        "exact actions; objects and items of interest; environment and setting; notable events "
        "or changes. Be specific and factual about locations, timing, and counts."
    ),
    "surveillance": (
        "Analyze this surveillance footage. For every frame describe: all visible people, "
        "their behaviors and movements; any safety hazards or security concerns; suspicious "
        "activities; crowd dynamics and formations; abandoned objects or vehicles. "
        "Note exact locations and timing of any incidents."
    ),
    "traffic": (
        "Analyze this traffic camera footage. For every frame describe: vehicle movements "
        "and types; traffic flow and congestion; any violations (running lights, illegal turns, "
        "speeding); pedestrian activity at crosswalks; accidents or near-misses; road conditions."
    ),
    "sports": (
        "Analyze this sports footage. For every frame describe: key plays and actions occurring; "
        "EXACT count of players visible per team (count each individual, do not estimate); "
        "player positions, movements, and techniques; scoring opportunities; fouls or decisions; "
        "game momentum and critical moments."
    ),
    "retail": (
        "Analyze this retail store footage. For every frame describe: customer traffic patterns; "
        "shopping behaviors and product interactions; queue lengths at checkouts; staff presence "
        "and service interactions; any suspicious activities or potential theft indicators."
    ),
    "warehouse": (
        "Analyze this warehouse footage. For every frame describe: forklift and equipment "
        "movements and safety compliance; worker activities and tasks; PPE usage compliance; "
        "any hazards, spills, or unsafe conditions; loading/unloading dock activities."
    ),
}

# ── Embedding model singleton ─────────────────────────────────────────────────
_embed_model: Optional[TextEmbedding] = None
_embed_lock = threading.Lock()


def _get_embed_model() -> TextEmbedding:
    global _embed_model
    if _embed_model is None:
        with _embed_lock:
            if _embed_model is None:
                _embed_model = TextEmbedding(
                    "BAAI/bge-small-en-v1.5",
                    cache_dir=FASTEMBED_CACHE_DIR,
                )
    return _embed_model


def _embed(text: str) -> List[float]:
    model = _get_embed_model()
    vecs = list(model.embed([text]))
    return vecs[0].tolist()


# ── PostgreSQL helpers ────────────────────────────────────────────────────────
def _pg_conn():
    return psycopg2.connect(
        host=PG_HOST, port=PG_PORT, dbname=PG_DB,
        user=PG_USER, password=PG_PASSWORD,
        connect_timeout=10,
    )


def _ensure_schema() -> None:
    with _pg_conn() as conn, conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS vss2_videos (
                id TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                video_path TEXT NOT NULL,
                vlm_model TEXT NOT NULL,
                scenario TEXT NOT NULL DEFAULT 'general',
                custom_prompt TEXT DEFAULT '',
                camera_id TEXT DEFAULT '',
                location TEXT DEFAULT '',
                capture_type TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                batch_done INTEGER DEFAULT 0,
                total_batches INTEGER DEFAULT 0,
                frames INTEGER DEFAULT 0,
                duration FLOAT DEFAULT 0.0,
                error_msg TEXT DEFAULT '',
                upload_timestamp TIMESTAMPTZ DEFAULT NOW()
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS vss2_chunks (
                id SERIAL PRIMARY KEY,
                video_id TEXT NOT NULL REFERENCES vss2_videos(id) ON DELETE CASCADE,
                chunk_index INTEGER NOT NULL,
                chunk_text TEXT NOT NULL,
                segment_start FLOAT DEFAULT 0.0,
                segment_end FLOAT DEFAULT 0.0,
                embedding vector(384),
                UNIQUE(video_id, chunk_index)
            );
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS vss2_chunks_emb_idx
            ON vss2_chunks USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 64);
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS vss2_chunks_vid_idx ON vss2_chunks(video_id);")
        cur.execute("ALTER TABLE vss2_videos ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ;")
        cur.execute("ALTER TABLE vss2_videos ADD COLUMN IF NOT EXISTS worker_id TEXT DEFAULT '';")
        conn.commit()


def _db_insert_video(video_id: str, filename: str, path: str, vlm_model: str,
                     scenario: str, custom_prompt: str, camera_id: str,
                     location: str, capture_type: str) -> None:
    with _pg_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO vss2_videos
               (id, filename, video_path, vlm_model, scenario, custom_prompt,
                camera_id, location, capture_type, status)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending')
               ON CONFLICT (id) DO UPDATE SET
                 vlm_model=EXCLUDED.vlm_model, scenario=EXCLUDED.scenario,
                 custom_prompt=EXCLUDED.custom_prompt, status='pending',
                 batch_done=0, total_batches=0, frames=0, error_msg=''""",
            (video_id, filename, path, vlm_model, scenario, custom_prompt,
             camera_id, location, capture_type),
        )
        conn.commit()


def _db_update_video(video_id: str, **kwargs) -> None:
    if not kwargs:
        return
    cols = ", ".join(f"{k}=%s" for k in kwargs)
    vals = list(kwargs.values()) + [video_id]
    with _pg_conn() as conn, conn.cursor() as cur:
        cur.execute(f"UPDATE vss2_videos SET {cols} WHERE id=%s", vals)
        conn.commit()


def _db_get_video(video_id: str) -> Optional[Dict]:
    with _pg_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM vss2_videos WHERE id=%s", (video_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def _db_list_videos() -> List[Dict]:
    with _pg_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM vss2_videos ORDER BY upload_timestamp DESC")
        return [dict(r) for r in cur.fetchall()]


def _db_delete_video(video_id: str) -> Optional[str]:
    with _pg_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT video_path FROM vss2_videos WHERE id=%s", (video_id,))
        row = cur.fetchone()
        if not row:
            return None
        path = row[0]
        cur.execute("DELETE FROM vss2_videos WHERE id=%s", (video_id,))
        conn.commit()
        return path


def _db_store_chunks(video_id: str, chunks: List[Dict]) -> None:
    with _pg_conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM vss2_chunks WHERE video_id=%s", (video_id,))
        for i, c in enumerate(chunks):
            vec = _embed(c["text"])
            cur.execute(
                """INSERT INTO vss2_chunks (video_id, chunk_index, chunk_text,
                   segment_start, segment_end, embedding)
                   VALUES (%s,%s,%s,%s,%s,%s)""",
                (video_id, i, c["text"], c["start"], c["end"],
                 json.dumps(vec)),
            )
        conn.commit()


def _db_search(query_vec: List[float], top_k: int, camera_id: str = "",
               location: str = "", scenario: str = "",
               min_similarity: float = 0.0) -> List[Dict]:
    vec_str = json.dumps(query_vec)
    with _pg_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """SELECT c.video_id, c.chunk_index, c.chunk_text,
                      c.segment_start, c.segment_end,
                      1 - (c.embedding <=> %s::vector) AS similarity,
                      v.filename, v.scenario, v.camera_id, v.location,
                      v.capture_type, v.vlm_model, v.duration
               FROM vss2_chunks c
               JOIN vss2_videos v ON c.video_id = v.id
               WHERE v.status = 'ready'
                 AND (%s = '' OR v.camera_id ILIKE %s)
                 AND (%s = '' OR v.location ILIKE %s)
                 AND (%s = '' OR v.scenario = %s)
               ORDER BY c.embedding <=> %s::vector
               LIMIT %s""",
            (vec_str,
             camera_id, f"%{camera_id}%",
             location, f"%{location}%",
             scenario, scenario,
             vec_str, top_k),
        )
        rows = [dict(r) for r in cur.fetchall()]
    return [r for r in rows if float(r["similarity"]) >= min_similarity]


# ── OCI GenAI client ──────────────────────────────────────────────────────────
def _build_client() -> GenerativeAiInferenceClient:
    signer = InstancePrincipalsSecurityTokenSigner()
    return GenerativeAiInferenceClient(
        config={"region": "us-phoenix-1"},
        signer=signer,
        service_endpoint=OCI_INFERENCE_ENDPOINT,
        retry_strategy=oci.retry.NoneRetryStrategy(),
        timeout=(60, 300),
    )


def _extract_text(resp) -> str:
    try:
        choices = resp.data.chat_response.choices
        if choices:
            c = choices[0].message.content
            if isinstance(c, list):
                return "".join(getattr(x, "text", "") for x in c)
            return str(c)
    except Exception:
        pass
    return ""


# ── LLM synthesis ─────────────────────────────────────────────────────────────
def _synthesize(query: str, results: List[Dict], llm_model: str) -> str:
    parts = []
    for i, r in enumerate(results[:8]):
        ts = f"{r['segment_start']:.0f}s–{r['segment_end']:.0f}s"
        parts.append(
            f"[{i+1}] {r['filename']} ({ts}, score={r['similarity']:.2f}):\n"
            f"{r['chunk_text'][:500]}"
        )
    context = "\n\n".join(parts)
    client = _build_client()
    sys_msg = SystemMessage()
    sys_msg.content = [TextContent(text=(
        "You are a video intelligence assistant. Based on the video analysis segments "
        "provided, answer the user's question accurately and specifically. "
        "When referencing specific moments, mention the filename and timestamp range. "
        "If the information is insufficient, say so clearly."
    ))]
    user_msg = UserMessage()
    user_msg.content = [TextContent(text=(
        f"Question: {query}\n\nVideo analysis segments:\n{context}"
    ))]
    req = GenericChatRequest()
    req.api_format = "GENERIC"
    req.messages = [sys_msg, user_msg]
    req.max_tokens = 2000
    req.temperature = 0.2
    details = ChatDetails()
    details.compartment_id = COMPARTMENT_ID
    details.serving_mode = OnDemandServingMode(model_id=llm_model)
    details.chat_request = req
    return _extract_text(client.chat(details))


# ── FastAPI app ───────────────────────────────────────────────────────────────
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
STATIC_DIR = Path(os.environ.get("STATIC_DIR", "/app/static"))

app = FastAPI(title="VSS2 — Video Search & Summarization", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup():
    try:
        _ensure_schema()
    except Exception as e:
        print(f"[WARN] Schema init failed: {e}")


@app.get("/health")
def health():
    return {"status": "healthy"}


@app.get("/api/models")
def models():
    return {"vlmModels": VLM_MODELS, "llmModels": LLM_MODELS}


@app.get("/api/scenarios")
def scenarios():
    return {"scenarios": [
        {"id": k, "label": k.replace("_", " ").title()}
        for k in SCENARIO_PROMPTS
    ]}


@app.post("/api/upload")
async def upload(
    file: UploadFile = File(...),
    vlm_model: str = Form(DEFAULT_VLM),
    scenario: str = Form("general"),
    custom_prompt: str = Form(""),
    camera_id: str = Form(""),
    location: str = Form(""),
    capture_type: str = Form(""),
) -> JSONResponse:
    suffix = Path(file.filename or "video.mp4").suffix or ".mp4"
    video_id = uuid.uuid4().hex
    dest = UPLOADS_DIR / f"{video_id}{suffix}"
    dest.write_bytes(await file.read())

    _db_insert_video(
        video_id, file.filename or dest.name, str(dest),
        vlm_model, scenario, custom_prompt[:800],
        camera_id, location, capture_type,
    )
    # A worker pod will claim and process this job automatically
    return JSONResponse({"videoId": video_id, "status": "pending"})


@app.get("/api/videos/{video_id}/status")
def analysis_status(video_id: str) -> JSONResponse:
    row = _db_get_video(video_id)
    if not row:
        return JSONResponse({"status": "not_found"})
    resp: Dict[str, Any] = {
        "status":        row["status"],
        "frames":        row["frames"],
        "duration":      row["duration"],
        "batch_done":    row["batch_done"],
        "total_batches": row["total_batches"],
        "error":         row.get("error_msg", ""),
        "filename":      row["filename"],
        "vlm_model":     row["vlm_model"],
        "scenario":      row["scenario"],
        "worker_id":     row.get("worker_id", ""),
    }
    if row["status"] == "analysing" and row.get("started_at") and row["batch_done"] > 0:
        started = row["started_at"]
        if not started.tzinfo:
            started = started.replace(tzinfo=timezone.utc)
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        avg = elapsed / row["batch_done"]
        remaining = row["total_batches"] - row["batch_done"]
        resp["elapsed"] = round(elapsed, 1)
        resp["eta"] = round(avg * remaining, 1) if remaining > 0 else 0.0
    return JSONResponse(resp)


@app.post("/api/videos/{video_id}/reanalyse")
def reanalyse(
    video_id: str,
    vlm_model: str = Form(DEFAULT_VLM),
    scenario: str = Form("general"),
    custom_prompt: str = Form(""),
) -> JSONResponse:
    row = _db_get_video(video_id)
    if not row:
        raise HTTPException(404, "Video not found")
    _db_update_video(
        video_id,
        vlm_model=vlm_model, scenario=scenario,
        custom_prompt=custom_prompt[:800], status="pending",
        batch_done=0, total_batches=0, error_msg="",
        started_at=None, worker_id="",
    )
    return JSONResponse({"videoId": video_id, "status": "pending"})


@app.get("/api/videos")
def list_videos() -> JSONResponse:
    try:
        rows = _db_list_videos()
        for r in rows:
            r["upload_timestamp"] = str(r.get("upload_timestamp", ""))
        return JSONResponse({"videos": rows})
    except Exception as e:
        return JSONResponse({"videos": [], "error": str(e)})


@app.delete("/api/videos/{video_id}")
def delete_video(video_id: str) -> JSONResponse:
    path = _db_delete_video(video_id)
    if path is None:
        raise HTTPException(404, "Video not found")
    try:
        Path(path).unlink(missing_ok=True)
    except Exception:
        pass
    return JSONResponse({"deleted": video_id})


@app.get("/api/videos/{video_id}/file")
def stream_video(video_id: str):
    row = _db_get_video(video_id)
    if not row:
        raise HTTPException(404, "Video not found")
    path = Path(row["video_path"])
    if not path.exists():
        raise HTTPException(404, "Video file not found on disk")
    return FileResponse(
        str(path),
        media_type="video/mp4",
        headers={"Accept-Ranges": "bytes"},
    )


class SearchRequest(BaseModel):
    query: str
    top_k: int = 10
    llm_model: str = DEFAULT_LLM
    use_llm: bool = True
    min_similarity: float = 0.0
    camera_id: str = ""
    location: str = ""
    scenario: str = ""


@app.post("/api/search")
def search(req: SearchRequest) -> JSONResponse:
    if not req.query.strip():
        raise HTTPException(400, "Query cannot be empty")
    t0 = time.time()
    q_vec = _embed(req.query)
    embed_ms = round((time.time() - t0) * 1000, 1)

    t1 = time.time()
    results = _db_search(
        q_vec, req.top_k,
        req.camera_id, req.location, req.scenario, req.min_similarity,
    )
    search_ms = round((time.time() - t1) * 1000, 1)

    synthesis = ""
    llm_ms = 0.0
    if req.use_llm and results:
        t2 = time.time()
        try:
            synthesis = _synthesize(req.query, results, req.llm_model)
        except Exception as e:
            synthesis = f"(LLM synthesis failed: {e})"
        llm_ms = round((time.time() - t2) * 1000, 1)

    for r in results:
        r["similarity"] = round(float(r["similarity"]), 4)
        r["segment_start"] = float(r["segment_start"])
        r["segment_end"] = float(r["segment_end"])
        r["duration"] = float(r.get("duration") or 0)

    return JSONResponse({
        "results": results,
        "synthesis": synthesis,
        "total": len(results),
        "embed_ms": embed_ms,
        "search_ms": search_ms,
        "llm_ms": llm_ms,
    })


@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC_DIR / "index.html").read_text()
