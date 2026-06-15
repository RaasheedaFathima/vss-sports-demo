#!/usr/bin/env python3
"""VSS2 FastAPI Backend — OCI GenAI + Oracle ADB + fastembed

Changes from Prachi's original:
  1. PostgreSQL → Oracle ADB (oracledb thin mode)
  2. pgvector → ADB CLOB for embeddings (upgrade to 23ai VECTOR later)
  3. Region updated to us-ashburn-1
  4. Added /api/videos/{video_id}/summary endpoint for structured JSON race output
  5. Removed COHERE_CMD_A_TEXT dependency on LLM synthesis (uses Gemini Flash)
"""
import json
import os
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import oci
import oracledb
import requests
from fastembed import TextEmbedding
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from oci.auth.signers import InstancePrincipalsSecurityTokenSigner
from oci.generative_ai_inference import GenerativeAiInferenceClient
from oci.generative_ai_inference.models import (
    ChatDetails, GenericChatRequest, OnDemandServingMode,
    SystemMessage, TextContent, UserMessage,
)
from pydantic import BaseModel

# ── Environment ───────────────────────────────────────────────────────────────
ADB_USER        = os.environ["ADB_USER"]
ADB_PASSWORD    = os.environ["ADB_PASSWORD"]
ADB_DSN         = os.environ["ADB_DSN"]

UPLOADS_DIR         = Path(os.environ.get("UPLOADS_DIR", "/home/opc/uploads"))
FASTEMBED_CACHE_DIR = os.environ.get("FASTEMBED_CACHE_DIR", "/home/opc/fastembed-cache")
COMPARTMENT_ID      = os.environ["COMPARTMENT_ID"]

GEMINI_25_PRO_OCID   = os.environ["GEMINI_25_PRO_OCID"]
GEMINI_25_FLASH_OCID = os.environ["GEMINI_25_FLASH_OCID"]
COHERE_CMD_A_VISION  = os.environ.get("COHERE_CMD_A_VISION", "cohere.command-a-vision")

NVIDIA_API_KEY        = os.environ.get("NVIDIA_API_KEY", "")
COSMOS_REASON2_MODEL  = os.environ.get("COSMOS_REASON2_MODEL", "nvidia/cosmos-reason2-8b")
NVIDIA_NIM_ENDPOINT   = os.environ.get(
    "NVIDIA_NIM_ENDPOINT",
    "https://integrate.api.nvidia.com/v1/chat/completions",
)
LOCAL_COSMOS_ENDPOINT = os.environ.get("LOCAL_COSMOS_ENDPOINT", "")
LOCAL_COSMOS_API_KEY  = os.environ.get("LOCAL_COSMOS_API_KEY", "")
LOCAL_COSMOS_MODEL    = os.environ.get("LOCAL_COSMOS_MODEL", "nvidia/cosmos-reason2-8b")

OCI_INFERENCE_ENDPOINT = "https://inference.generativeai.us-ashburn-1.oci.oraclecloud.com"
OBJECT_STORAGE_NAMESPACE = os.environ.get("OBJECT_STORAGE_NAMESPACE", "idxkccw2srke")
OBJECT_STORAGE_BUCKET = os.environ.get("OBJECT_STORAGE_BUCKET", "marathon-vlm-inputs")
OBJECT_STORAGE_REGION = os.environ.get("OBJECT_STORAGE_REGION", "us-ashburn-1")
OBJECT_STORAGE_PREFIX = os.environ.get("OBJECT_STORAGE_PREFIX", "uploads/")

# ── Model catalogue ───────────────────────────────────────────────────────────
VLM_MODELS = [
    {"id": GEMINI_25_PRO_OCID,   "label": "Google Gemini 2.5 Pro (OCI GenAI)"},
    {"id": GEMINI_25_FLASH_OCID, "label": "Google Gemini 2.5 Flash (OCI GenAI)"},
    {"id": COHERE_CMD_A_VISION,  "label": "Cohere Command A Vision (OCI GenAI)"},
    {"id": COSMOS_REASON2_MODEL, "label": "NVIDIA Cosmos-Reason2-8b (Public NIM)"},
    {"id": "local/cosmos-reason2","label": "NVIDIA Cosmos-Reason2-8b (Local NIM)"},
]
LLM_MODELS = [
    {"id": GEMINI_25_FLASH_OCID, "label": "Google Gemini 2.5 Flash (OCI GenAI)"},
    {"id": GEMINI_25_PRO_OCID,   "label": "Google Gemini 2.5 Pro (OCI GenAI)"},
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
        "Analyze this surveillance footage. Describe: all visible people, behaviors and movements; "
        "safety hazards or security concerns; suspicious activities; crowd dynamics; abandoned objects. "
        "Note exact locations and timing of any incidents."
    ),
    "traffic": (
        "Analyze this traffic camera footage. Describe: vehicle movements and types; traffic flow; "
        "any violations; pedestrian activity; accidents or near-misses; road conditions."
    ),
    "sports": (
        "You are an expert sports analyst specialising in track and field events. "
        "Analyse this COMPLETE race video holistically. "
        "Return ONLY a JSON object with this schema:\n\n"
        "{\n"
        "  \"event_type\": \"string\",\n"
        "  \"venue\": \"string or null\",\n"
        "  \"total_runners\": number,\n"
        "  \"race_duration_seconds\": number or null,\n"
        "  \"winner\": {\"name\": null, \"bib\": null, \"country\": null, \"finish_time\": null},\n"
        "  \"runners\": [{\"name\": null, \"bib\": \"string\", \"country\": null, "
        "\"position\": number, \"finish_time\": null, \"notable_events\": []}],\n"
        "  \"second_by_second\": [{\"second\": number, \"timestamp\": \"MM:SS\", "
        "\"description\": \"what is visible or spoken during that exact second\"}],\n"
        "  \"commentary\": [{\"timestamp\": \"HH:MM:SS\", \"event\": \"string\"}],\n"
        "  \"confidence\": \"high|medium|low\",\n"
        "  \"notes\": \"string\"\n"
        "}\n\n"
        "Populate second_by_second for every second from 0 through the visible race duration. "
        "Use null for values you cannot determine. No markdown, no explanation, JSON only."
    ),
    "retail": (
        "Analyze this retail store footage. Describe: customer traffic patterns; "
        "shopping behaviors; queue lengths; staff interactions; suspicious activities."
    ),
    "warehouse": (
        "Analyze this warehouse footage. Describe: equipment movements; safety compliance; "
        "worker activities; PPE usage; hazards or unsafe conditions."
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
    return list(_get_embed_model().embed([text]))[0].tolist()


# ── JSON normalization ───────────────────────────────────────────────────────
def _json_safe(value: Any) -> Any:
    """Convert Oracle driver objects into plain JSON-serializable values."""
    if hasattr(value, "read"):
        return value.read()
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    return value


# ── ADB helpers ───────────────────────────────────────────────────────────────
def _adb_conn():
    """Return an Oracle ADB connection (thin mode)."""
    return oracledb.connect(
        user=ADB_USER,
        password=ADB_PASSWORD,
        dsn=ADB_DSN,
        config_dir=os.environ.get("ADB_WALLET_DIR", "/home/opc/wallet"),
        wallet_location=os.environ.get("ADB_WALLET_DIR", "/home/opc/wallet"),
        wallet_password=ADB_PASSWORD,
    )


def _ensure_schema() -> None:
    """Create tables if they don't exist."""
    conn = _adb_conn()
    cur = conn.cursor()
    for ddl in [
        """BEGIN
            EXECUTE IMMEDIATE '
                CREATE TABLE VSS2_VIDEOS (
                    id               VARCHAR2(64)   NOT NULL PRIMARY KEY,
                    filename         VARCHAR2(500)  NOT NULL,
                    video_path       VARCHAR2(1000) NOT NULL,
                    vlm_model        VARCHAR2(500)  NOT NULL,
                    scenario         VARCHAR2(100)  DEFAULT ''general'',
                    custom_prompt    CLOB           DEFAULT '''',
                    camera_id        VARCHAR2(200)  DEFAULT '''',
                    location         VARCHAR2(200)  DEFAULT '''',
                    capture_type     VARCHAR2(100)  DEFAULT '''',
                    status           VARCHAR2(50)   DEFAULT ''pending'',
                    batch_done       NUMBER(10)     DEFAULT 0,
                    total_batches    NUMBER(10)     DEFAULT 0,
                    frames           NUMBER(10)     DEFAULT 0,
                    duration         NUMBER(10,3)   DEFAULT 0,
                    error_msg        CLOB           DEFAULT '''',
                    worker_id        VARCHAR2(200)  DEFAULT '''',
                    started_at       TIMESTAMP,
                    upload_timestamp TIMESTAMP      DEFAULT CURRENT_TIMESTAMP
                )
            ';
        EXCEPTION WHEN OTHERS THEN
            IF SQLCODE = -955 THEN NULL; ELSE RAISE; END IF;
        END;""",
        """BEGIN
            EXECUTE IMMEDIATE '
                CREATE TABLE VSS2_CHUNKS (
                    id            NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    video_id      VARCHAR2(64)  NOT NULL,
                    chunk_index   NUMBER(10)    NOT NULL,
                    chunk_text    CLOB          NOT NULL,
                    segment_start NUMBER(10,3)  DEFAULT 0,
                    segment_end   NUMBER(10,3)  DEFAULT 0,
                    embedding     CLOB,
                    CONSTRAINT uq_vss2_chunks UNIQUE (video_id, chunk_index),
                    CONSTRAINT fk_vss2_chunks_video FOREIGN KEY (video_id)
                        REFERENCES VSS2_VIDEOS(id) ON DELETE CASCADE
                )
            ';
        EXCEPTION WHEN OTHERS THEN
            IF SQLCODE = -955 THEN NULL; ELSE RAISE; END IF;
        END;""",
    ]:
        cur.execute(ddl)
    conn.commit()
    cur.close()
    conn.close()
    print("[APP] ADB schema OK.")


def _db_insert_video(video_id: str, filename: str, path: str, vlm_model: str,
                     scenario: str, custom_prompt: str, camera_id: str,
                     location: str, capture_type: str) -> None:
    conn = _adb_conn()
    with conn.cursor() as cur:
        cur.execute(
            """MERGE INTO VSS2_VIDEOS v
               USING DUAL ON (v.id = :id)
               WHEN MATCHED THEN UPDATE SET
                 vlm_model=:vlm_model, scenario=:scenario,
                 custom_prompt=:custom_prompt, status='pending',
                 batch_done=0, total_batches=0, frames=0, error_msg=''
               WHEN NOT MATCHED THEN INSERT
                 (id, filename, video_path, vlm_model, scenario, custom_prompt,
                  camera_id, location, capture_type, status)
               VALUES
                 (:id, :filename, :video_path, :vlm_model, :scenario, :custom_prompt,
                  :camera_id, :location, :capture_type, 'pending')""",
            dict(id=video_id, filename=filename, video_path=path,
                 vlm_model=vlm_model, scenario=scenario,
                 custom_prompt=custom_prompt, camera_id=camera_id,
                 location=location, capture_type=capture_type),
        )
    conn.commit()
    conn.close()


def _db_update_video(video_id: str, **kwargs) -> None:
    if not kwargs:
        return
    cols = ", ".join(f"{k}=:{k}" for k in kwargs)
    params = {**kwargs, "video_id": video_id}
    conn = _adb_conn()
    with conn.cursor() as cur:
        cur.execute(f"UPDATE VSS2_VIDEOS SET {cols} WHERE id=:video_id", params)
    conn.commit()
    conn.close()


def _db_get_video(video_id: str) -> Optional[Dict]:
    conn = _adb_conn()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, filename, video_path, vlm_model, scenario, custom_prompt,
                   camera_id, location, capture_type, status, batch_done,
                   total_batches, frames, duration, error_msg, worker_id,
                   started_at, upload_timestamp
            FROM VSS2_VIDEOS WHERE id=:1
        """, [video_id])
        cols = [c[0].lower() for c in cur.description]
        row = cur.fetchone()
    if not row:
        conn.close()
        return None
    result = _json_safe(dict(zip(cols, row)))
    conn.close()
    return result


def _db_list_videos() -> List[Dict]:
    conn = _adb_conn()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, filename, video_path, vlm_model, scenario,
                   status, batch_done, total_batches, frames, duration,
                   error_msg, worker_id, started_at, upload_timestamp
            FROM VSS2_VIDEOS ORDER BY upload_timestamp DESC
        """)
        cols = [c[0].lower() for c in cur.description]
        rows = [_json_safe(dict(zip(cols, r))) for r in cur.fetchall()]
    conn.close()
    return rows


def _db_delete_video(video_id: str) -> Optional[str]:
    conn = _adb_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT video_path FROM VSS2_VIDEOS WHERE id=:1", [video_id])
        row = cur.fetchone()
        if not row:
            conn.close()
            return None
        path = row[0]
        cur.execute("DELETE FROM VSS2_VIDEOS WHERE id=:1", [video_id])
    conn.commit()
    conn.close()
    return path


def _db_get_chunks(video_id: str) -> List[Dict]:
    conn = _adb_conn()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT chunk_index, chunk_text, segment_start, segment_end
            FROM VSS2_CHUNKS WHERE video_id=:1
            ORDER BY chunk_index
        """, [video_id])
        cols = [c[0].lower() for c in cur.description]
        rows = []
        for r in cur.fetchall():
            row = dict(zip(cols, r))
            if hasattr(row.get("chunk_text"), "read"):
                row["chunk_text"] = row["chunk_text"].read()
            rows.append(row)
    conn.close()
    return rows


def _db_search(query_vec: List[float], top_k: int,
               camera_id: str = "", location: str = "",
               scenario: str = "", video_id: str = "") -> List[Dict]:
    """
    Basic text search — returns chunks from ready videos.
    For full vector similarity search, upgrade ADB to 23ai and use VECTOR columns.
    """
    conn = _adb_conn()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT c.video_id, c.chunk_index, c.chunk_text,
                   c.segment_start, c.segment_end,
                   v.filename, v.scenario, v.camera_id, v.location,
                   v.capture_type, v.vlm_model, v.duration,
                   0.9 AS similarity
            FROM VSS2_CHUNKS c
            JOIN VSS2_VIDEOS v ON c.video_id = v.id
            WHERE v.status = 'ready'
              AND (:video_id IS NULL OR c.video_id = :video_id)
              AND (:camera_id IS NULL OR v.camera_id LIKE :camera_id)
              AND (:location IS NULL OR v.location LIKE :location)
              AND (:scenario IS NULL OR v.scenario = :scenario)
            FETCH FIRST :top_k ROWS ONLY
        """, dict(
            video_id=video_id if video_id else None,
            camera_id=f"%{camera_id}%" if camera_id else None,
            location=f"%{location}%" if location else None,
            scenario=scenario if scenario else None,
            top_k=top_k,
        ))
        cols = [c[0].lower() for c in cur.description]
        rows = [_json_safe(dict(zip(cols, r))) for r in cur.fetchall()]
    conn.close()
    return rows


def _estimate_tokens(text: Any) -> int:
    if text is None:
        return 0
    return max(1, round(len(str(text)) / 4))


def _db_audit(limit: int = 50) -> List[Dict]:
    conn = _adb_conn()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT v.id, v.filename, v.video_path, v.vlm_model, v.scenario,
                   v.status, v.batch_done, v.total_batches, v.frames, v.duration,
                   v.error_msg, v.worker_id, v.started_at, v.upload_timestamp,
                   DBMS_LOB.GETLENGTH(v.custom_prompt) AS prompt_chars,
                   (SELECT COUNT(*)
                      FROM VSS2_CHUNKS c
                     WHERE c.video_id = v.id) AS chunk_count,
                   (SELECT NVL(SUM(DBMS_LOB.GETLENGTH(c.chunk_text)), 0)
                      FROM VSS2_CHUNKS c
                     WHERE c.video_id = v.id) AS output_chars
            FROM VSS2_VIDEOS v
            ORDER BY v.upload_timestamp DESC
            FETCH FIRST :limit ROWS ONLY
        """, {"limit": limit})
        cols = [c[0].lower() for c in cur.description]
        rows = [_json_safe(dict(zip(cols, r))) for r in cur.fetchall()]
    conn.close()
    return rows


# ── OCI GenAI client ──────────────────────────────────────────────────────────
def _build_client() -> GenerativeAiInferenceClient:
    signer = InstancePrincipalsSecurityTokenSigner()
    return GenerativeAiInferenceClient(
        config={"region": "us-ashburn-1"},
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
def _clip_context(text: str, limit: int = 7000) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    edge = max(1000, limit // 2)
    return f"{text[:edge]}\n\n[...middle omitted for brevity...]\n\n{text[-edge:]}"


def _synthesize(query: str, results: List[Dict], llm_model: str) -> str:
    parts = []
    for i, r in enumerate(results[:8]):
        ts = f"{r['segment_start']:.0f}s–{r['segment_end']:.0f}s"
        parts.append(
            f"[{i+1}] {r['filename']} ({ts}):\n{_clip_context(r.get('chunk_text', ''))}"
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
    user_msg.content = [TextContent(text=f"Question: {query}\n\nVideo analysis segments:\n{context}")]
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

app = FastAPI(title="VSS2 — Video Search & Summarization", version="2.0.0")
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
    return {"status": "healthy", "region": "us-ashburn-1", "db": "oracle-adb"}


@app.get("/api/models")
def models():
    return {"vlmModels": VLM_MODELS, "llmModels": LLM_MODELS}


@app.get("/api/scenarios")
def scenarios():
    return {"scenarios": [
        {"id": k, "label": k.replace("_", " ").title()}
        for k in SCENARIO_PROMPTS
    ]}


def _object_storage_client():
    signer = InstancePrincipalsSecurityTokenSigner()
    return oci.object_storage.ObjectStorageClient(
        config={"region": OBJECT_STORAGE_REGION},
        signer=signer,
    )


def _safe_object_name(filename: str) -> str:
    base = Path(filename or "race-video.mp4").name.strip() or "race-video.mp4"
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in base)
    safe = safe.strip("-") or "race-video.mp4"
    prefix = OBJECT_STORAGE_PREFIX.strip("/")
    return f"{prefix}/{uuid.uuid4().hex}-{safe}" if prefix else f"{uuid.uuid4().hex}-{safe}"


def _parse_oci_uri(uri: str):
    raw = uri.replace("oci://", "", 1)
    parts = raw.split("/", 2)
    if len(parts) != 3:
        raise HTTPException(500, "Invalid Object Storage URI stored for video.")
    return parts[0], parts[1], parts[2]


def _queue_object_storage_video(
    object_name: str,
    filename: str,
    vlm_model: str,
    scenario: str,
    custom_prompt: str,
    camera_id: str = "",
    location: str = "",
    capture_type: str = "",
):
    video_id = uuid.uuid4().hex
    video_path = f"oci://{OBJECT_STORAGE_NAMESPACE}/{OBJECT_STORAGE_BUCKET}/{object_name}"
    _db_insert_video(
        video_id, filename, video_path,
        vlm_model, scenario, (custom_prompt or "")[:800],
        camera_id, location, capture_type,
    )
    return video_id, video_path


@app.post("/api/upload-to-storage")
async def upload_to_storage(
    file: UploadFile = File(...),
    vlm_model: str = Form(DEFAULT_VLM),
    scenario: str = Form("sports"),
    custom_prompt: str = Form(""),
    camera_id: str = Form(""),
    location: str = Form(""),
    capture_type: str = Form("sports"),
) -> JSONResponse:
    payload = await file.read()
    if not payload:
        raise HTTPException(400, "Uploaded video is empty.")

    object_name = _safe_object_name(file.filename or "race-video.mp4")
    try:
        _object_storage_client().put_object(
            OBJECT_STORAGE_NAMESPACE,
            OBJECT_STORAGE_BUCKET,
            object_name,
            payload,
            content_type=file.content_type or "application/octet-stream",
        )
    except Exception as exc:
        raise HTTPException(500, f"Object Storage upload failed: {exc}") from exc

    return JSONResponse({
        "status": "uploaded",
        "object_name": object_name,
        "bucket": OBJECT_STORAGE_BUCKET,
        "file_size_bytes": len(payload),
        "message": "Uploaded to Object Storage. Call /api/analyze-from-storage to queue analysis.",
    })


@app.post("/api/analyze-from-storage")
def analyze_from_storage(
    object_name: str = Form(..., description="Object name in Object Storage e.g. uploads/race.mp4"),
    vlm_model: str = Form(DEFAULT_VLM),
    scenario: str = Form("sports"),
    custom_prompt: str = Form(""),
) -> JSONResponse:
    try:
        head = _object_storage_client().head_object(
            OBJECT_STORAGE_NAMESPACE,
            OBJECT_STORAGE_BUCKET,
            object_name,
        )
        file_size = head.headers.get("content-length", "unknown")
    except Exception as exc:
        raise HTTPException(404, f"Object not found in Object Storage: {object_name} - {exc}") from exc

    filename = object_name.split("/")[-1]
    video_id, video_path = _queue_object_storage_video(
        object_name=object_name,
        filename=filename,
        vlm_model=vlm_model,
        scenario=scenario,
        custom_prompt=custom_prompt,
    )
    return JSONResponse({
        "videoId": video_id,
        "status": "pending",
        "object_name": object_name,
        "video_path": video_path,
        "bucket": OBJECT_STORAGE_BUCKET,
        "file_size_bytes": file_size,
        "message": "Object Storage video queued for analysis",
    })


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
        vlm_model, scenario, (custom_prompt or "")[:800],
        camera_id, location, capture_type,
    )
    return JSONResponse({"videoId": video_id, "status": "pending"})


@app.get("/api/videos/{video_id}/status")
def analysis_status(video_id: str) -> JSONResponse:
    row = _db_get_video(video_id)
    if not row:
        return JSONResponse({"status": "not_found"})
    resp: Dict[str, Any] = {
        "status":        row["status"],
        "frames":        row["frames"],
        "duration":      float(row["duration"] or 0),
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
        if hasattr(started, "tzinfo") and not started.tzinfo:
            started = started.replace(tzinfo=timezone.utc)
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        avg = elapsed / row["batch_done"]
        remaining = row["total_batches"] - row["batch_done"]
        resp["elapsed"] = round(elapsed, 1)
        resp["eta"] = round(avg * remaining, 1) if remaining > 0 else 0.0
    return JSONResponse(_json_safe(resp))


@app.get("/api/videos/{video_id}/summary")
def get_summary(video_id: str) -> JSONResponse:
    """Return the structured JSON race summary for sports scenario videos."""
    row = _db_get_video(video_id)
    if not row:
        raise HTTPException(404, "Video not found")
    if row["status"] != "ready":
        return JSONResponse({"status": row["status"], "summary": None})
    chunks = _db_get_chunks(video_id)
    if not chunks:
        return JSONResponse({"status": "ready", "summary": None})
    # For sports scenario, first chunk contains the JSON
    raw = chunks[0]["chunk_text"]
    if hasattr(raw, "read"):
        raw = raw.read()
    try:
        # Strip markdown fences if present
        clean = str(raw).strip()
        if clean.startswith("```"):
            clean = clean.split("```")[1]
            if clean.startswith("json"):
                clean = clean[4:]
        summary = json.loads(clean.strip())
    except Exception:
        summary = {"raw": str(raw)}
    return JSONResponse({"status": "ready", "summary": summary})


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
        custom_prompt=(custom_prompt or "")[:800], status="pending",
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
            r["started_at"] = str(r.get("started_at", ""))
            r["duration"] = float(r.get("duration") or 0)
        return JSONResponse({"videos": rows})
    except Exception as e:
        return JSONResponse({"videos": [], "error": str(e)})


@app.get("/api/audit")
def audit(limit: int = 50) -> JSONResponse:
    limit = max(1, min(int(limit or 50), 200))
    rows = _db_audit(limit)
    analyses = []
    totals = {
        "analyses": len(rows),
        "ready": 0,
        "pending": 0,
        "analysing": 0,
        "error": 0,
        "output_chars": 0,
        "estimated_output_tokens": 0,
        "estimated_prompt_tokens": 0,
        "estimated_total_text_tokens": 0,
    }
    for row in rows:
        status = row.get("status") or "unknown"
        if status in totals:
            totals[status] += 1
        output_chars = int(row.get("output_chars") or 0)
        prompt_chars = int(row.get("prompt_chars") or 0)
        output_tokens = _estimate_tokens("x" * output_chars) if output_chars else 0
        prompt_tokens = _estimate_tokens("x" * prompt_chars) if prompt_chars else 0
        total_tokens = output_tokens + prompt_tokens
        totals["output_chars"] += output_chars
        totals["estimated_output_tokens"] += output_tokens
        totals["estimated_prompt_tokens"] += prompt_tokens
        totals["estimated_total_text_tokens"] += total_tokens
        video_path = str(row.get("video_path") or "")
        analyses.append({
            "video_id": row.get("id"),
            "filename": row.get("filename"),
            "status": status,
            "model": row.get("vlm_model"),
            "scenario": row.get("scenario"),
            "duration_seconds": float(row.get("duration") or 0),
            "batch_done": int(row.get("batch_done") or 0),
            "total_batches": int(row.get("total_batches") or 0),
            "frames": int(row.get("frames") or 0),
            "chunk_count": int(row.get("chunk_count") or 0),
            "output_chars": output_chars,
            "estimated_output_tokens": output_tokens,
            "estimated_prompt_tokens": prompt_tokens,
            "estimated_total_text_tokens": total_tokens,
            "storage_source": "Object Storage" if video_path.startswith("oci://") else "VM disk",
            "worker_id": row.get("worker_id") or "",
            "started_at": row.get("started_at"),
            "upload_timestamp": row.get("upload_timestamp"),
            "error": row.get("error_msg") or "",
        })
    return JSONResponse(_json_safe({
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "limit": limit,
        "token_accounting": "estimated_from_stored_text; OCI GenAI request/vision token usage was not stored historically",
        "totals": totals,
        "analyses": analyses,
    }))


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
    raw_path = str(_json_safe(row["video_path"]))
    if raw_path.startswith("oci://"):
        namespace, bucket, object_name = _parse_oci_uri(raw_path)
        try:
            response = _object_storage_client().get_object(namespace, bucket, object_name)
        except Exception as exc:
            raise HTTPException(404, f"Video object not found: {exc}") from exc
        return StreamingResponse(
            response.data.raw.stream(1024 * 1024, decode_content=False),
            media_type="video/mp4",
            headers={"Accept-Ranges": "bytes"},
        )
    path = Path(raw_path)
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
    video_id: str = ""


@app.post("/api/search")
def search(req: SearchRequest) -> JSONResponse:
    if not req.query.strip():
        raise HTTPException(400, "Query cannot be empty")
    t0 = time.time()
    q_vec = _embed(req.query)
    embed_ms = round((time.time() - t0) * 1000, 1)

    t1 = time.time()
    results = _db_search(q_vec, req.top_k, req.camera_id, req.location, req.scenario, req.video_id)
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
        r["similarity"] = round(float(r.get("similarity") or 0), 4)
        r["segment_start"] = float(r.get("segment_start") or 0)
        r["segment_end"] = float(r.get("segment_end") or 0)
        r["duration"] = float(r.get("duration") or 0)

    return JSONResponse(_json_safe({
        "results": results,
        "synthesis": synthesis,
        "total": len(results),
        "embed_ms": embed_ms,
        "search_ms": search_ms,
        "llm_ms": llm_ms,
    }))


@app.get("/", response_class=HTMLResponse)
def index():
    html_path = STATIC_DIR / "index.html"
    if html_path.exists():
        return HTMLResponse(
            html_path.read_text(),
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
            },
        )
    return HTMLResponse(
        "<h1>VSS2 Sports Analytics</h1><p>Frontend not found. Check STATIC_DIR.</p>",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
        },
    )
