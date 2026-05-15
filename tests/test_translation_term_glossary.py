from api.handlers.document_handler import (
    build_translation_term_glossary,
    ensure_translation_terms_visible,
    polish_translated_document_text,
    translation_idiom_issues,
)


def test_translation_term_glossary_keeps_article_proper_nouns_and_concepts():
    source = (
        "Kate Seear. La Trobe University. Psychiatry, Psychology and Law. "
        "Making addicts: critical reflections on agency and responsibility from lawyers and decision makers. "
        "The paper discusses addiction, criminal law, therapeutic jurisprudence, neuroscience, and drug court practice."
    )

    glossary = build_translation_term_glossary(source, target_lang="繁體中文")

    assert "Kate Seear" in glossary
    assert "La Trobe University" in glossary
    assert "agency" in glossary
    assert "responsibility" in glossary
    assert "addiction" in glossary
    assert "therapeutic jurisprudence" in glossary


def test_translation_terms_are_annotated_on_every_occurrence():
    source = "Addiction, agency, and responsibility. Addiction is discussed again."
    glossary = build_translation_term_glossary(source, target_lang="繁體中文")

    fixed = ensure_translation_terms_visible(
        source,
        "成癮、能動性與責任。成癮再次被討論。",
        term_glossary=glossary,
        target_lang="繁體中文",
    )

    assert fixed.count("成癮（addiction）") == 2
    assert "能動性（agency）" in fixed
    assert "責任（responsibility）" in fixed


def test_previous_life_as_professional_role_is_not_reincarnation():
    source = "So, in my previous life as a prosecutor, DSM-IV had come out."
    bad = "所以，我前世是檢察官。DSM-IV 已經出版。"

    assert translation_idiom_issues(source, bad)
    fixed = polish_translated_document_text(bad)
    assert "前世" not in fixed
    assert "我之前擔任檢察官時" in fixed


def test_academic_concept_postprocess_keeps_original_terms_every_time():
    source = "Agency and responsibility matter. Agency appears again at La Trobe University."
    glossary = build_translation_term_glossary(source, target_lang="繁體中文")

    fixed = ensure_translation_terms_visible(
        source,
        "代理權與責任很重要。代理權再次出現在拉籌伯大學。",
        term_glossary=glossary,
        target_lang="繁體中文",
    )

    assert fixed.count("能動性（agency）") == 2
    assert "責任（responsibility）" in fixed
    assert "拉籌伯大學（La Trobe University）" in fixed
