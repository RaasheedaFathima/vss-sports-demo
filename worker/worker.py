#!/usr/bin/env python3
"""VSS2 Worker — polls Oracle ADB for pending video jobs and processes them.

Changes from Prachi's original:
  1. PostgreSQL → Oracle ADB (oracledb thin mode, no wallet needed for now)
  2. OCI GenAI Gemini path: native video upload (whole video) instead of 1fps frame batching
     → this is the key accuracy fix (Gemini sees the full video holistically)
  3. Region updated to us-ashburn-1 (where this instance lives)
  4. ADB Vector Search replaces pgvector for embeddings

Multiple worker pods process videos in parallel safely using
  SELECT ... FOR UPDATE SKIP LOCKED
so no two workers ever claim the same video.
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
import oracledb
import requests
from fastembed import TextEmbedding
from oci.auth.signers import InstancePrincipalsSecurityTokenSigner
from oci.generative_ai_inference import GenerativeAiInferenceClient
from oci.generative_ai_inference.models import (
    ChatDetails, GenericChatRequest, ImageContent, ImageUrl,
    OnDemandServingMode, TextContent, UserMessage,
)

# ── Environment ────────────────────────────────────────────────────────────────
# ADB connection (thin mode — no Oracle Instant Client or wallet needed for demo)
ADB_USER        = os.environ["ADB_USER"]
ADB_PASSWORD    = os.environ["ADB_PASSWORD"]
ADB_DSN         = os.environ["ADB_DSN"]        # e.g. "(description=(address=(protocol=tcps)...)" or just "host:port/service"

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

# ── CHANGED: us-ashburn-1 (where this VM lives + where Gemini 2.5 is available)
OCI_INFERENCE_ENDPOINT = "https://inference.generativeai.us-ashburn-1.oci.oraclecloud.com"

WORKER_ID             = socket.gethostname()
POLL_INTERVAL         = 5
STALE_TIMEOUT_MINUTES = 20

# Gemini native video limits (OCI GenAI)
GEMINI_MAX_VIDEO_MB  = 50      # OCI GenAI Gemini accepts up to 50MB inline
NVIDIA_MAX_VIDEO_MB  = 48

# Fallback frame-batching settings (used only for Cohere which doesn't support native video)
FRAMES_PER_SECOND    = 1.0
MAX_FRAMES_PER_BATCH = 60
MAX_TOTAL_FRAMES     = 300

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
        "You are an expert sports analyst specialising in track and field events. "
        "Analyse this COMPLETE race video holistically. "
        "The video may or may not have audio — if there is no audio or it is unclear, rely entirely on visual cues. "
        "The video may be low quality, wide angle, or have partially obscured bib numbers — do your best with what is visible. "
        "Return ONLY a JSON object with this exact schema, no markdown, no explanation:\n\n"
        "{\n"
        "  \"event_type\": \"string (e.g. 800m, 1500m)\",\n"
        "  \"venue\": \"string or null\",\n"
        "  \"has_audio\": true or false,\n"
        "  \"video_quality\": \"high|medium|low\",\n"
        "  \"total_runners\": number,\n"
        "  \"race_duration_seconds\": number or null,\n"
        "  \"winner\": {\n"
        "    \"name\": \"string or null\",\n"
        "    \"bib\": \"string or null\",\n"
        "    \"country\": \"string or null\",\n"
        "    \"finish_time\": \"MM:SS.ms or null\"\n"
        "  },\n"
        "  \"runners\": [\n"
        "    {\n"
        "      \"name\": \"string or null\",\n"
        "      \"bib\": \"string or null\",\n"
        "      \"country\": \"string or null\",\n"
        "      \"position\": number or null,\n"
        "      \"finish_time\": \"MM:SS.ms or null\",\n"
        "      \"split_400m\": \"MM:SS.ms or null\",\n"
        "      \"notable_events\": [\"string\"]\n"
        "    }\n"
        "  ],\n"
        "  \"second_by_second\": [\n"
        "    {\n"
        "      \"second\": number,\n"
        "      \"timestamp\": \"HH:MM:SS\",\n"
        "      \"positions\": [{\"bib\": \"string\", \"rank\": number}],\n"
        "      \"event\": \"string describing what is happening at this exact second\"\n"
        "    }\n"
        "  ],\n"
        "  \"commentary\": [\n"
        "    {\"timestamp\": \"HH:MM:SS\", \"event\": \"string\"}\n"
        "  ],\n"
        "  \"confidence\": \"high|medium|low\",\n"
        "  \"notes\": \"string — any caveats about video quality, missing audio, occluded bibs\"\n"
        "}\n\n"
        "For second_by_second: provide an entry for EVERY second of the video. "
        "For each second, list the bib numbers in current race position order (rank 1 = leading). "
        "Note any overtaking, lane changes, or significant events. "
        "If bib numbers are not visible at a particular second, use your best estimate based on runner positions. "
        "Use null for values you genuinely cannot determine. "
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


# ── ADB helpers ────────────────────────────────────────────────────────────────
def _adb_conn():
    """Return an Oracle ADB connection (thin mode — no Instant Client needed)."""
    return oracledb.connect(
        user=ADB_USER,
        password=ADB_PASSWORD,
        dsn=ADB_DSN,
        config_dir=os.environ.get("ADB_WALLET_DIR", "/home/opc/wallet"),
        wallet_location=os.environ.get("ADB_WALLET_DIR", "/home/opc/wallet"),
        wallet_password=ADB_PASSWORD,
    )


def _db_update(video_id: str, **kwargs) -> None:
    if not kwargs:
        return
    # Oracle uses :param_name style placeholders
    cols = ", ".join(f"{k}=:{k}" for k in kwargs)
    params = {**kwargs, "video_id": video_id}
    with _adb_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE VSS2_VIDEOS SET {cols} WHERE id=:video_id", params)
        conn.commit()


def _db_store_chunks(video_id: str, chunks: List[Dict]) -> None:
    """Store analysis chunks with vector embeddings in ADB."""
    with _adb_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM VSS2_CHUNKS WHERE video_id=:1", [video_id])
            for i, c in enumerate(chunks):
                vec = _embed(c["text"])
                vec_json = json.dumps(vec)
                cur.execute(
                    """INSERT INTO VSS2_CHUNKS
                       (video_id, chunk_index, chunk_text, segment_start, segment_end, embedding)
                       VALUES (:1, :2, :3, :4, :5, :6)""",
                    [video_id, i, c["text"], c["start"], c["end"], vec_json],
                )
        conn.commit()


def _claim_video() -> Optional[Dict]:
    """Atomically claim one pending video using SELECT FOR UPDATE SKIP LOCKED."""
    try:
        conn = _adb_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT id, filename, video_path, vlm_model, scenario, custom_prompt
            FROM VSS2_VIDEOS
            WHERE status = 'pending'
            AND ROWID = (
                SELECT MIN(ROWID) FROM VSS2_VIDEOS
                WHERE status = 'pending'
            )
            FOR UPDATE SKIP LOCKED
        """)
        row = cur.fetchone()
        if not row:
            conn.rollback()
            cur.close()
            conn.close()
            return None
        cols = ["id", "filename", "video_path", "vlm_model", "scenario", "custom_prompt"]
        record = dict(zip(cols, row))
        # Read LOB fields while connection is still open
        for k, v in record.items():
            if hasattr(v, "read"):
                record[k] = v.read()
        cur.execute(
            "UPDATE VSS2_VIDEOS SET status='analysing', worker_id=:1, started_at=CURRENT_TIMESTAMP WHERE id=:2",
            [WORKER_ID, record["id"]],
        )
        conn.commit()
        cur.close()
        conn.close()
        return record
    except Exception as e:
        print(f"[WORKER] DB error in _claim_video: {e}")
        return None


def _recover_stale_jobs() -> None:
    """Reset jobs stuck in 'analysing' longer than STALE_TIMEOUT_MINUTES."""
    try:
        with _adb_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                    UPDATE VSS2_VIDEOS
                    SET status='pending', worker_id=NULL, started_at=NULL, batch_done=0
                    WHERE status='analysing'
                      AND started_at < CURRENT_TIMESTAMP - INTERVAL '{STALE_TIMEOUT_MINUTES}' MINUTE
                """)
                n = cur.rowcount
                conn.commit()
            if n > 0:
                print(f"[WORKER] Recovered {n} stale job(s)")
    except Exception as e:
        print(f"[WORKER] Stale job recovery error: {e}")


# ── Video frame extraction (fallback for non-Gemini models) ───────────────────
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
    """Extract 1 fps frames. Returns (b64_jpeg_list, duration_seconds).
    Used only for Cohere which doesn't support native video upload.
    """
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


# ── OCI GenAI client ───────────────────────────────────────────────────────────
def _build_oci_client() -> GenerativeAiInferenceClient:
    signer = InstancePrincipalsSecurityTokenSigner()
    return GenerativeAiInferenceClient(
        config={"region": "us-ashburn-1"},
        signer=signer,
        service_endpoint=OCI_INFERENCE_ENDPOINT,
        retry_strategy=oci.retry.NoneRetryStrategy(),
        timeout=(60, 600),  # 10 min timeout for video processing
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


# ── KEY CHANGE: Gemini native video analysis (whole video, one API call) ───────
def _analyse_video_gemini_native(
    client,
    vlm_model: str,
    video_path: str,
    duration: float,
    scenario_prompt: str,
) -> str:
    """Send the ENTIRE video to Gemini via OCI GenAI in one call.

    This is the key accuracy improvement over Prachi's 1fps frame-batching approach.
    Gemini sees the full video holistically and can track runners across time,
    giving much more accurate timing, participant counts, and commentary.

    Supports up to 50MB inline (base64). Larger videos should be uploaded to
    Object Storage and passed as URI — add that path if needed later.
    """
    with open(video_path, "rb") as fh:
        video_bytes = fh.read()

    size_mb = len(video_bytes) / (1024 * 1024)
    print(f"[WORKER] Video size: {size_mb:.1f} MB")

    if size_mb > GEMINI_MAX_VIDEO_MB:
        raise RuntimeError(
            f"Video is {size_mb:.1f} MB — exceeds {GEMINI_MAX_VIDEO_MB} MB inline limit. "
            "Upload to OCI Object Storage and pass as URI instead."
        )

    # Detect MIME type
    p = video_path.lower()
    mime = (
        "video/quicktime" if p.endswith(".mov") else
        "video/x-msvideo" if p.endswith(".avi") else
        "video/webm" if p.endswith(".webm") else
        "video/mp4"
    )

    video_b64 = base64.b64encode(video_bytes).decode("ascii")

    # Build OCI GenAI request with video as inline image content
    text_intro = TextContent(text=(
        f"This is a {duration:.1f}s race video. Analyse it completely and holistically.\n\n"
        f"{scenario_prompt}"
    ))

    # OCI GenAI Gemini accepts video as ImageContent with base64 data URL
    video_content = ImageContent()
    video_content.image_url = ImageUrl(url=f"data:{mime};base64,{video_b64}")

    user_msg = UserMessage()
    user_msg.content = [text_intro, video_content]

    req = GenericChatRequest()
    req.api_format = "GENERIC"
    req.messages = [user_msg]
    req.max_tokens = 8000
    req.temperature = 0.1

    details = ChatDetails()
    details.compartment_id = COMPARTMENT_ID
    details.serving_mode = OnDemandServingMode(model_id=vlm_model)
    details.chat_request = req

    print(f"[WORKER] Sending full video to Gemini via OCI GenAI...")
    resp = client.chat(details)
    return _extract_oci_text(resp)


# ── Fallback: OCI GenAI batched frame analysis (Cohere) ───────────────────────
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



# ── Object Storage download helper ───────────────────────────────────────────
def _download_from_object_storage(video_path: str) -> str:
    """Download video from Object Storage to a temp file. Returns local path."""
    import oci
    # Parse oci://namespace/bucket/object_name
    path = video_path.replace("oci://", "")
    parts = path.split("/", 2)
    namespace, bucket, object_name = parts[0], parts[1], parts[2]

    signer = InstancePrincipalsSecurityTokenSigner()
    os_client = oci.object_storage.ObjectStorageClient(
        config={"region": "us-ashburn-1"}, signer=signer
    )

    ext = os.path.splitext(object_name)[1] or ".mp4"
    tmp_path = tempfile.mktemp(suffix=ext)

    print(f"[WORKER] Downloading from Object Storage: {object_name}")
    response = os_client.get_object(namespace, bucket, object_name)
    with open(tmp_path, "wb") as f:
        for chunk in response.data.raw.stream(1024 * 1024, decode_content=False):
            f.write(chunk)
    print(f"[WORKER] Downloaded to {tmp_path} ({os.path.getsize(tmp_path) / 1024 / 1024:.1f} MB)")
    return tmp_path

# ── Main job processor ─────────────────────────────────────────────────────────
def _analyse_video_gemini_frames(
    client,
    vlm_model: str,
    video_path: str,
    duration: float,
    scenario_prompt: str,
    fps: float = 2.0,
) -> str:
    """Extract frames at 2fps and send all to Gemini in one call.
    Gives true frame-by-frame analysis while keeping Gemini's holistic reasoning.
    """
    print(f"[WORKER] Extracting frames at {fps}fps...")
    frames = []
    timestamps = []
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(
            [
                "ffmpeg", "-i", video_path,
                "-vf", f"fps={fps},scale=960:-2",
                "-q:v", "2", f"{tmp}/f%04d.jpg",
            ],
            capture_output=True, check=True, timeout=300,
        )
        for fname in sorted(os.listdir(tmp)):
            if fname.endswith(".jpg"):
                idx = len(frames)
                t = idx / fps
                timestamps.append(t)
                with open(os.path.join(tmp, fname), "rb") as fh:
                    frames.append(base64.b64encode(fh.read()).decode("ascii"))

    BATCH_SIZE = 60
    batches = []
    for i in range(0, len(frames), BATCH_SIZE):
        batches.append((frames[i:i+BATCH_SIZE], timestamps[i:i+BATCH_SIZE]))
    print(f"[WORKER] Extracted {len(frames)} frames at {fps}fps → {len(batches)} batches. Sending in parallel...")

    def _analyse_batch(batch_idx, batch_frames, batch_ts):
        t_start = batch_ts[0]
        t_end = batch_ts[-1]
        content: List[Any] = [
            TextContent(text=(
                f"You are analysing batch {batch_idx+1} of {len(batches)} of a {duration:.1f}s race video.\n"
                f"This segment covers {t_start:.1f}s to {t_end:.1f}s ({len(batch_frames)} frames at {fps}fps).\n\n"
                f"{scenario_prompt}\n\n"
                "For EACH frame describe runner positions and any overtaking. "
                "Return a JSON object with these fields: event_type, venue, has_audio, video_quality, "
                "total_runners, winner, runners, second_by_second, commentary, confidence, notes. "
                "For second_by_second include an entry for every frame in this segment only."
            ))
        ]
        for i, (b64, t) in enumerate(zip(batch_frames, batch_ts)):
            mins = int(t // 60)
            secs = t % 60
            ts = f"{mins:02d}:{secs:05.2f}"
            content.append(TextContent(text=f"\n--- Frame {i+1} | Timestamp {ts} ({t:.2f}s) ---"))
            img = ImageContent()
            img.image_url = ImageUrl(url=f"data:image/jpeg;base64,{b64}")
            content.append(img)
        user_msg = UserMessage()
        user_msg.content = content
        req = GenericChatRequest()
        req.api_format = "GENERIC"
        req.messages = [user_msg]
        req.max_tokens = 32000
        req.temperature = 0.1
        details = ChatDetails()
        details.compartment_id = COMPARTMENT_ID
        details.serving_mode = OnDemandServingMode(model_id=vlm_model)
        details.chat_request = req
        resp = client.chat(details)
        return batch_idx, _extract_oci_text(resp)

    import concurrent.futures
    results = [None] * len(batches)
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(batches), 5)) as executor:
        futures = {
            executor.submit(_analyse_batch, idx, bf, bt): idx
            for idx, (bf, bt) in enumerate(batches)
        }
        for future in concurrent.futures.as_completed(futures):
            idx, text = future.result()
            results[idx] = text
            print(f"[WORKER] Batch {idx+1}/{len(batches)} complete")

    # Merge all segments into final JSON using Gemini
    combined = "\n\n".join([
        f"=== SEGMENT {i+1} ({batches[i][1][0]:.0f}s-{batches[i][1][-1]:.0f}s) ===\n{r}"
        for i, r in enumerate(results) if r
    ])
    print(f"[WORKER] All {len(batches)} batches done. Merging into final JSON...")
    merge_msg = UserMessage()
    merge_msg.content = [TextContent(text=(
        f"Merge these {len(batches)} sequential race video analysis segments into one final JSON.\n"
        f"Total race duration: {duration:.1f}s\n\n"
        f"{scenario_prompt}\n\n"
        "Segments:\n" + combined + "\n\n"
        "Rules: combine all second_by_second entries in order, merge runner info, "
        "determine winner from last segment if finish line visible, "
        "Return ONLY the merged JSON, no markdown."
    ))]
    merge_req = GenericChatRequest()
    merge_req.api_format = "GENERIC"
    merge_req.messages = [merge_msg]
    merge_req.max_tokens = 65536
    merge_req.temperature = 0.1
    merge_details = ChatDetails()
    merge_details.compartment_id = COMPARTMENT_ID
    merge_details.serving_mode = OnDemandServingMode(model_id=vlm_model)
    merge_details.chat_request = merge_req
    merge_resp = client.chat(merge_details)
    return _extract_oci_text(merge_resp)
def _process_video(video: Dict) -> None:
    video_id        = video["id"]
    video_path_raw  = video["video_path"]
    if hasattr(video_path_raw, "read"):
        video_path_raw = video_path_raw.read()
    video_path_raw = str(video_path_raw).strip()
    print(f"[WORKER] video_path_raw type={type(video_path_raw).__name__} value={str(video_path_raw)[:80]}")
    vlm_model       = video["vlm_model"]

    # Download from Object Storage if needed
    _tmp_file = None
    if video_path_raw.startswith("oci://"):
        _tmp_file = _download_from_object_storage(video_path_raw)
        video_path = _tmp_file
    else:
        video_path = video_path_raw
    scenario        = video["scenario"]
    custom_prompt   = video.get("custom_prompt") or ""
    if hasattr(custom_prompt, "read"):
        custom_prompt = custom_prompt.read()
    custom_prompt   = video.get("custom_prompt") or ""
    if hasattr(custom_prompt, "read"):
        custom_prompt = custom_prompt.read()
    custom_prompt = str(custom_prompt).strip()
    scenario_prompt = (
        custom_prompt
        if custom_prompt
        else SCENARIO_PROMPTS.get(scenario, SCENARIO_PROMPTS["general"])
    )

    is_cosmos_public = vlm_model.startswith("nvidia/")
    is_cosmos_local  = vlm_model == "local/cosmos-reason2"
    is_cosmos        = is_cosmos_public or is_cosmos_local

    # Gemini models (via OCI GenAI) — use native video path for best accuracy
    is_gemini = vlm_model in (GEMINI_25_PRO_OCID, GEMINI_25_FLASH_OCID)

    try:
        if is_cosmos:
            # ── Cosmos-Reason2: entire video in one API call ──────────────────
            if is_cosmos_local:
                if not LOCAL_COSMOS_ENDPOINT:
                    raise RuntimeError(
                        "LOCAL_COSMOS_ENDPOINT is not configured. "
                        "Set it to your NIM service URL."
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
            text = _analyse_cosmos_video(
                video_path, duration, scenario_prompt, endpoint, api_key, model
            )
            _db_update(video_id, batch_done=1)
            chunks: List[Dict] = [{"text": text, "start": 0.0, "end": duration}]

        elif is_gemini:
            # ── Gemini: frame-by-frame for sports, native video for others ──
            duration = _get_video_duration(video_path)
            client = _build_oci_client()
            if scenario == "sports":
                _db_update(video_id, total_batches=1, frames=0, duration=duration, batch_done=0)
                text = _analyse_video_gemini_frames(
                    client, vlm_model, video_path, duration, scenario_prompt, fps=0.5
                )
            else:
                _db_update(video_id, total_batches=1, frames=0, duration=duration, batch_done=0)
                text = _analyse_video_gemini_native(
                    client, vlm_model, video_path, duration, scenario_prompt
                )
            _db_update(video_id, batch_done=1)
            chunks = [{"text": text, "start": 0.0, "end": duration}]
        else:
            # ── Fallback: 1fps batched frame analysis (Cohere and others) ────
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
            _db_update(video_id, status="error", error_msg=err[:4000])
        except Exception:
            pass
    finally:
        # Clean up temp file if downloaded from Object Storage
        if _tmp_file and os.path.exists(_tmp_file):
            os.unlink(_tmp_file)
            print(f"[WORKER] Cleaned up temp file: {_tmp_file}")


# ── Entry point ────────────────────────────────────────────────────────────────
def main() -> None:
    print(f"[WORKER {WORKER_ID}] Starting. Connecting to ADB...")
    for attempt in range(30):
        try:
            with _adb_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1 FROM DUAL")
            print("[WORKER] ADB connection OK.")
            break
        except Exception as e:
            print(f"[WORKER] Waiting for ADB ({attempt + 1}/30): {e}")
            time.sleep(5)
    else:
        raise SystemExit("[WORKER] Could not connect to ADB — giving up.")

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

# ── Object Storage download helper ───────────────────────────────────────────
def _download_from_object_storage(video_path: str) -> str:
    """Download video from Object Storage to a temp file. Returns local path."""
    import oci
    # Parse oci://namespace/bucket/object_name
    path = video_path.replace("oci://", "")
    parts = path.split("/", 2)
    namespace, bucket, object_name = parts[0], parts[1], parts[2]

    signer = InstancePrincipalsSecurityTokenSigner()
    os_client = oci.object_storage.ObjectStorageClient(
        config={"region": "us-ashburn-1"}, signer=signer
    )

    ext = os.path.splitext(object_name)[1] or ".mp4"
    tmp_path = tempfile.mktemp(suffix=ext)

    print(f"[WORKER] Downloading from Object Storage: {object_name}")
    response = os_client.get_object(namespace, bucket, object_name)
    with open(tmp_path, "wb") as f:
        for chunk in response.data.raw.stream(1024 * 1024, decode_content=False):
            f.write(chunk)
    print(f"[WORKER] Downloaded to {tmp_path} ({os.path.getsize(tmp_path) / 1024 / 1024:.1f} MB)")
    return tmp_path

# ── Main job processor ─────────────────────────────────────────────────────────
# ── Entry point ────────────────────────────────────────────────────────────────
def main() -> None:
    print(f"[WORKER {WORKER_ID}] Starting. Connecting to ADB...")
    for attempt in range(30):
        try:
            with _adb_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1 FROM DUAL")
            print("[WORKER] ADB connection OK.")
            break
        except Exception as e:
            print(f"[WORKER] Waiting for ADB ({attempt + 1}/30): {e}")
            time.sleep(5)
    else:
        raise SystemExit("[WORKER] Could not connect to ADB — giving up.")

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
# ── Gemini 2fps frame-by-frame analysis (sports scenario) ─────────────────────
