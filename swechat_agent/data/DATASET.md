# Dataset location

## SWE-chat

- **HuggingFace repo ID:** `SALT-NLP/SWE-chat`
- **Version / snapshot:** latest (as of 2026-05-02)
- **Date fetched:** (fill in after download)
- **Local path:** downloaded at runtime via `load_dataset("SALT-NLP/SWE-chat", ...)` (no local copy)
- **Size on disk:** ~12.8 GB total (6 tables as parquet)
- **License:** ODC-By (Open Data Commons Attribution License)
- **Access:** Gated — requires HuggingFace account with approved access request

## Tables used by this workspace

| Table | Config name | Rows | Used for |
|---|---|---|---|
| `sessions` | `sessions` | 5,851 | Session metadata: persona, agent_percentage |
| `conversations` | `conversations` | 2,692,480 | User prompts with pushback labels |

## How to authenticate

```bash
# Option 1: set env var
export HF_TOKEN=hf_...

# Option 2: interactive login
huggingface-cli login

# Then run:
python scripts/swechat_extract.py
```

## Citation

Baumann et al. (2026). SWE-chat: Real-World AI Coding Sessions in the Wild.
arXiv:2604.20779. https://arxiv.org/pdf/2604.20779
