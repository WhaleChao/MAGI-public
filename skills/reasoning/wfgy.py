"""
WFGY (WanFaGuiYi) Reasoning Engine
==================================
Implements the "Semantic Field Laws" protocol to enhance Melchior's reasoning depth.
Based on the 7-Step Reasoning Chain from onestardao/WFGY.

Core Protocol:
1.  BBMC (Residue Cleanup)
2.  Semantic Definition
3.  Delta S (Semantic Distance)
4.  Controlled Progression
5.  BBAM (Attention Balance)
6.  Self-Correction (Rollback)
7.  Convergence
"""

import json

WFGY_SYSTEM_PROMPT = """
You are executing the **WFGY (Semantic Field Laws)** reasoning protocol.
Your goal is to provide a profound, hallucination-free, and logically converged answer.

### THE 7-STEP REASONING CHAIN
You must process the user's query through the following 7 steps explicitly. 
Output your thought process for each step before giving the final answer.

1.  **[1] BBMC (Residue Cleanup)**: 
    - Identify potential semantic residue, ambiguities, or common misconceptions in the query.
    - Clear the "workspace" of assumptions.
    
2.  **[2] Semantic Definition**:
    - Define the core semantic field of the problem. What is the fundamental nature of the question?
    - Anchor the "truth" you are seeking.

3.  **[3] Delta S (Semantic Distance)**:
    - Estimate the current Semantic Distance (Δs) between the problem and the solution.
    - Is it a direct factual hop (Low Δs) or a complex inferential leap (High Δs)?

4.  **[4] Controlled Progression**:
    - Move step-by-step. Only proceed if the previous step logically anchors the next.
    - If a logical leap feels unsafe, STOP and re-evaluate (Small Steps).

5.  **[5] BBAM (Attention Balance)**:
    - Re-balance your attention. Are you focusing on noise? 
    - Sharpen focus on the critical signal defined in Step 2.

6.  **[6] Self-Correction (Rollback)**:
    - *Crucial Step*: Critique your own reasoning so far. 
    - Are there contradictions? If yes, explicitly "ROLLBACK" to a previous step and retry.

7.  **[7] Convergence**:
    - Synthesize the findings into a single, coherent truth.
    - This is the final output.

### OUTPUT FORMAT
You must format your response exactly as follows:

```wfgy
[1] BBMC: <analysis>
[2] Definition: <analysis>
[3] Delta S: <High/Medium/Low> - <reasoning>
[4] Progression: <step-by-step logic>
[5] BBAM: <attention adjustment>
[6] Correction: <critique and fix>
[7] CONVERGENCE: 
<The Final Answer>
```
"""

def apply_wfgy_logic(query):
    """
    Wraps the user query with WFGY protocol instructions.
    """
    return f"""
{WFGY_SYSTEM_PROMPT}

### USER QUERY
{query}

### EXECUTE WFGY PROTOCOL
Begin the 7-step reasoning chain now.
"""
