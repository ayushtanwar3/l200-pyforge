# PyForge — Self-Healing CLI Tool & Script Studio

PyForge is an autonomous multi-agent Python CLI tool studio that designs, codes, statically checks, tests, and self-heals single-file Python CLI utilities using standard libraries.

## Architecture (4 Layers)

1. **Layer 1 — Observability & Safety (`src/observability/`)**:
   - `PIIScrubber`: Compiled regex sanitization for API keys (`sk-...`, `AIza...`, `ghp_...`, `AKIA...`), Bearer tokens, private keys, JWTs, passwords, emails, credit cards, and IP addresses.
   - `StructuredLogger`: Intent vs. Outcome JSON logger integrated with OpenTelemetry trace/span IDs and automatic `PIIScrubber` sanitization.
   - `TelemetryManager`: OpenTelemetry `pyforge.tracer` and `pyforge.meter` spans & metrics.
   - `ASTSecurityVisitor`: Static AST security validator blocking `eval`, `exec`, `compile`, `__import__`, `globals`, `locals`, unauthorized network libraries (`socket`, `urllib`, `requests`, `httpx`, `aiohttp`, `ftplib`), and arbitrary shells (`os.system`, `pty`, `subprocess.Popen(shell=True)`).

2. **Layer 2 — Context & Memory (`src/storage/`, `src/agents/constitution.py`)**:
   - `SQLiteDatabase`: Persistent storage in WAL mode (`sessions`, `messages`, `generated_scripts`) with an `asyncio.Queue` background write worker.
   - `SlidingWindowMemory`: Sliding-window conversation buffer that compacts older turns into a persistent summary via `gemini-2.5-flash`.
   - `AgentConstitution`: Immutable single-file stdlib-only and safety boundaries.

3. **Layer 3 — Orchestration & Agents (`src/agents/`)**:
   - `ArchitectAgent` (`gemini-2.5-pro`): Designs `ScriptSpecification` and deterministic `TestVector` suites.
   - `CoderAgent` (`gemini-2.5-flash`): Writes and self-heals standalone `argparse`-based Python CLI scripts.
   - `PyForgeOrchestrator`: Coordinates the end-to-end pipeline with up to 3 automated self-healing iterations.

4. **Layer 4 — Tool Execution (`src/tools/`)**:
   - `SandboxTestRunner`: Runs generated scripts in an isolated temporary directory with a strict 5.0s timeout and stripped sensitive environment variables.
   - `HumanInTheLoopGate`: Rich terminal UI displaying syntax-highlighted code and test outcomes before installation into `./bin/<tool_name>.py` with `chmod +x`.

## Usage

```bash
# Generate a new CLI tool interactively
.venv/bin/python3 -m src.cli new "Build a CLI utility that counts lines, words, and characters in stdin"

# List generated CLI tools stored in SQLite
.venv/bin/python3 -m src.cli list

# Re-run AST security and sandbox test vectors on an existing utility
.venv/bin/python3 -m src.cli test <script_name>
```

## Running Tests

```bash
.venv/bin/pytest -v
```
