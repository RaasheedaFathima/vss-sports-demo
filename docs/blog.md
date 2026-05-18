# How We Built an AI Race Analyst on Oracle Cloud

*A deep-dive into VSS2: building video intelligence for sports with VLMs, pgvector, and Kubernetes*

---

Imagine uploading a 20-minute race video and asking, ten seconds later, *"Who was leading after the first chicane?"* — and getting a timestamped, coherent answer. That is exactly what VSS2 does, and in this post I will walk through how we built it, why we made the architectural choices we did, and how the same system can be applied far beyond motorsport.

## The Problem

Video is the richest — and the most underused — data format in sports analysis. Coaches spend hours scrubbing through footage. Race directors replay incidents frame by frame. Broadcasters manually annotate highlights. None of this scales.

Modern Vision-Language Models can understand video the same way a human analyst does: they see what is happening, describe it in natural language, and reason about sequences of events. The challenge is turning that capability into a reliable, scalable, production-quality system.

## What VSS2 Does

VSS2 (Video Search & Summarization v2) is a cloud-native platform that:

1. **Accepts any video upload** through a web interface
2. **Analyses it automatically** using one or more VLM backends
3. **Stores the analysis as searchable text** with vector embeddings
4. **Answers natural-language questions** about the content
5. **Generates PDF race reports** with per-heat breakdowns and timelines

The demo focuses on motorsport — karting, circuit racing, endurance events — but the same architecture handles CCTV surveillance, retail foot-traffic analysis, warehouse safety monitoring, and traffic incident detection.

## The Architecture

The system runs entirely on OCI (Oracle Cloud Infrastructure) using OKE (Oracle Container Engine for Kubernetes). There are three moving parts:

### The API (FastAPI)

A stateless Python service that handles:
- **Upload**: saves the video to shared NFS storage, inserts a `pending` job into PostgreSQL
- **Status polling**: the frontend polls every 3 seconds; the API reads progress from the DB
- **Search**: converts a text query to a vector embedding and runs a pgvector similarity search
- **Streaming**: serves the video file over HTTP range requests for in-browser playback

Crucially, the API does no analysis. It hands off immediately and returns a job ID. This keeps response times fast regardless of how many videos are in-flight.

### The Worker Pods (Kubernetes + HPA)

This is where the heavy lifting happens. Each worker pod:

1. Polls the database using `SELECT ... FOR UPDATE SKIP LOCKED` — this atomically claims exactly one unclaimed job, even if dozens of workers are running simultaneously
2. Extracts 1 frame per second using **ffmpeg** (max 300 frames)
3. Sends batches of 24 frames to the configured VLM
4. Embeds each analysis paragraph using **fastembed** (BAAI/bge-small-en-v1.5, 384 dimensions)
5. Stores chunks + vectors in **pgvector**
6. Updates job status to `ready`

The Kubernetes HPA watches CPU utilisation. ffmpeg and fastembed are both CPU-bound, so when three videos hit the queue simultaneously, CPU spikes and the HPA adds replicas — up to 5 — within 30 seconds.

### The Database (PostgreSQL + pgvector)

PostgreSQL does double duty:

- **Job queue**: the `vss2_videos` table tracks status (`pending → analysing → ready/error`) and worker assignment; `FOR UPDATE SKIP LOCKED` turns it into a simple, broker-free work queue
- **Vector store**: the `vss2_chunks` table stores analysis text and 384-dimensional embeddings; an HNSW index allows millisecond cosine-similarity search across millions of chunks

We chose pgvector over a dedicated vector database (Pinecone, Weaviate, Qdrant) for simplicity: one less service to manage, and the metadata + vector queries join naturally in SQL.

## Choosing a VLM

We support four VLM configurations:

**OCI GenAI — Google Gemini 2.5 Pro/Flash**

The default. Frames are base64-encoded and sent in batches of 24 with a scenario-specific system prompt (*"You are an expert motorsport analyst..."*). Gemini describes what is happening in each batch — driver positions, overtaking manoeuvres, safety incidents, track conditions. Results are rich and nuanced.

**OCI GenAI — Cohere Command A Vision**

A drop-in alternative for Gemini. Cohere is particularly strong on structured outputs, useful when you need per-timestamp event tables rather than flowing prose.

**NVIDIA Cosmos-Reason2-8b**

Cosmos is different: instead of batches of images, it accepts the entire video file and reasons about temporal dynamics natively. You get a single holistic analysis that understands causality across time — *"The contact at 2:34 directly led to the spin visible at 2:37"* — rather than independent per-batch descriptions.

Cosmos is available in two flavours:
- **Public NIM** (via `integrate.api.nvidia.com`): instant access with an NVIDIA API key, no infrastructure
- **Local NIM**: deploy the model on your own GPU nodes for data sovereignty and lower per-call latency

The VLM is selected per-video at upload time and stored in the database so the frontend always shows which model produced the analysis.

## The Sports Analysis Scenario

When a user uploads a race video and selects the *Sports* scenario, the worker uses this system prompt:

> You are an expert motorsport analyst reviewing race footage. Describe what happens in this segment: identify vehicles, race numbers, positions, overtaking manoeuvres, incidents, driver behaviour, track conditions, and any notable events. Be specific about timing, positions, and what each driver does.

This prompt engineers the VLM to think like a race engineer, not a generic video captioning model. The resulting analysis mentions car numbers, gap estimates, track position, and incident descriptions — exactly what you need to ask follow-up questions or build a race report.

## Semantic Search in Practice

After analysis, a user can type:

> *"When did the lead change?"*

The API embeds this query with the same fastembed model used to embed the VLM output, then runs:

```sql
SELECT v.filename, c.chunk_text, c.segment_start,
       1 - (c.embedding <=> $1) AS similarity
FROM vss2_chunks c
JOIN vss2_videos v ON v.id = c.video_id
WHERE v.status = 'ready'
ORDER BY c.embedding <=> $1
LIMIT 20;
```

The `<=>` operator is pgvector's cosine distance. The HNSW index makes this sub-millisecond even at scale. Results come back ranked by semantic relevance — not keyword match — so *"lead change"* finds chunks that say *"Car 7 overtook car 12 on the back straight"* even though the phrase "lead change" never appears in the analysis.

If a synthesis model is configured, the top chunks are sent to an LLM with the question, and the API returns a synthesised narrative answer alongside the raw evidence chunks.

## The Race Report Generator

Beyond interactive search, VSS2 includes a standalone PDF report generator built with ReportLab. Given a video ID (or all analysed videos), it:

1. Fetches all analysis chunks from the DB
2. Identifies distinct heats/races using semantic matching
3. Extracts per-lap event timelines, driver standings, and incident summaries
4. Generates a branded PDF with tables, charts, and narrated summaries

The report is useful for post-event review, steward decisions, and sharing highlights with teams.

## What We Learned

**Keep the API stateless.** Storing uploads on NFS and job state in Postgres means any number of app pods can serve any request. We never had to think about session affinity.

**`SELECT FOR UPDATE SKIP LOCKED` is underrated.** It turns your existing Postgres into a reliable job queue with one line of SQL. For workloads that don't need RabbitMQ-style fanout or dead-letter queues, it is hard to beat.

**CPU is a good HPA signal for this workload.** ffmpeg and fastembed are predictably CPU-bound. When jobs arrive, CPU goes up. When the queue drains, CPU drops. The HPA reacts within 30 seconds. We never needed custom metrics.

**VLM prompt engineering matters a lot.** The difference between a generic prompt and a sports-specific one is the difference between *"cars are driving on a track"* and *"Car 12 makes an aggressive move into Turn 3, forcing Car 7 wide onto the kerb"*. For production, invest in scenario-specific prompts.

**Cosmos for temporal reasoning, Gemini for detail density.** Cosmos gives you causal, time-aware narratives. Gemini gives you frame-by-frame detail. For race analysis we often use both and merge the analyses.

## Deployment

The full project is open-source on GitHub. To deploy:

1. Configure your OCI GenAI credentials and OKE cluster details in `k8s/configmap.yaml`
2. Build and push the app and worker Docker images
3. `kubectl apply -f k8s/` — that is it

The K8s manifests include PostgreSQL with a pgvector sidecar, the FastAPI app behind NGINX Ingress, worker pods with the HPA, and all ConfigMaps/Secrets templates.

See the [README](../README.md) for the full quickstart.

---

*VSS2 is built on Oracle Cloud Infrastructure. The demo hardware is an OKE cluster with A10 GPU nodes for local Cosmos NIM inference.*
