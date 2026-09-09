"""Tests for Statutory Document Classifier (invoice_core/document_classifier.py).

Verifies accurate classification across all 7 statutory categories:
1. Фактури (FAKTURI)
2. Кредитни известия (KREDITNI_IZVESTIYA)
3. Стокови разписки (STOKOVI_RAZPISKI)
4. Фискални бонове (FISKALNI_BONEVE)
5. Пощенски парични преводи (POSHTENSKI_PARICHNI_PREVODI)
6. Платежни документи (PLATEZHNI_DOKUMENTI)
7. Некласифицирани (NEKLASIFITSIRANI)

Also verifies multi-page transition detection and physical obscuration detection (e.g. 16.pdf).
"""

import pytest
from invoice_core.document_classifier import (
    DocumentClassifier,
    DocumentCategory,
    ClassificationResult,
    get_document_classifier,
)


@pytest.fixture
def classifier():
    return get_document_classifier()


class TestDocumentClassification:
    def test_classify_faktura_text(self, classifier):
        text = """
        ОРИГИНАЛ
        ФАКТУРА № 1000023456
        Дата на издаване: 15.03.2024
        ДОСТАВЧИК: ТЕХНОМАРКЕТ ЕООД
        ЕИК: 123456789 ДДС № BG123456789
        ПОЛУЧАТЕЛ: СИКРЕТ ЛЕДЖЪНД ЕООД
        ЕИК: 204567890
        Данъчна основа: 1000.00 лв.
        ДДС 20%: 200.00 лв.
        Сума за плащане: 1200.00 лв.
        """
        res = classifier.classify_text(text)
        assert res.category == DocumentCategory.FAKTURI
        assert res.confidence >= 0.7
        assert "фактура" in res.matched_keywords

    def test_classify_kreditno_izvestie_storno(self, classifier):
        text = """
        КРЕДИТНО ИЗВЕСТИЕ № 0000000068
        Към Фактура № 1000023456 от 15.03.2024
        Основание: Връщане на дефектна стока
        Сторнирана сума: -120.00 лв.
        ДДС 20%: -24.00 лв.
        Обща сторно стойност: -144.00 лв.
        """
        res = classifier.classify_text(text)
        assert res.category == DocumentCategory.KREDITNI_IZVESTIYA
        assert res.confidence >= 0.7
        assert any("кредитно известие" in kw or "сторно" in kw for kw in res.matched_keywords)

    def test_classify_stokova_razpiska(self, classifier):
        text = """
        СТОКОВА РАЗПИСКА № 1205
        Дата: 10.02.2024
        Предал: Иван Иванов
        Приел: Георги Петров
        Склад: Централен склад София
        1. Кабел UTP Cat5e - 100 м.
        2. Конектор RJ45 - 50 бр.
        Подпис предал: ........  Подпис приел: ........
        """
        res = classifier.classify_text(text)
        assert res.category == DocumentCategory.STOKOVI_RAZPISKI
        assert res.confidence >= 0.7
        assert "стокова разписка" in res.matched_keywords

    def test_classify_fiskalen_bon(self, classifier):
        text = """
        ФИСКАЛЕН БОН
        ДАТА: 05.01.2024 ЧАС: 14:23:10
        КАСОВ БОН
        АРТИКУЛ 1               10.50 Б
        ОБЩА СУМА:              10.50
        В БРОЙ:                 20.00
        РЕСТО:                   9.50
        ФИСКАЛНА ПАМЕТ: FP123456
        ФИСКАЛЕН АПАРАТ: DT512034
        КЛЕН: 001234
        """
        res = classifier.classify_text(text)
        assert res.category == DocumentCategory.FISKALNI_BONEVE
        assert res.confidence >= 0.7
        assert any(kw in ["фискален бон", "касов бон", "фискална памет", "фискален апарат"] for kw in res.matched_keywords)

    def test_classify_poshtenski_parichen_prevod(self, classifier):
        text = """
        РАЗПИСКА ЗА ПОЩЕНСКИ ПАРИЧЕН ПРЕВОД (ППП)
        Лицензиран пощенски оператор: ЕКОНТ ЕКСПРЕС ООД
        Наложен платеж по товарителница № 1055443322
        Изплатена сума: 340.50 лв.
        Получател на превода: СИКРЕТ ЛЕДЖЪНД ЕООД
        Основание: Наложен платеж
        На основание чл. 3 от Наредба Н-18 не се издава фискален касов бон.
        """
        res = classifier.classify_text(text)
        assert res.category == DocumentCategory.POSHTENSKI_PARICHNI_PREVODI
        assert res.confidence >= 0.7
        assert any("пощенски паричен превод" in kw or "наложен платеж" in kw or "еконт" in kw for kw in res.matched_keywords)

    def test_classify_platezhen_dokument(self, classifier):
        text = """
        ПЛАТЕЖНО НАРЕЖДАНЕ / ВНОСНА БЕЛЕЖКА
        За кредитен превод към бюджет / доставчик
        IBAN на наредителя: BG80BNBG91234567890123
        BIC: BNBGBGSF
        IBAN на получателя: BG12UBBS80021012345678
        Сума: 5500.00 BGN
        Банково извлечение № 42
        Основание за плащане: Плащане по договор
        Счетоводен оператор: ОББ АД
        """
        res = classifier.classify_text(text)
        assert res.category == DocumentCategory.PLATEZHNI_DOKUMENTI
        assert res.confidence >= 0.7
        assert any("платежно нареждане" in kw or "банково извлечение" in kw or "iban" in kw for kw in res.matched_keywords)

    def test_classify_obscured_document_neklasifitsirani(self, classifier):
        """Simulates 16.pdf where receipt covers invoice header."""
        text = """
        ФИСКАЛЕН БОН
        КАСОВ БОН
        Vivacom България
        СУМА: 45.20 BGN
        
        Месечен абонамент мобилни услуги
        План Unlimited 50
        Интернет трафик в роуминг
        Срок за плащане: 25.04.2024
        """
        # Text has invoice-like line items and telecom subscription, plus fiscal receipt,
        # but completely lacks "Фактура № <номер>" or invoice header
        res = classifier.classify_text(text, inspect_obscuration=True)
        assert res.is_obscured is True
        assert res.category == DocumentCategory.NEKLASIFITSIRANI
        assert "покрива" in res.obscuration_reason.lower() or "obscured" in res.obscuration_reason.lower() or "бон" in res.obscuration_reason.lower()

    def test_classify_subdocument_transitions(self, classifier):
        pages = [
            "СТОКОВА РАЗПИСКА № 505\nПредал: Иван\nПриел: Георги\nСклад: Централен",
            "ОРИГИНАЛ\nФАКТУРА № 1000009999\nДата: 12.01.2024\nДоставчик: ABC\nДДС 20%\nСума: 240.00 лв",
        ]
        res = classifier.classify_pages(pages)
        assert len(res.subdocuments) >= 2
        assert res.subdocuments[0].category == DocumentCategory.STOKOVI_RAZPISKI
        assert res.subdocuments[1].category == DocumentCategory.FAKTURI
