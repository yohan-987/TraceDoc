# TraceDocs

AI-assisted document forensics and contextual intelligence (hackathon prototype, built from scratch).
Fictional documents only. Findings are observations or potential anomalies, never fraud verdicts.

See `ARCHITECTURE.md` (design), `docs/INTEGRATION_STATUS.md` (what works, verified) and `docs/LIMITATIONS.md`. Full README (setup, licenses,
AI-assistance disclosure) is written in batch X3.

| Folder | Purpose |
| --- | --- |
| `contracts/` | Shared types, JSON Schemas, pydantic mirror, fixtures, OpenAPI. Source of truth. |
| `backend/` | Node + Express API and the reasoning engine (`backend/src/engine/`) |
| `ocr-service/` | Python + FastAPI analysis service (OCR, extraction, forensic analyzers) |
| `frontend/` | React + Vite investigation UI |
| `samples/` | Fictional sample documents, ground truth |
| `eval/` | Evaluation scripts and results |
| `docs/` | Limitations, integration status, demo script |

## Acknowledgements

- Design ideas for risk fusion (noisy-OR with a cap on weak evidence), graceful degradation of optional layers, an access log, a retention policy endpoint and cross-record correlation come from the team's earlier email-forensics project, TraceMail. No TraceMail code is used here; everything in this repository was written for TraceDocs.
- AI coding assistants (Claude, from Anthropic) were used during development. A full disclosure is written in batch X3.
