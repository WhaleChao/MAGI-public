"""
api.osc -- OSC Web API utility package.

Re-exports all utility functions from api.osc.utils so that existing code
can do ``from api.osc.utils import _osc_exec`` or
``from api.osc import _osc_exec`` interchangeably.
"""

from api.osc.utils import (  # noqa: F401
    # Database config & connection
    OSC_WEB_DB_CONFIG,
    _load_code_db_profile,
    _resolve_osc_web_db_config,
    _osc_web_db_candidates,
    _osc_web_connect,
    _osc_exec,
    # JSON / row helpers
    _osc_json_value,
    _osc_row_json,
    _osc_parse_dt,
    # Path / folder utilities
    _osc_norm_path,
    _osc_local_path_candidates,
    _osc_allowed_local_roots,
    _osc_is_safe_local_path,
    _osc_resolve_existing_local_path,
    _osc_relpath_under,
    _osc_human_size,
    _osc_folder_entries,
    # File reading utilities
    _OSC_TEXT_EXTENSIONS,
    _osc_is_editable_text_path,
    _osc_read_text_file,
    _osc_smb_candidates,
    _osc_path_to_smb,
    _osc_try_open_path,
    _osc_case_folder_from_doc_path,
    _osc_guess_case_folder,
    # Fulltext / HTML utilities
    _osc_strip_html_to_text,
    _osc_fetch_url_text,
    _osc_lookup_fulltext_fallback,
    # Skill execution utilities
    _osc_run_skill,
    _osc_skill_json_task,
    _osc_parse_skill_output,
    # Parsing and normalization utilities
    _osc_title_norm,
    _OSC_JUDICIAL_COURT_SEARCH_LABELS,
    _OSC_JUDICIAL_COURT_ALIASES,
    _osc_unique_keep_order,
    _osc_normalize_court_name,
    _osc_extract_court_names,
    _osc_normalize_case_word,
    _osc_parse_structured_case_spec,
    _osc_extract_case_markers,
    _osc_load_judicial_search_results,
    _osc_pick_exact_judicial_search_result,
    _osc_fetch_fulltext_from_exact_case_search,
    _osc_pick_best_manifest_item,
    _osc_summarize_legal_insight,
    _osc_fetch_fulltext_from_judicial,
    # Core helper functions
    _osc_norm_case_category,
    _osc_resolve_case_id,
    _osc_safe_int,
    _osc_truthy,
    _osc_text,
    _osc_current_actor,
    _osc_log_activity,
    _osc_accounting_window,
    _osc_get_setting_value,
    _osc_unique_strings,
    _osc_read_plain_text,
    _osc_read_docx_text,
    _osc_read_pdf_text,
    _osc_read_textutil_text,
    _osc_resolve_existing_local_path_with_candidates,
    _osc_read_reference_document,
)
