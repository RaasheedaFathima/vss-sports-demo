# VSS2 — Video Search & Summarization

> **AI-powered video analysis platform on Oracle Cloud Infrastructure**

VSS2 is a production-grade, cloud-native application that uses multimodal AI models to analyse video footage, extract semantic meaning frame-by-frame, and answer natural-language questions about the content. It was built to demonstrate how sports race footage — and any other domain — can be indexed, searched, and summarised using modern Vision-Language Models (VLMs) and Kubernetes-native scaling.

---

## Demo: Sports Race Analysis

Upload a race video. In seconds, VSS2:

1. Extracts one frame per second using ffmpeg
2. Sends batches of frames to a multimodal VLM (Gemini, Cohere, or NVIDIA Cosmos)
3. Stores a rich natural-language analysis in PostgreSQL with pgvector embeddings
4. Lets you ask questions like *"Who crossed the finish line first?"* or *"At what timestamp did the lead change?"*
5. Generates a professional PDF race report with per-heat results and timelines

---

## Architecture

```
┌────────────────────────────────────────────────────────────┐
│                    Kubernetes Cluster (OKE)                │
│                                                            │
│  ┌──────────────┐    ┌─────────────────────────────────┐  │
│  │  NGINX Ingress│───▶│         FastAPI App             │  │
│  └──────────────┘    │  • Upload video                  │  │
│                      │  • Poll analysis status          │  │
│                      │  • Semantic search               │  │
│                      │  • Video streaming               │  │
│                      └──────────┬──────────────────────-┘  │
│                                 │ DB job queue              │
│                      ┌──────────▼──────────────────────┐   │
│                      │   Worker Pods (HPA: 1–5)         │   │
│                      │  • Claim job (FOR UPDATE SKIP    │   │
│                      │    LOCKED — parallel-safe)       │   │
│                      │  • Extract 1fps frames (ffmpeg)  │   │
│                      │  • Call VLM for analysis         │   │
│                      │  • Embed chunks (fastembed)      │   │
│                      │  • Store in pgvector             │   │
│                      └──────────┬──────────────────────-┘   │
│                                 │                            │
│                      ┌──────────▼──────────────────────┐    │
│                      │   PostgreSQL + pgvector          │    │
│                      │   (video metadata + embeddings)  │    │
│                      └─────────────────────────────────┘    │
│                                                              │
└──────────────────────────────────────────────────────────────┘
         │                   │                    │
   OCI GenAI           NVIDIA NIM         NVIDIA NIM
  (Gemini, Cohere)    (Public API)       (Local / on-prem)
```

See [docs/architecture.md](docs/architecture.md) for the full Mermaid diagram and component details.

---

## Supported VLM Backends

| Model | Provider | How it works |
|-------|----------|--------------|
| Google Gemini 2.5 Pro | OCI GenAI | Batched 1fps frames → OCI Inference API |
| Google Gemini 2.5 Flash | OCI GenAI | Batched 1fps frames → OCI Inference API |
| Cohere Command A Vision | OCI GenAI | Batched 1fps frames → OCI Inference API |
| NVIDIA Cosmos-Reason2-8b | Public NIM | Entire video → `integrate.api.nvidia.com` |
| NVIDIA Cosmos-Reason2-8b | Local NIM | Entire video → self-hosted NIM endpoint |

---

## Repository Structure

```
vss-sports-demo/
├── app/                    FastAPI backend service
│   ├── main.py             API + DB + semantic search
│   ├── requirements.txt
│   └── Dockerfile
├── worker/                 Background video analysis worker
│   ├── worker.py           Polls DB, runs VLM, stores embeddings
│   ├── requirements.txt
│   └── Dockerfile
├── frontend/               Single-page web UI
│   └── index.html
├── reports/                Offline PDF report generator
│   └── generate_race_report.py
├── k8s/                    Kubernetes manifests
│   ├── configmap.yaml      Non-secret configuration
│   ├── secret.yaml.example Secret template (do not commit filled in)
│   ├── postgres.yaml       PostgreSQL + pgvector
│   ├── app-deployment.yaml FastAPI app + Service + Ingress
│   ├── worker-deployment.yaml Worker pods
│   └── hpa.yaml            Horizontal Pod Autoscaler
└── docs/
    ├── architecture.md     Component diagram and design notes
    └── blog.md             Technical blog post
```

---

## Prerequisites

- **OCI account** with GenAI enabled (Phoenix region recommended)
- **OKE cluster** with NGINX Ingress Controller installed
- **OCI Instance Principal** policy so pods can call OCI APIs without API keys:
  ```
  Allow dynamic-group <your-node-pool-dg> to use generative-ai-family in compartment <compartment>
  ```
- **Shared NFS storage** (OCI FSS PVC) mounted at `/mnt/fss` for video files and the fastembed model cache
- **Docker** and `kubectl` configured to reach your cluster
- *(Optional)* NVIDIA NIM API key from [build.nvidia.com](https://build.nvidia.com) for Cosmos-Reason2 public NIM

---

## Quick Start

### 1. Configure

Copy the secret template and fill in your credentials:

```bash
cp k8s/secret.yaml.example k8s/secret.yaml
# Edit k8s/secret.yaml — add your PG_PASSWORD and optionally NVIDIA_API_KEY
```

Edit `k8s/configmap.yaml` and replace all `<YOUR_...>` placeholders with your OCI tenancy details and model OCIDs.

### 2. Build and push images

```bash
# App image
docker build -t <YOUR_REGISTRY>/vss2-app:latest ./app
docker push <YOUR_REGISTRY>/vss2-app:latest

# Worker image
docker build -t <YOUR_REGISTRY>/vss2-worker:latest ./worker
docker push <YOUR_REGISTRY>/vss2-worker:latest
```

### 3. Deploy to Kubernetes

```bash
# Secrets and config
kubectl apply -f k8s/secret.yaml
kubectl apply -f k8s/configmap.yaml

# Database
kubectl apply -f k8s/postgres.yaml

# Application
kubectl apply -f k8s/app-deployment.yaml
kubectl apply -f k8s/worker-deployment.yaml
kubectl apply -f k8s/hpa.yaml

# Wait for pods
kubectl get pods -w
```

### 4. Open the UI

Navigate to the Ingress address shown by:
```bash
kubectl get ingress vss2-app-ingress
```

---

## Generating Race Reports

After analysing race footage, generate a professional PDF report:

```bash
# List available videos in the DB
python3 reports/generate_race_report.py --list

# Report for a specific video
python3 reports/generate_race_report.py race_final.mp4

# Report for all analysed videos
python3 reports/generate_race_report.py

# Report for a specific race/heat number within the video
python3 reports/generate_race_report.py race_final.mp4 --race 2
```

Requires `reportlab`: `pip install reportlab`

---

## How Parallel Processing Works

Each worker pod runs an infinite polling loop:

1. `SELECT ... FOR UPDATE SKIP LOCKED` atomically claims one video from the queue
2. Extracts frames with ffmpeg (CPU-bound)
3. Calls the VLM API in batches
4. Embeds analysis text with fastembed (CPU-bound)
5. Writes results to PostgreSQL
6. Returns to polling

The HPA watches CPU utilisation on worker pods. When videos are being processed, CPU spikes and the HPA adds replicas (up to 5) automatically. No two workers ever claim the same job.

---

## Development

Run the backend locally against an existing Postgres:

```bash
cd app
pip install -r requirements.txt
export PG_HOST=localhost PG_DB=vss2_db PG_USER=vss2_user PG_PASSWORD=...
export COMPARTMENT_ID=... GEMINI_25_PRO_OCID=... # etc.
uvicorn main:app --reload
```

---

## License

Apache 2.0 — see [LICENSE](LICENSE).
