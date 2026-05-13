# MAGI Non-Distributed Model Recommendations

Last updated: 2026-03-08

## Scope

This report reviews MAGI models that are currently referenced in the codebase, excluding the distributed-inference tier.

Excluded from this review:

- `qwen3:30b`
- `llama3.1:70b`
- `llama3.1:405b`
- `triumvirate-70b`
- other explicit distributed / 70B-class paths

Included here:

- local main reasoning models
- fast fallback models
- embedding models
- OCR / vision models
- speech-to-text models
- code generation / code-fix candidate models
- image generation fallbacks

## Executive Assessment

MAGI currently has a useful model spread, but the allocation is not tight enough.

The main issues are:

1. `taide-12b` is used too broadly across high-value reasoning, memory query expansion, councils, summaries, and some proxy flows.
2. OCR / vision routing is partially specialized, but some chains still put general VLMs ahead of OCR-specific models.
3. Speech and transcription naming is inconsistent: the code says "transcribe model", but actual STT is Whisper / mlx-whisper, while some text models are only post-processing.
4. The codegen / autofix candidate list is broader than the actual operational usage, so some model roles are stale.
5. Taiwan legal-language handling exists, but TAIDE is still underused as a post-edit and terminology guardrail.

The best next step is not "add more models".
The best next step is "sharpen model contracts by task".

## Model Inventory By Function

## 1. Core Local Reasoning

### `taide-12b`

Observed usage:

- local main / collab default in `api/orchestrator.py`
- local main / chat fallback in `api/tools_api.py`
- local generation in `skills/bridge/grounded_ai.py`
- council model in `skills/magi/local_council.py`
- memory query expansion in `skills/memory/mem_bridge.py`
- world intel reasoning default in `skills/worldmonitor-intel/action.py`
- nightly memory consolidation in `scripts/memory_consolidation.py`

Current fit:

- Strong for deeper reasoning
- Acceptable for structured summarization
- Overkill for light routing and helper substeps

Improvement recommendation:

- Keep `taide-12b` as the non-distributed "serious reasoning" model.
- Do not use it as the default for lightweight helper steps such as:
  - query expansion
  - intent classification
  - short summaries
  - routine routing confirmation
- Reserve it for:
  - legal analysis
  - high-stakes answer synthesis
  - council deliberation
  - skill generation fallback
  - final cleanup on complex long-context jobs

Concrete changes:

- Remove `taide-12b` from default memory query expansion; keep query expansion disabled or move it to a smaller model.
- Keep it as `MAGI_MAIN_MODEL`, but narrow where that env var is reused automatically.
- Add explicit "final_synthesis_model" vs "fast_helper_model" separation.

### `llama3.1:8b`

Observed usage:

- sub / fast model in `api/orchestrator.py`
- quick local fallback in `skills/bridge/melchior_client.py`
- intent classifier default in `skills/bridge/intention_classifier.py`
- local general / captcha / date extraction in `skills/bridge/inference_gateway.py`
- summary fast-local path in `skills/bridge/balthasar_bridge.py`
- crawler architect in `skills/law_firm/crawler_architect.py`

Current fit:

- Good latency model
- Reasonable general fallback
- Too generic for OCR-heavy extraction and some codegen tasks

Improvement recommendation:

- Keep `llama3.1:8b` as the system-wide fast fallback.
- Do not ask it to perform tasks that are actually OCR or VLM problems.
- Keep it for:
  - quick local chat
  - short fallback summaries
  - intent fallback
  - general assistant continuity when heavier models are busy

Concrete changes:

- Remove or reduce its role in `captcha` / `date_extract` if specialized OCR is available.
- Stop using it as a generic stand-in for all structured extraction.

### `qwen2.5:7b`

Observed usage:

- default Balthasar local model in `For_Balthasar_Setup/balthasar_agent_v2.py`

Current fit:

- Lightweight general chat model
- Fine for a separate Balthasar node
- Not clearly integrated into current mainline Casper-side flows

Improvement recommendation:

- If Balthasar remains a separate lightweight node, `qwen2.5:7b` is a reasonable local conversational model.
- If Balthasar is mostly council-only and summarization is already proxied through Casper, this model should either:
  - be demoted to node-local utility use only, or
  - be aligned with the same summary / language-policy stack as Casper

Concrete changes:

- Decide whether Balthasar is still an active independent runtime.
- If yes, give it a single clearly scoped job:
  - concise response drafting
  - low-cost dialogue
  - low-risk task explanation

### `cwchang/llama3-taide-lx-8b-chat-alpha1:latest`

Observed usage:

- `tc_review` in `skills/bridge/inference_gateway.py`
- classifier fallback in `skills/bridge/intention_classifier.py`
- quick local candidate in `skills/bridge/melchior_client.py`

Current fit:

- Very good role candidate for Taiwan Chinese style and terminology normalization
- Underused as a post-processing specialist

Improvement recommendation:

- Do not promote it to main reasoner.
- Promote it to mandatory post-edit pass for:
  - Taiwan legal Chinese polishing
  - traditional Chinese normalization
  - jurisdiction-specific wording review

Concrete changes:

- Add TAIDE post-pass after:
  - judgment summaries
  - legal analysis
  - translation to Traditional Chinese
- Use TAIDE to normalize wording, not to decide legal logic.

## 2. Memory / Embedding

### `nomic-embed-text`

Observed usage:

- default embedding model in `skills/memory/mem_bridge.py`
- local sync embedding in `skills/memory/keeper_sync.py`
- codebase RAG in `skills/memory/codebase_rag.py`

Current fit:

- Good operational baseline
- May be weak on Traditional Chinese legal retrieval compared with a multilingual embedding model

Improvement recommendation:

- Keep it for now as the stable baseline.
- Benchmark it before replacing.
- The benchmark corpus should include:
  - Traditional Chinese legal questions
  - statute article retrieval
  - case-note retrieval
  - mixed Chinese-English technical content

Concrete changes:

- Build a small eval set and compare retrieval hit rate before changing embeddings.
- Only replace after measuring:
  - top-1 statute retrieval
  - top-3 note recall
  - latency
  - embedding storage size

## 3. Summary / Translation / Reflection

### `gemma3:12b`

Observed usage:

- summary / translate / transcribe / reflection day model in `skills/bridge/inference_gateway.py`
- pdf-namer vision chain defaults in `skills/pdf-namer/vision_parser.py` and `skills/pdf-namer/action.py`

Current fit:

- Better suited to text summarization and moderate multimodal reasoning than to primary OCR
- Good middle-weight structured summarizer

Improvement recommendation:

- Keep `gemma3:12b` for:
  - summaries
  - reflection
  - lightweight translation drafts
- Remove it from the front of OCR-specific chains unless benchmark proves otherwise.

Concrete changes:

- In pdf naming / stamp-date extraction, do not try `gemma3:12b` before OCR-specific models.
- Use it after OCR as a cleanup / interpretation model if needed.

### `gemma3:27b`

Observed usage:

- summary / translate / transcribe / reflection night model in `skills/bridge/inference_gateway.py`
- pdf vision chain fallback in `skills/pdf-namer/vision_parser.py`

Current fit:

- Quality-oriented text model
- Probably too expensive for routine background jobs if local resources are tight

Improvement recommendation:

- Keep `gemma3:27b` only for quality windows such as:
  - nightly reflection
  - high-value summarization
  - difficult translation cleanup
- Do not let it become a silent default for batch OCR pipelines.

Concrete changes:

- Add explicit job classes that are allowed to use `gemma3:27b`.
- Make nightly / quality mode opt-in by task type, not just by time of day.

## 4. Vision / OCR / Document Understanding

### `minicpm-v:latest`

Observed usage:

- primary vision model in `skills/bridge/melchior_bridge.py`
- `vision` task in `skills/bridge/inference_gateway.py`
- local vision candidate chain in gateway
- pdf-namer vision chain fallback

Current fit:

- Good primary VLM for document screenshots and mixed visual reasoning
- Better default than `llava:7b` for practical document tasks

Improvement recommendation:

- Keep `minicpm-v:latest` as the default general VLM.
- Use it for:
  - image description
  - layout-aware document inspection
  - screenshot interpretation
  - nontrivial visual extraction after OCR

Concrete changes:

- Make it the primary for general vision.
- Keep OCR-only tasks separate from general vision tasks.

### `glm-ocr:latest`

Observed usage:

- OCR-specific hint in `skills/bridge/melchior_bridge.py`
- tools API OCR routing

Current fit:

- This is the most sensible model in the repo for strict OCR-style tasks

Improvement recommendation:

- Promote `glm-ocr:latest` to first choice for:
  - date stamp extraction
  - filing stamp reading
  - seal / receipt text transcription
  - dense printed text from scan crops

Concrete changes:

- Reorder OCR chains to:
  - `glm-ocr:latest`
  - `minicpm-v:latest`
  - `llava:7b`
- Use OCR prompts that demand literal transcription before interpretation.

### `llava:7b`

Observed usage:

- local fallback in `skills/bridge/melchior_bridge.py`
- local vision fallback in `skills/bridge/inference_gateway.py`
- pdf-namer safe final fallback

Current fit:

- Good emergency fallback
- Not ideal as primary OCR or primary document VLM

Improvement recommendation:

- Keep it as the "always available" last fallback.
- Do not promote it above `minicpm-v` or `glm-ocr` for document tasks.

Concrete changes:

- Keep `llava:7b` only at the tail of fallback chains.
- Add degraded-mode labels to outputs produced by `llava:7b`.

## 5. Speech To Text

### `mlx-whisper` family

Observed usage:

- claimed preferred local path in `skills/bridge/balthasar_bridge.py`

Current fit:

- Architecturally correct for Apple Silicon
- Operational visibility is poor because the repo does not clearly pin a concrete mlx-whisper model in this path

Improvement recommendation:

- Explicitly configure and log the local Whisper model variant.
- Right now the system describes the route, but the model contract is not explicit enough.

Concrete changes:

- Add env vars like:
  - `MAGI_MLX_WHISPER_MODEL`
  - `MAGI_MLX_WHISPER_LANGUAGE`
- Emit the selected model into logs and API responses.

### OpenAI Whisper CLI `medium`

Observed usage:

- default CLI STT model in `skills/bridge/balthasar_bridge.py`

Current fit:

- Sensible default for court / meeting audio quality
- Could be too heavy for routine quick jobs

Improvement recommendation:

- Keep `medium` as the default "quality balanced" fallback.
- Add explicit tiers:
  - `small` for fast rough drafts
  - `medium` for default operational use
  - a larger model only for manual high-accuracy reruns

Concrete changes:

- Introduce task-level STT tiers instead of one default for everything.
- Keep the model explicit in transcription output metadata.

Important note:

- The current "transcribe" model entries in `inference_gateway.py` are text-model choices, not actual STT engines.
- Rename that task class internally to something like `transcribe_postedit` if it is only post-processing transcript text.

## 6. Code Generation / Autofix / Skill Genesis

### `qwen2.5-coder:7b`

Observed usage:

- candidate in `skills/evolution/skill_genesis.py`
- candidate in `skills/management/code_autofix.py`

Current fit:

- Best role in this repo for lightweight code repair and patch drafting

Improvement recommendation:

- Promote it to the primary non-distributed code-fix model.
- Use it before generic chat models for:
  - syntax repair
  - function rewrites
  - targeted patch generation

Concrete changes:

- Make `qwen2.5-coder:7b` the first local codegen choice in autofix and skill generation.
- Keep `taide-12b` only for harder multi-file reasoning after the small code model fails.

### `mistral-nemo:12b`

Observed usage:

- preferred candidate in `skills/evolution/skill_genesis.py`
- legacy diagnostic call in `skills/legal/runner.py`

Current fit:

- Good general-purpose drafting model
- Not as code-specialized as a coder model

Improvement recommendation:

- Keep it as a fallback for:
  - parser repair
  - structured response drafting
  - explanation-heavy code repair
- Do not make it the first choice for precision code patching.

### `deepseek-r1:14b`

Observed usage:

- preferred candidate in `skills/evolution/skill_genesis.py`

Current fit:

- Better suited to harder reasoning than routine generation
- Likely overkill or too slow for ordinary skill scaffolding

Improvement recommendation:

- Use only for:
  - hard debugging
  - multi-step reasoning
  - failure analysis after smaller models fail

Concrete changes:

- Keep it out of the default codegen hot path.

### `phi3.5:3.8b`

Observed usage:

- safe candidate list in `skills/evolution/skill_genesis.py`

Current fit:

- Good small utility model candidate
- Not currently given a clear production role

Improvement recommendation:

- Reassign it to cheap helper tasks if installed:
  - intent fallback
  - short rewrite
  - rule extraction
  - metadata cleanup

Concrete changes:

- If you want an ultra-fast utility tier, use `phi3.5:3.8b` there instead of wasting `taide-12b`.

### `gemma2:9b`

Observed usage:

- safe candidate list in `skills/evolution/skill_genesis.py`

Current fit:

- Candidate only; not clearly operational in current flows

Improvement recommendation:

- Either benchmark and give it a role, or remove it from the active safe shortlist.
- Stale model lists create false confidence and unpredictable routing.

## 7. Image Generation

### `dall-e-3`

Observed usage:

- fallback image generation model in `skills/bridge/melchior_bridge.py`

Current fit:

- Fine as a cloud fallback
- Not aligned with strict local-first or legal-data-sensitive workflows

Improvement recommendation:

- Keep it as explicit opt-in fallback only.
- Never allow it to silently become the default for sensitive document-related flows.

Concrete changes:

- Require a clear env gate for cloud image generation.
- Always return provider + model metadata to the caller.

### `realisticVisionV51.safetensors`

Observed usage:

- appears in `scripts/ops/diagnose_melchior.py`

Current fit:

- Diagnostic / remote-image-stack reference, not clearly part of the main active local path in this repo

Improvement recommendation:

- Do not treat it as part of the core non-distributed MAGI model contract unless the real Melchior image server is versioned in-repo.
- If it is truly production, version the checkpoint choice in code or env, not only in ops scripts.

## Cross-Functional Recommendations

## A. Stop using one model family for too many roles

Recommended role split:

- `taide-12b`
  - final reasoning
  - legal synthesis
  - council
  - hard long-context analysis
- `llama3.1:8b`
  - fast fallback
  - quick local continuity
  - low-cost helper generation
- `cwchang/llama3-taide-lx-8b-chat-alpha1:latest`
  - Taiwan Chinese post-edit
  - terminology review
- `nomic-embed-text`
  - baseline embeddings until benchmark replacement
- `glm-ocr:latest`
  - literal OCR extraction
- `minicpm-v:latest`
  - general vision / doc understanding
- `llava:7b`
  - degraded last fallback
- `qwen2.5-coder:7b`
  - local code patching / autofix
- Whisper / mlx-whisper
  - actual STT

## B. Fix OCR chain ordering

Current repo behavior still leaves room for bad ordering in some paths.

Recommended default OCR order:

1. `glm-ocr:latest`
2. `minicpm-v:latest`
3. `llava:7b`

For pdf naming and receipt date extraction, do not put `gemma3` first.

## C. Make model selection observable

Every response path should emit:

- provider
- model
- route
- degraded flag

Without this, tuning becomes guesswork.

## D. Separate STT from transcript cleanup

Right now "transcribe" is overloaded.

Split into:

- `speech_to_text_model`
- `transcript_cleanup_model`
- `speaker_diarization_model` if added later

## E. Benchmark instead of guessing

Before replacing any model, build three small regression sets:

1. Taiwan legal text set
2. OCR / filing-stamp set
3. summarization / translation set

Measure:

- correctness
- latency
- failure rate
- degraded fallback frequency

## Recommended Immediate Changes

Do these first:

1. Narrow `taide-12b` to high-value reasoning only.
2. Reorder OCR chains so `glm-ocr` comes before `gemma3` and before generic VLM interpretation.
3. Promote TAIDE to a post-edit reviewer for Traditional Chinese legal output.
4. Make `qwen2.5-coder:7b` the primary local code-fix model.
5. Add explicit STT model metadata and stop calling text cleanup models "transcribe models".
6. Benchmark `nomic-embed-text` on a Traditional Chinese legal retrieval set before touching RAG embeddings.

## Bottom Line

MAGI does not mainly need more models.

MAGI needs:

- cleaner model contracts
- stricter task-to-model routing
- better OCR specialization
- clearer Taiwan legal language post-processing
- explicit observability for every model hop

If you only implement one routing cleanup, make it this:

- `glm-ocr` for literal OCR
- `minicpm-v` for vision understanding
- `llava:7b` for degraded fallback
- `llama3.1:8b` for fast text fallback
- `taide-12b` for final reasoning only
