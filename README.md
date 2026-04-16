# Athenaeum

Open source knowledge management pipeline for AI agents — append-only intake, tiered compilation, configurable schemas.

## Architecture

Athenaeum implements a novel approach to persistent AI agent memory:

- **Append-only intake** — safety through write constraints, not trust scores
- **Wikipedia-style footnote trust** — source entities build an emergent trust graph
- **Configurable observation filter** — a self-improving "what to remember" prompt
- **Three types of contradiction** — factual (fix), contextual (keep both), principled (revise axiom)
- **Four-tier compilation** — programmatic → fast LLM → capable LLM → human escalation

## Installation

```bash
pip install athenaeum
```

## Quick start

```bash
# Initialize a knowledge directory
athenaeum init

# Or specify a custom path
athenaeum init --path ~/my-knowledge
```

## Development

```bash
# Clone and install in development mode
git clone https://github.com/Kromatic-Innovation/athenaeum.git
cd athenaeum
pip install -e ".[dev]"

# Run tests
pytest tests/ -v

# Lint
ruff check src/ tests/
```

## License

Apache 2.0 — see [LICENSE](LICENSE) for details.
