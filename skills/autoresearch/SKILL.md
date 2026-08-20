---
name: autoresearch
description: Autonomous machine-learning research loop for GPU hosts. It prepares data, runs bounded training experiments, records val_bpb results, and reports experiment status.
compatibility: Requires Python, uv, CUDA-capable NVIDIA GPU, and a prepared GPU host.
metadata:
  role: research
  entry: skills/autoresearch/action.py
---

# autoresearch

`autoresearch` runs small, time-boxed machine-learning experiments on a configured GPU host. MAGI uses it to prepare the dataset, start or stop autonomous experiment loops, inspect status, and collect result tables.

## Commands

```bash
python skills/autoresearch/action.py --task "autoresearch setup <host>"
python skills/autoresearch/action.py --task "autoresearch run <host> [--tag TAG]"
python skills/autoresearch/action.py --task "autoresearch status [host]"
python skills/autoresearch/action.py --task "autoresearch results [host]"
python skills/autoresearch/action.py --task "autoresearch stop <host>"
```

## Notes

- `prepare.py` handles data preparation and tokenizer setup.
- `train.py` is the experiment file that autonomous agents may iterate on.
- `program.md` contains the research loop instructions.
- Results are compared by validation bits per byte (`val_bpb`), where lower is better.
