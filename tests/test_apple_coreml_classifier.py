# -*- coding: utf-8 -*-
"""Tests for skills/apple/coreml_classifier.py — document classification."""

import pytest
from skills.apple.coreml_classifier import (
    DocumentClassifier,
    classify_document,
    get_classifier,
    CATEGORIES,
)


class TestDocumentClassifier:
    def setup_method(self):
        self.classifier = DocumentClassifier()  # keyword-only mode

    def test_judgment(self):
        cat, conf = self.classifier.classify("臺灣臺北地方法院刑事判決書 113年度易字第888號")
        assert cat == "判決書"
        assert conf >= 0.9

    def test_ruling(self):
        cat, conf = self.classifier.classify("裁定 案號：113年度勞訴字第19號 主文：准許假執行")
        assert cat == "裁定"
        assert conf >= 0.9

    def test_indictment(self):
        cat, conf = self.classifier.classify("起訴書 被告：黃語玲 犯罪事實")
        assert cat == "起訴書"
        assert conf >= 0.9

    def test_defense_brief(self):
        cat, conf = self.classifier.classify("答辯狀 案號：113年度勞訴字第19號")
        assert cat == "答辯狀"
        assert conf >= 0.9

    def test_preparation_brief(self):
        cat, conf = self.classifier.classify("準備書狀 案號：113年度訴字第100號")
        assert cat == "準備書狀"
        assert conf >= 0.9

    def test_transcript(self):
        cat, conf = self.classifier.classify("言詞辯論筆錄 案號：113年度訴字第100號")
        assert cat == "筆錄"
        assert conf >= 0.9

    def test_subpoena(self):
        cat, conf = self.classifier.classify("傳票 開庭通知 113年度訴字第100號")
        assert cat == "傳票"
        assert conf >= 0.9

    def test_mediation_notice(self):
        cat, conf = self.classifier.classify("調解通知 調解期日定為 2026年5月1日")
        assert cat == "調解通知"
        assert conf >= 0.9

    def test_payment_order(self):
        cat, conf = self.classifier.classify("支付命令 主文：債務人應給付")
        assert cat == "支付命令"
        assert conf >= 0.9

    def test_petition(self):
        cat, conf = self.classifier.classify("聲請狀 聲請人：王小明")
        assert cat == "聲請狀"
        assert conf >= 0.9

    def test_power_of_attorney(self):
        cat, conf = self.classifier.classify("委任狀 茲委任李律師")
        assert cat == "委任狀"
        assert conf >= 0.9

    def test_evidence_list(self):
        cat, conf = self.classifier.classify("證據清單 一、僱傭契約 二、薪資單")
        assert cat == "證據清單"
        assert conf >= 0.9

    def test_court_notice(self):
        cat, conf = self.classifier.classify("臺灣臺北地方法院庭通知書")
        assert cat == "庭通知書"
        assert conf >= 0.9

    def test_unknown(self):
        cat, conf = self.classifier.classify("這是一份普通的文件，沒有特殊格式")
        assert cat == "unknown"
        assert conf == 0.0

    def test_empty_text(self):
        cat, conf = self.classifier.classify("")
        assert cat == "unknown"
        assert conf == 0.0

    def test_batch_classify(self):
        texts = [
            "判決書主文：被告無罪",
            "裁定：駁回上訴",
            "這是普通文件",
        ]
        results = self.classifier.classify_batch(texts)
        assert len(results) == 3
        assert results[0][0] == "判決書"
        assert results[1][0] == "裁定"
        assert results[2][0] == "unknown"


class TestCategories:
    def test_categories_not_empty(self):
        assert len(CATEGORIES) > 10

    def test_common_types_present(self):
        for t in ["判決書", "裁定", "起訴書", "筆錄", "傳票"]:
            assert t in CATEGORIES


class TestGetClassifier:
    def test_returns_classifier(self):
        c = get_classifier()
        assert isinstance(c, DocumentClassifier)

    def test_classify_document_convenience(self):
        cat, conf = classify_document("裁定 案號：113年度勞訴字第19號")
        assert cat == "裁定"
