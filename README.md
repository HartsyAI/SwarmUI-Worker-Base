# SwarmUI Worker Base

A provider-neutral Docker image that runs [SwarmUI](https://github.com/mcmonkeyprojects/SwarmUI) as a secure, self-releasing cloud GPU worker. It is the foundation for Hartsy's provider workers:

| Image | Provider | Repo |
|---|---|---|
| `kalebbroo/swarmui-worker-base` | none (this repo) | [SwarmUI-Worker-Base](https://github.com/HartsyAI/SwarmUI-Worker-Base) |
| `kalebbroo/swarmui-worker-runpod` | RunPod Serverless and Pods | [RunPod-Worker-SwarmUI](https://github.com/HartsyAI/RunPod-Worker-SwarmUI) |
| `kalebbroo/swarmui-worker-vast` | Vast.ai Serverless and Instances | [Vast-Worker-SwarmUI](https://github.com/HartsyAI/Vast-Worker-SwarmUI) |

The workers are designed for the [Cloud Backends](https://github.com/HartsyAI/SwarmUI-CloudBackends) SwarmUI extension, which starts them on demand, sends generations to them, and lets them shut down when idle.

## What the image does

- **Runs SwarmUI on loopback only.** SwarmUI trusts every local caller as its admin, so it never listens on a public interface.
- **Puts an authenticated gateway in front of it.** Port `7801` is the only public port. Every HTTP and WebSocket request must carry `Authorization: Bearer <token>`. Requests without it get `401`. Between serverless leases no token is valid at all.
- **Releases itself when idle.** The supervisor watches SwarmUI's own activity counters (`/API/GetGlobalStatus`). It releases a lease once no generation has run for the idle window, so provider billing stops.
- **Starts clean for every lease.** A new lease gets a new token. Ending a lease closes every open connection and wipes outputs, so the next holder of a warm worker cannot reach the previous one's session or images.
- **Keeps shared model storage safe.** When models live on a volume shared by several workers, each worker reads them through a private tree of symlinks, so SwarmUI's per-folder metadata databases are never written to shared storage.
- **Starts fast and reproducibly.** SwarmUI, the backend, and every Python dependency the backend installs on first run are baked in at pinned versions. Auto-update is off. A cold worker downloads nothing.

## Tags

Images are published to Docker Hub as `kalebbroo/swarmui-worker-base:<version>-<backend>`:

- `<backend>` is `comfyui` (the full ComfyUI backend) or `hartsyinference` (Hartsy's pure C# backend: a smaller image with a faster cold start).
- `<version>` is a release such as `1.0.0`. `edge-<backend>` tracks `main` and is not for production.

Always pin a release version in production.

## Running it

Standalone mode is for a rented machine (a pod or an instance) that stays up until you stop it. It requires a fixed token:

```bash
docker run --gpus all -p 7801:7801 \
  -e SWARMUI_WORKER_TOKEN="$(openssl rand -base64 48)" \
  -e SWARMUI_MODEL_ROOT=/workspace/models \
  -v /path/to/models:/workspace/models \
  kalebbroo/swarmui-worker-base:1.0.0-comfyui
```

Connect a SwarmUI to it with a **Swarm API** backend, whose address is `http://<host>:7801` and whose `AuthorizationHeader` is `Bearer <token>`.

Serverless providers do not use standalone mode. Their images drive leases through the supervisor API (see [Building a provider image](#building-a-provider-image)).

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `SWARMUI_WORKER_TOKEN` | *(none)* | A fixed token, at least 32 characters. Required in standalone mode. Serverless adapters leave it unset, so each lease gets its own token. |
| `SWARMUI_MODEL_ROOT` | the image's `Models` folder | Where the models are, e.g. a mounted volume. |
| `SWARMUI_SHADOW_MODELS` | `true` | Read models through a private symlink tree (see above). |
| `SWARMUI_IDLE_SECONDS` | `120` | Release a lease after this long with no generation activity. |
| `SWARMUI_STARTUP_GRACE_SECONDS` | `600` | How long a new lease may wait for its first generation. |
| `SWARMUI_MAX_LEASE_SECONDS` | `3600` | Longest lease (0 means no limit). Never interrupts a running generation; set the provider's execution timeout above it. |
| `SWARMUI_PUBLIC_PORT` | `7801` | The gateway port, and the only port to expose. |
| `SWARMUI_INTERNAL_PORT` | `7810` | SwarmUI's loopback port. |
| `SWARMUI_WORKER_ALLOW_URL_LOGIN` | `false` | Allow a browser to log in once with `?worker_token=...`, which sets an HttpOnly cookie. Off by default, because tokens in URLs can end up in logs and browser history. |
| `SWARMUI_BOOT_TIMEOUT` | `900` | Longest SwarmUI may take to start. |
| `SWARMUI_LOG_JSON` / `SWARMUI_LOG_LEVEL` | `true` / `INFO` | JSON log lines for collectors, and the log level. Tokens are always redacted. |

## Building

```bash
docker build --build-arg BACKEND=comfyui -t swarmui-worker-base:local-comfyui .
docker build --build-arg BACKEND=hartsyinference -t swarmui-worker-base:local-hartsyinference .
```

| Build argument | Default | Meaning |
|---|---|---|
| `BACKEND` | `comfyui` | `comfyui` or `hartsyinference`. |
| `SWARMUI_REF` | pinned commit | SwarmUI commit to build. |
| `COMFYUI_REF` | pinned commit | ComfyUI commit (`comfyui` only). |
| `HARTSYINFERENCE_REF` | pinned commit | HartsyInference extension commit (`hartsyinference` only). |
| `TORCH_INDEX_URL` | CUDA 12.8 wheels | PyTorch wheel index (`comfyui` only). CUDA 12.8 needs host driver 570 or newer. |
| `BAKE_MODEL_URL` / `BAKE_MODEL_SHA256` | *(none)* | Bake one model into the image, for providers with no persistent storage. The SHA-256 is required. |
| `BAKE_MODEL_SUBDIR` | `Stable-Diffusion` | Model folder the baked model goes into. |

The build boots SwarmUI once, on CPU, so SwarmUI installs its backend's dependencies itself. It fails the build if the backend doesn't reach `running`, or if SwarmUI tries to rebuild an extension (the runtime image has no .NET SDK).

## Building a provider image

A provider image starts `FROM kalebbroo/swarmui-worker-base:<version>-<backend>`, adds its provider SDK to `/opt/worker/venv`, and drives leases through `BackgroundSupervisor`:

```python
from swarmui_worker.config import WorkerConfig
from swarmui_worker.supervisor import BackgroundSupervisor

supervisor = BackgroundSupervisor(WorkerConfig.from_env())
supervisor.start()                              # blocking: SwarmUI up, gateway listening

async def serve_one_lease():
    lease = await supervisor.begin_lease()      # fresh token for this lease
    # ... hand lease.token and the public URL to the client via the provider's channel ...
    reason = await supervisor.wait_for_release()  # returns once idle / never used / capped
    await supervisor.end_lease()                # revoke token, drop connections, wipe outputs
```

The supervisor runs on its own thread and event loop, so it keeps serving between the provider SDK's jobs, whatever event loop that SDK uses.

## Development

```bash
pip install "aiohttp>=3.10,<4" "pytest>=8"
python -m pytest
bash tests/smoke/smoke.sh <image> <backend>   # CPU smoke test against a built image
```

## License

MIT, see [LICENSE](LICENSE).
