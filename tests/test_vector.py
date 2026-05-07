import os
import sys

# Add project root to path
sys.path.insert(0, os.path.abspath('.'))

from skills.documents.vector_pipeline import ingest_text_to_vector_memory

text = "This is a test document. " * 500
print("Testing ingest_text_to_vector_memory...")
res = ingest_text_to_vector_memory(
    kind="test",
    primary="test_primary",
    title="test_title",
    text=text,
    chunk_chars=50,
    overlap=0,
    max_chunks_total=10,
)
print("Result:")
print(res)
