#!/usr/bin/env python3
"""VSS2 Worker — polls PostgreSQL for pending video jobs and processes them.

Multiple worker pods process videos in parallel safely using
  SELECT ... FOR UPDATE SKIP LOCKED
so no two workers ever claim the same video.

Horizontal scaling is driven by a Kubernetes HPA watching CPU utilisation
on this deployment. Frame extraction (ffmpeg) and local embedding (fastembed)
are CPU-intensive and will cause the HPA to spin up additional replicas when
the queue is deep.
"""
import base64
import json
import os
import socket
import subprocess
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import oci
import psycopg2
import psycopg2.extras
import requests
from fastembed import TextEmbedding
from oci.auth.signers import InstancePrincipalsSecurityTokenSigner
from oci.generative_ai_inference import GenerativeAiInferenceClient
from oci.generative_ai_inference.models import (
    ChatDetails, GenericChatRequest, ImageContent, ImageUrl,
    OnDemandServingMode, TextContent, UserMessage,
)

# ── Environment ────────────────────────────────────────────────────────────────
PG_HOST             = os.environ["PG_HOST"]
PG_PORT             = int(os.environ.get("PG_PORT", 5432))
PG_DB               = os.environ["PG_DB"]
PG_USER             = os.environ["PG_USER"]
PG_PASSWORD         = os.environ["PG_PASSWORD"]
FASTEMBED_CACHE_DIR = os.environ.get("FASTEMBED_CACHE_DIR", "/mnt/fss/vss2/fastembed-cache")
COMPARTMENT_ID      = os.environ["COMPARTMENT_ID"]

GEMINI_25_PRO_OCID   = os.environ["GEMINI_25_PRO_OCID"]
GEMINI_25_FLASH_OCID = os.environ["GEMINI_25_FLASH_OCID"]
COHERE_CMD_A_VISION  = os.environ["COHERE_CMD_A_VISION"]

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

WORKER_ID             = socket.gethostname()    # pod name acts as worker ID
POLL_INTERVAL         = 5                        # seconds to sleep when queue empty
STALE_TIMEOUT_MINUTES = 20                       # reset stuck 'analysing' jobs

FRAMES_PER_SECOND    = 1.0
MAX_FRAMES_PER_BATCH = 24
MAX_TOTAL_FRAMES     = 300
NVIDIA_MAX_VIDEO_MB  = 48

# ── Analysis scenarios ─────────────────────────────────────────────────────────
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

# ── Embedding model singleton ──────────────────────────────────────────────────
_embed_model = None


def _get_embed_model():
    global _embed_model
    if _embed_model is None:
        _embed_model = TextEmbedding(
            "BAAI/bge-small-en-v1.5",
            cache_dir=FASTEMBED_CACHE_DIR,
        )
    return _embed_model


def _embed(text: str) -> List[float]:
    return list(_get_embed_model().embed([text]))[0].tolist()


# ── DB helpers ─────────────────────────────────────────────────────────────────
def _pg_conn():
    return psycopg2.connect(
        host=PG_HOST, port=PG_PORT, dbname=PG_DB,
        user=PG_USER, password=PG_PASSWORD, connect_timeout=10,
    )


def _db_update(video_id: str, **kwargs) -> None:
    if not kwargs:
        return
    cols = ", ".join(f"{k}=%s" for k in kwargs)
    vals = list(kwargs.values()) + [video_id]
    with _pg_conn() as conn, conn.cursor() as cur:
        cur.execute(f"UPDATE vss2_videos SET {cols} WHERE id=%s", vals)
        conn.commit()


def _db_store_chunks(video_id: str, chunks: List[Dict]) -> None:
    with _pg_conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM vss2_chunks WHERE video_id=%s", (video_id,))
        for i, c in enumerate(chunks):
            vec = _embed(c["text"])
            cur.execute(
                """INSERT INTO vss2_chunks (video_id, chunk_index, chunk_text,
                   segment_start, segment_end, embedding)
                   VALUES (%s,%s,%s,%s,%s,%s)""",
                (video_id, i, c["text"], c["start"], c["end"], json.dumps(vec)),
            )
        conn.commit()


def _claim_video() -> Optional[Dict]:
    """Atomically claim one pending video using SELECT FOR UPDATE SKIP LOCKED."""
    try:
        with _pg_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT * FROM vss2_videos
                    WHERE status = 'pending'
                    ORDER BY upload_timestamp ASC
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                """)
                row = cur.fetchone()
                if not row:
                    conn.rollback()
                    return None
                cur.execute(
                    "UPDATE vss2_videos SET status='analysing', worker_id=%s, started_at=NOW() WHERE id=%s",
                    (WORKER_ID, row["id"]),
                )
                conn.commit()
                return dict(row)
    except Exception as e:
        print(f"[WORKER] DB error in _claim_video: {e}")
        return None


def _recover_stale_jobs() -> None:
    """Reset jobs stuck in 'analysing' longer than STALE_TIMEOUT_MINUTES."""
    try:
        with _pg_conn() as conn, conn.cursor() as cur:
            cur.execute(f"""
                UPDATE vss2_videos
                SET status='pending', worker_id=NULL, started_at=NULL, batch_done=0
                WHERE status='analysing'
                  AND started_at < NOW() - INTERVAL '{STALE_TIMEOUT_MINUTES} minutes'
            """)
            n = cur.rowcount
            conn.commit()
            if n > 0:
                print(f"[WORKER] Recovered {n} stale job(s)")
    except Exception as e:
        print(f"[WORKER] Stale job recovery error: {e}")


# ── Video frame extraction ─────────────────────────────────────────────────────
def _get_video_duration(path: str) -> float:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", path],
            capture_output=True, text=True, timeout=15,
        )
        for s in json.loads(r.stdout).get("streams", []):
            if s.get("codec_type") == "video":
                return float(s.get("duration", 30) or 30)
    except Exception:
        pass
    return 30.0


def _extract_frames_as_b64(video_path: str) -> Tuple[List[str], float]:
    """Extract 1 fps frames. Returns (b64_jpeg_list, duration_seconds)."""
    duration = _get_video_duration(video_path)
    n = min(max(int(duration * FRAMES_PER_SECOND), 1), MAX_TOTAL_FRAMES)
    frames: List[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(
            [
                "ffmpeg", "-i", video_path,
                "-vf", f"fps={FRAMES_PER_SECOND},scale=960:-2",
                "-frames:v", str(n), "-q:v", "2", f"{tmp}/f%04d.jpg",
            ],
            capture_output=True, check=True, timeout=300,
        )
        for fname in sorted(os.listdir(tmp)):
            if fname.endswith(".jpg"):
                with open(os.path.join(tmp, fname), "rb") as fh:
                    frames.append(base64.b64encode(fh.read()).decode("ascii"))
    return frames, duration


# ── OCI GenAI (batched frame analysis) ────────────────────────────────────────
def _build_oci_client() -> GenerativeAiInferenceClient:
    signer = InstancePrincipalsSecurityTokenSigner()
    return GenerativeAiInferenceClient(
        config={"region": "us-phoenix-1"},
        signer=signer,
        service_endpoint=OCI_INFERENCE_ENDPOINT,
        retry_strategy=oci.retry.NoneRetryStrategy(),
        timeout=(60, 300),
    )


def _extract_oci_text(resp) -> str:
    try:
        choices = resp.data.chat_response.choices
        if choices:
            c = choices[0].message.content
            return (
                "".join(getattr(x, "text", "") for x in c)
                if isinstance(c, list)
                else str(c)
            )
    except Exception:
        pass
    return ""


def _analyse_batch_oci(
    client,
    vlm_model: str,
    batch: List[str],
    batch_start: float,
    interval: float,
    duration: float,
    idx: int,
    total: int,
    scenario_prompt: str,
) -> str:
    batch_end = batch_start + len(batch) * interval
    content: List[Any] = [
        TextContent(text=(
            f"You are analysing segment {idx + 1} of {total} of a video "
            f"({duration:.1f}s total). This segment covers {batch_start:.1f}s "
            f"to {batch_end:.1f}s ({len(batch)} frames at 1 fps).\n\n"
            f"{scenario_prompt}\n\n"
            "For EACH frame, provide a detailed description. After all frames, "
            "write a brief segment summary."
        ))
    ]
    for i, b64 in enumerate(batch):
        t = batch_start + i * interval
        content.append(TextContent(text=f"\nFrame at {t:.1f}s:"))
        img = ImageContent()
        img.image_url = ImageUrl(url=f"data:image/jpeg;base64,{b64}")
        content.append(img)
    user = UserMessage()
    user.content = content
    req = GenericChatRequest()
    req.api_format = "GENERIC"
    req.messages = [user]
    req.max_tokens = 4000
    req.temperature = 0.1
    details = ChatDetails()
    details.compartment_id = COMPARTMENT_ID
    details.serving_mode = OnDemandServingMode(model_id=vlm_model)
    details.chat_request = req
    return _extract_oci_text(client.chat(details))


# ── Cosmos-Reason2 NIM (public or local) ──────────────────────────────────────
def _analyse_cosmos_video(
    video_path: str,
    duration: float,
    scenario_prompt: str,
    endpoint: str,
    api_key: str,
    model: str,
) -> str:
    """Send the entire video to a Cosmos-Reason2 NIM endpoint (public or local)."""
    with open(video_path, "rb") as fh:
        video_bytes = fh.read()
    size_mb = len(video_bytes) / (1024 * 1024)
    if size_mb > NVIDIA_MAX_VIDEO_MB:
        raise RuntimeError(
            f"Video is {size_mb:.1f} MB — Cosmos-Reason2 NIM accepts up to "
            f"{NVIDIA_MAX_VIDEO_MB} MB. Trim the video or choose an OCI GenAI model."
        )
    p = video_path.lower()
    mime = (
        "video/quicktime" if p.endswith(".mov") else
        "video/x-msvideo" if p.endswith(".avi") else
        "video/webm" if p.endswith(".webm") else
        "video/mp4"
    )
    video_b64 = base64.b64encode(video_bytes).decode("ascii")
    full_prompt = (
        f"This video is {duration:.1f}s long.\n\n{scenario_prompt}\n\n"
        "Provide a detailed, structured analysis of everything that happens, "
        "noting specific timestamps and observations throughout the video."
    )
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": full_prompt},
            {"type": "video_url", "video_url": {"url": f"data:{mime};base64,{video_b64}"}},
        ]}],
        "max_tokens": 8000,
        "temperature": 0.1,
    }
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    resp = requests.post(endpoint, headers=headers, json=payload, timeout=600)
    if resp.status_code != 200:
        raise RuntimeError(f"Cosmos NIM error {resp.status_code}: {resp.text[:600]}")
    choices = resp.json().get("choices", [])
    if not choices:
        raise RuntimeError("No choices in Cosmos NIM response")
    return choices[0].get("message", {}).get("content", "")


# ── Main job processor ─────────────────────────────────────────────────────────
def _process_video(video: Dict) -> None:
    video_id        = video["id"]
    video_path      = video["video_path"]
    vlm_model       = video["vlm_model"]
    scenario        = video["scenario"]
    custom_prompt   = video.get("custom_prompt", "")
    scenario_prompt = (
        custom_prompt.strip()
        if custom_prompt.strip()
        else SCENARIO_PROMPTS.get(scenario, SCENARIO_PROMPTS["general"])
    )

    is_cosmos_public = vlm_model.startswith("nvidia/")
    is_cosmos_local  = vlm_model == "local/cosmos-reason2"
    is_cosmos        = is_cosmos_public or is_cosmos_local

    try:
        if is_cosmos:
            # ── Cosmos-Reason2: entire video in one API call ──────────────────
            if is_cosmos_local:
                if not LOCAL_COSMOS_ENDPOINT:
                    raise RuntimeError(
                        "LOCAL_COSMOS_ENDPOINT is not configured. "
                        "Set it to your NIM service URL, e.g. "
                        "http://cosmos-nim.default.svc.cluster.local:8000/v1/chat/completions"
                    )
                endpoint = LOCAL_COSMOS_ENDPOINT
                api_key  = LOCAL_COSMOS_API_KEY
                model    = LOCAL_COSMOS_MODEL
            else:
                if not NVIDIA_API_KEY:
                    raise RuntimeError(
                        "NVIDIA_API_KEY is not configured. "
                        "Set NVIDIA_API_KEY in your secret to use the public NIM."
                    )
                endpoint = NVIDIA_NIM_ENDPOINT
                api_key  = NVIDIA_API_KEY
                model    = COSMOS_REASON2_MODEL

            duration = _get_video_duration(video_path)
            _db_update(video_id, total_batches=1, frames=0, duration=duration, batch_done=0)
            try:
                text = _analyse_cosmos_video(
                    video_path, duration, scenario_prompt, endpoint, api_key, model
                )
            except (OSError, requests.exceptions.ConnectionError) as conn_err:
                if is_cosmos_local:
                    raise RuntimeError(
                        f"Cannot reach local Cosmos NIM at {endpoint}. "
                        "Make sure the NIM service is deployed and LOCAL_COSMOS_ENDPOINT "
                        "is correct in your ConfigMap. "
                        f"(Original error: {conn_err})"
                    ) from conn_err
                raise
            _db_update(video_id, batch_done=1)
            chunks: List[Dict] = [{"text": text, "start": 0.0, "end": duration}]

        else:
            # ── OCI GenAI: 1fps batched frame analysis ────────────────────────
            frames, duration = _extract_frames_as_b64(video_path)
            if not frames:
                raise RuntimeError("No frames could be extracted from this video.")
            interval = 1.0 / FRAMES_PER_SECOND
            batches  = [frames[i:i + MAX_FRAMES_PER_BATCH]
                        for i in range(0, len(frames), MAX_FRAMES_PER_BATCH)]
            total    = len(batches)
            _db_update(video_id, total_batches=total, frames=len(frames), duration=duration)
            client = _build_oci_client()
            chunks = []
            for idx, batch in enumerate(batches):
                batch_start = idx * MAX_FRAMES_PER_BATCH * interval
                batch_end   = batch_start + len(batch) * interval
                text = _analyse_batch_oci(
                    client, vlm_model, batch, batch_start, interval,
                    duration, idx, total, scenario_prompt,
                )
                _db_update(video_id, batch_done=idx + 1)
                chunks.append({
                    "text":  f"Segment {idx + 1} ({batch_start:.0f}s–{batch_end:.0f}s):\n{text}",
                    "start": batch_start,
                    "end":   batch_end,
                })

        _db_store_chunks(video_id, chunks)
        _db_update(video_id, status="ready")
        print(f"[WORKER {WORKER_ID}] Done: {video['filename']} ({video_id})")

    except Exception:
        err = traceback.format_exc()
        print(f"[WORKER {WORKER_ID}] FAILED: {video['filename']}\n{err}")
        try:
            _db_update(video_id, status="error", error_msg=err[:1000])
        except Exception:
            pass


# ── Entry point ────────────────────────────────────────────────────────────────
def main() -> None:
    print(f"[WORKER {WORKER_ID}] Starting.")
    for attempt in range(30):
        try:
            with _pg_conn() as conn, conn.cursor() as cur:
                cur.execute("SELECT 1")
            print("[WORKER] DB connection OK.")
            break
        except Exception as e:
            print(f"[WORKER] Waiting for DB ({attempt + 1}/30): {e}")
            time.sleep(5)
    else:
        raise SystemExit("[WORKER] Could not connect to DB — giving up.")

    last_recovery = 0.0
    while True:
        try:
            if time.time() - last_recovery > 60:
                _recover_stale_jobs()
                last_recovery = time.time()
            video = _claim_video()
            if video:
                print(f"[WORKER {WORKER_ID}] Claimed: {video['filename']} ({video['id']})")
                _process_video(video)
            else:
                time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            print(f"[WORKER {WORKER_ID}] Shutting down.")
            break
        except Exception as e:
            print(f"[WORKER {WORKER_ID}] Loop error: {e}")
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
