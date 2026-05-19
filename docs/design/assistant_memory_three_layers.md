# Assistant Memory Three Layers

## Layer 1 — conversation_history

- SQLite local store
- TTL 7 days
- Used only for recent continuity
- Does not participate in FAISS recall

## Layer 2 — assistant_utterances

- Stored as low-trust memory with `source_type=assistant_generated_utterance`
- Confidence capped at `0.25`
- Queried only for reflexive prompts such as「我上次說」「你之前說」
- Must never auto-promote into verified facts

## Layer 3 — verified_facts

- Stored as `source_type=verified_fact`
- Promotion paths:
  - `user_confirmed`
  - `tri_sage_consensus`
  - `file_evidence`
- Audit trail written to `.runtime/verified_fact_audit.jsonl`

## Red Lines

- Layer 2 cannot auto-upgrade to Layer 3
- Any Layer 3 write must have an explicit audited path
