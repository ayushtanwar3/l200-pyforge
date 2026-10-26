# PyForge — Self-Healing Multi-Agent CLI Tool & Script Studio

PyForge is an autonomous multi-agent Python CLI tool studio built on Google Agent Development Kit (ADK) patterns, Gemini (`gemini-2.5-pro` as Architect, `gemini-2.5-flash` as Coder), OpenTelemetry, and SQLite. It designs, codes, statically checks, tests, and self-heals single-file Python CLI utilities using standard libraries.

---

## 4-Layer AgentOps Architecture

1. **Layer 1 — Observability & Safety (`src/observability/`)**:
   - **`PIIScrubber` (`pii_scrubber.py`)**: Compiled regex sanitization for API keys (`sk-...`, `AIza...`, `ghp_...`, `AKIA...`), Bearer tokens, PEM private keys, JWTs, passwords, emails, credit cards, and IPv4 addresses.
   - **`StructuredLogger` (`structured_logging.py`)**: Emits structured JSON logs (`timestamp`, `level`, `trace_id`, `span_id`, `layer`, `intent`, `outcome`, `metadata`) with automatic `PIIScrubber` redaction.
   - **`TelemetryManager` (`telemetry.py`)**: OpenTelemetry distributed tracing (`pyforge.tracer`) and metrics (`pyforge.meter`) across agent routing, AST security checks, and sandbox execution.
   - **`ASTSecurityVisitor` (`ast_guardrail.py`)**: Static AST guardrail blocking `eval`, `exec`, `compile`, `__import__`, `globals`, `locals`, unauthorized network libraries (`socket`, `urllib`, `requests`, `httpx`, `aiohttp`, `ftplib`), and arbitrary shells (`os.system`, `pty`, `subprocess.Popen(shell=True)`).

2. **Layer 2 — Context & Memory (`src/storage/`, `src/agents/constitution.py`)**:
   - **`SQLiteDatabase` (`database.py`)**: Persistent storage in WAL mode (`sessions`, `messages`, `generated_scripts`) backed by an asynchronous `asyncio.Queue` write worker.
   - **`SlidingWindowMemory` (`memory.py`)**: Sliding-window conversation buffer that compacts older turns asynchronously in the background (`asyncio.Queue` / `asyncio.Task` + `client.aio.models.generate_content` with `gemini-2.5-flash`) to prevent UI blocking.
   - **`AgentConstitution` (`constitution.py`)**: Immutable system prompt rules enforcing single-file stdlib-only scripts and preventing destructive filesystem commands (`rm -rf /`, `shutil.rmtree('/')`).

3. **Layer 3 — Orchestration & Agents (`src/agents/`)**:
   - **`StrategicModelRouter` (`model_router.py`)**: Routes complex planning tasks (`TaskComplexity.PLANNING`) to `gemini-2.5-pro` and fast execution/healing/summarization tasks (`TaskComplexity.FAST_EXECUTION`) to `gemini-2.5-flash`, with automatic 503/404 model fallback.
   - **`ArchitectAgent` (`architect.py`)**: Uses `gemini-2.5-pro` (`temperature=0.2`) and strict Gemini `RESPONSE_SCHEMA` to generate `ScriptSpecification` and deterministic `TestVector` suites.
   - **`CoderAgent` (`coder.py`)**: Uses `gemini-2.5-flash` (`temperature=0.2`) to write and self-heal standalone `argparse`-based Python CLI scripts.
   - **`GuardrailPolicyPlugin` & `PyForgeOrchestrator` (`orchestrator.py`)**: Coordinates the Sequential + Coordinator loop with up to **3 automated self-healing iterations** before human approval.

4. **Layer 4 — Tool Execution (`src/tools/`)**:
   - **`SandboxTestRunner` (`sandbox_runner.py`)**: Executes generated scripts in an isolated temporary directory via `subprocess.run` with a strict `5.0s` timeout and stripped sensitive environment variables.
   - **`HumanInTheLoopGate` (`hitl_gate.py`)**: Interactive confirmation hook (`[Approve & Install / Edit Request / Abort]`) displaying syntax-highlighted code and test vector outcomes before writing `./bin/<tool_name>.py` and applying `chmod +x`.

---

## Quickstart & CLI Usage

```bash
# 1. Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Configure API Key (via environment variable or Google Cloud Secret Manager)
export GOOGLE_API_KEY="your-gemini-api-key"

# 3. Generate a new self-healing CLI utility interactively
pyforge new "Build a CLI utility that counts lines, words, and characters in stdin"

# 4. List generated CLI utilities stored in SQLite
pyforge list

# 5. Re-run AST security and sandbox test vectors on an existing utility
pyforge test json_formatter
```

### ADK & Agent CLI Workflow (`adk` / `agent-starter-pack`)

PyForge supports standard Google Agent Development Kit (ADK) and `agent-starter-pack` CLI workflows for local execution, evaluation, and Cloud Run deployment:

```bash
# Run interactive agent session via PyForge / ADK CLI
pyforge new "Create a CSV column summarizer CLI tool"
# Or with ADK CLI:
adk run src/agents

# Run static unit tests & Golden Dataset evaluation regression harness
pytest -v
# Or with ADK evaluation CLI against the golden dataset:
adk eval src/agents configs/golden_dataset.json

# Deploy agent service to Google Cloud Run via ADK CLI
adk deploy cloud_run --project=$GOOGLE_CLOUD_PROJECT --region=us-central1 src/agents
```

---

## Infrastructure as Code (Terraform) & Secret Manager

All cloud resources (Cloud Run v2 service, Vertex AI & Gemini APIs, least-privilege Agent Service Account, Cloud Storage state bucket, Artifact Registry, and **Google Cloud Secret Manager** injection for `GOOGLE_API_KEY`) are declaratively provisioned in [`infra/`](file:///Users/ayushtanwar/Documents/l200-pyforge/infra/main.tf):

```bash
cd infra
terraform init
terraform plan -var="project_id=your-gcp-project-id"
terraform apply -var="project_id=your-gcp-project-id"
```

At runtime, [`SecretManagerResolver`](file:///Users/ayushtanwar/Documents/l200-pyforge/src/config.py#L19-L51) in `src/config.py` resolves `GOOGLE_API_KEY` from injected environment variables or directly from Google Cloud Secret Manager (`projects/$GOOGLE_CLOUD_PROJECT/secrets/pyforge-gemini-api-key/versions/latest`) with zero hardcoded secrets.
