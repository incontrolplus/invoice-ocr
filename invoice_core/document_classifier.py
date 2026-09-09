"""Document Classifier for Bulgarian Accounting and Commercial Documents.

Classifies documents into 7 statutory and accounting categories:
1. Фактури (INVOICE)
2. Кредитни известия (CREDIT_NOTE)
3. Стокови разписки (STOCK_RECEIPT)
4. Фискални бонове (FISCAL_RECEIPT)
5. Пощенски парични преводи (POSTAL_MONEY_TRANSFER)
6. Платежни документи (PAYMENT_DOCUMENT)
7. Некласифицирани (UNCLASSIFIED)

Features:
- Deterministic keyword, structural, and regex pattern matching
- Evidence-based confidence scoring (0.0 to 1.0)
- Multi-page and multi-document bundle detection (e.g. Stock receipt + Invoice, multi-invoice files)
- Obscured / damaged document detection (e.g. tape/receipt covering invoice header -> UNCLASSIFIED)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import logging
from pathlib import Path
import re
from typing import Any, Optional, Sequence, Union

logger = logging.getLogger("document_classifier")


class DocumentCategory(str, Enum):
    FAKTURI = "Фактури"
    KREDITNI_IZVESTIYA = "Кредитни известия"
    STOKOVI_RAZPISKI = "Стокови разписки"
    FISKALNI_BONEVE = "Фискални бонове"
    POSHTENSKI_PARICHNI_PREVODI = "Пощенски парични преводи"
    PLATEZHNI_DOKUMENTI = "Платежни документи"
    NEKLASIFITSIRANI = "Некласифицирани"

    @property
    def code(self) -> str:
        mapping = {
            DocumentCategory.FAKTURI: "INVOICE",
            DocumentCategory.KREDITNI_IZVESTIYA: "CREDIT_NOTE",
            DocumentCategory.STOKOVI_RAZPISKI: "STOCK_RECEIPT",
            DocumentCategory.FISKALNI_BONEVE: "FISCAL_RECEIPT",
            DocumentCategory.POSHTENSKI_PARICHNI_PREVODI: "POSTAL_MONEY_TRANSFER",
            DocumentCategory.PLATEZHNI_DOKUMENTI: "PAYMENT_DOCUMENT",
            DocumentCategory.NEKLASIFITSIRANI: "UNCLASSIFIED",
        }
        return mapping[self]

    @classmethod
    def from_code(cls, code: str) -> DocumentCategory:
        mapping = {
            "INVOICE": cls.FAKTURI,
            "CREDIT_NOTE": cls.KREDITNI_IZVESTIYA,
            "STOCK_RECEIPT": cls.STOKOVI_RAZPISKI,
            "FISCAL_RECEIPT": cls.FISKALNI_BONEVE,
            "POSTAL_MONEY_TRANSFER": cls.POSHTENSKI_PARICHNI_PREVODI,
            "PAYMENT_DOCUMENT": cls.PLATEZHNI_DOKUMENTI,
            "UNCLASSIFIED": cls.NEKLASIFITSIRANI,
        }
        return mapping.get(code.upper(), cls.NEKLASIFITSIRANI)


@dataclass
class SubdocumentInfo:
    page_start: int  # 0-indexed, inclusive
    page_end: int    # 0-indexed, inclusive
    category: DocumentCategory
    confidence: float
    title: str = ""
    invoice_number: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "page_start": self.page_start,
            "page_end": self.page_end,
            "page_count": self.page_end - self.page_start + 1,
            "category": self.category.value,
            "category_code": self.category.code,
            "confidence": round(self.confidence, 4),
            "title": self.title,
            "invoice_number": self.invoice_number,
        }


@dataclass
class ClassificationResult:
    category: DocumentCategory
    confidence: float
    matched_keywords: list[str] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)
    is_obscured: bool = False
    obscured_reason: Optional[str] = None
    invoice_number: Optional[str] = None
    supplier_eik: Optional[str] = None
    supplier_name: Optional[str] = None
    has_subdocuments: bool = False
    subdocuments: list[SubdocumentInfo] = field(default_factory=list)
    page_count: int = 1
    details: str = ""
    extracted_text: str = ""

    @property
    def obscuration_reason(self) -> Optional[str]:
        return self.obscured_reason

    @property
    def category_name_bg(self) -> str:
        return self.category.value

    @property
    def category_code(self) -> str:
        return self.category.code

    def __iter__(self):
        return iter((self.category, self.confidence, self.matched_keywords, self.scores))

    def __getitem__(self, index: int):
        return (self.category, self.confidence, self.matched_keywords, self.scores)[index]

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category.value,
            "category_code": self.category.code,
            "confidence": round(self.confidence, 4),
            "matched_keywords": self.matched_keywords,
            "scores": {k: round(v, 4) for k, v in self.scores.items()},
            "is_obscured": self.is_obscured,
            "obscured_reason": self.obscured_reason,
            "obscuration_reason": self.obscured_reason,
            "invoice_number": self.invoice_number,
            "supplier_eik": self.supplier_eik,
            "supplier_name": self.supplier_name,
            "has_subdocuments": self.has_subdocuments,
            "subdocuments": [s.to_dict() for s in self.subdocuments],
            "page_count": self.page_count,
            "details": self.details,
            "extracted_text": (self.extracted_text or "")[:5000],
        }


PATTERNS = {
    DocumentCategory.KREDITNI_IZVESTIYA: {
        "strong": [
            r"\bкредитно\s+известие\b",
            r"\bкред\.?\s*известие\b",
            r"\bcredit\s+note\b",
            r"\bдебитно\s+известие\b",
            r"\bdebit\s+note\b",
            r"\bсториращ\s+документ\b",
            r"\bсторниране\b",
            r"\b(?:сторно|ctopho|ctorno|storno)\b",
            r"\bизвестие\s+към\s+фактура\b",
            r"\bкорекция\s+към\s+фактура\b",
            r"\bнамаление\s+(?:на\s+)?данъчна(?:та)?\s+основа\b",
            r"\bданъчно\s+известие\b",
            r"\b(?:по\s+)?ки\s*[:\s#№@]*\d{6,10}\b",
            r"\bки\s+към\s+ф[-–.]?р[ае]\b",
        ],
        "supporting": [
            r"\bкъм\s+фактура\s*№?\s*\d+\b",
            r"\bпричина\s+за\s+корекция\b",
            r"\bвръщане\s+на\s+стока\b",
            r"\bвръщане\s*[/и\s]*рек[лд]амация\b",
            r"\bрекламация\b",
            r"\bминус\s+[а-яa-z\s]+(?:eur|bgn|лв|лева|цента)\b",
            r"\b-\s*\d+[.,]\d{2}\s*(?:eur|bgn|лв|евро)\b",
        ],
    },
    DocumentCategory.FAKTURI: {
        "strong": [
            r"\bфактура\b",
            r"\binvoice\b",
            r"\bфактура\s*[-–]\s*оригинал\b",
            r"\bоригинал\s*[-–]\s*фактура\b",
            r"\bданъчна\s+фактура\b",
            r"\bопростена\s+фактура\b",
            r"\bфактура\s*№\b",
            r"\bфактура\s+no\b",
            r"\bелектронна\s+фактура\b",
        ],
        "supporting": [
            r"\bданъчна\s+основа\b",
            r"\bначислен\s+ддс\b",
            r"\bддс\s*20\s*%\b",
            r"\bставка\s+ддс\b",
            r"\bобща\s+сума\b",
            r"\bсума\s+за\s+плащане\b",
            r"\bсловом\b",
            r"\bдоставчик\b",
            r"\bполучател\b",
            r"\bклиент\b",
            r"\bин\s+по\s+ддс\b",
            r"\bеик\b",
            r"\bбулстат\b",
            r"\bбанкова\s+сметка\b",
            r"\biban\b",
            r"\bbic\b",
            r"\bмясто\s+на\s+сделката\b",
            r"\bдата\s+на\s+данъчно\s+събитие\b",
            r"\bчл\.\s*114\s+от\s+зддс\b",
        ],
    },
    DocumentCategory.STOKOVI_RAZPISKI: {
        "strong": [
            r"\bстокова\s+разписка\b",
            r"\bскладова\s+разписка\b",
            r"\bекспедиционна\s+бележка\b",
            r"\bприемо[-–\s]+предавателен\s+протокол\b",
            r"\bразписка\s+за\s+получена\s+стока\b",
            r"\bекспедиция\b",
            r"\bразписка\s*№?\s*\d+\b",
        ],
        "supporting": [
            r"\bпредал\s*(?:стоката)?\b",
            r"\bполучил\s*(?:стоката)?\b",
            r"\bсклад\b",
            r"\bот\s+склад\b",
            r"\bв\s+склад\b",
            r"\bматериално\s+отговорно\s+лице\b",
            r"\bмол\b",
            r"\bмерна\s+единица\b",
            r"\bмярка\b",
            r"\bколичество\b",
            r"\bнаправление\b",
            r"\bшофьор\b",
            r"\bпревозвач\b",
            r"\bномер\s+на\s+мпс\b",
        ],
    },
    DocumentCategory.FISKALNI_BONEVE: {
        "strong": [
            r"\bфискал[еа]н\s+бон\b",
            r"\bсист[еа]м[еа]н\s+бон\b",
            r"\bсист[еа]рнен\s+бон\b",
            r"\bслужебен\s+бон\b",
            r"\bкасов\s+бон\b",
            r"\bкасова\s+бележка\b",
            r"\bфискална\s+памет\b",
        ],
        "supporting": [
            r"\bзу\s*n[o.:]?\s*\w+",
            r"\bфп\s*n[o.:]?\s*\w+",
            r"\bин\s*на\s*фп\b",
            r"\bин\s*на\s*зу\b",
            r"\bfd\d{6,}\b",
            r"\bкасиер\b",
            r"\bкаса\s*(?:n[o.:]?)?\s*\d+",
            r"\bоператор\b",
            r"\bтотал\s*:\s*\d+[.,]\d{2}\b",
            r"\bв\s*брой\b",
            r"\bбезналично\b",
            r"\bкартово\s+плащане\b",
            r"\bресто\b",
            r"\bброй\s+артикули\b",
            r"\bобменен\s+курс\b",
            r"\bфискално\s+устройство\b",
            r"\bнаредба\s*н[-–\s]*18\b",
            r"\b\d{2}:\d{2}:\d{2}\b",  # timestamp with seconds
        ],
    },
    DocumentCategory.POSHTENSKI_PARICHNI_PREVODI: {
        "strong": [
            r"\bпощенски\s+паричен\s+превод\b",
            r"\bразписка\s+за\s+(?:прием|изплащане)\s+на\s+паричен\s+превод\b",
            r"\bпаричен\s+превод\b",
            r"\bналожен\s+платеж\b",
            r"\bтакса\s+наложен\s+платеж\b",
            r"\bппп\b",
            r"\bразписка\s+за\s+ппп\b",
            r"\bразписка\s+за\s+нп\b",
            r"\bнареждане\s+за\s+пощенски\s+паричен\s+превод\b",
        ],
        "supporting": [
            r"\bеконт\s+експрес\b",
            r"\becont\b",
            r"\bспиди\b",
            r"\bspeedy\b",
            r"\bбългарски\s+пощи\b",
            r"\bbg\s*post\b",
            r"\bтоварителница\b",
            r"\bподател\s+на\s+пратката\b",
            r"\bполучател\s+на\s+пратката\b",
            r"\bкуриерска\s+услуга\b",
            r"\bкод\s+на\s+пратка\b",
            r"\bномер\s+на\s+разписка\b",
        ],
    },
    DocumentCategory.PLATEZHNI_DOKUMENTI: {
        "strong": [
            r"\bплатежно\s+нареждане\b",
            r"\bвносна\s+бележка\b",
            r"\bнареждане\s+за\s+превод\b",
            r"\bбанково\s+бордеро\b",
            r"\bпреводно\s+нареждане\b",
            r"\b(?:moneygram|[lm]oneygram)\b",
            r"\bwestern\s+union\b",
            r"\b(?:easypay|изипей)\b",
            r"\bбанково\s+извлечение\b",
            r"\bразписка\s+за\s+банков\s+превод\b",
            r"\bмеждународен\s+превод\b",
            r"\bвалутен\s+превод\b",
            r"\bплатежна\s+услуга\b",
            r"\bpayment\s+service\b",
            r"\bsend\s+money\b",
        ],
        "supporting": [
            r"\b(?:наредител|наредителя|sender)\b",
            r"\bплатец\b",
            r"\bсметка\s+на\s+наредителя\b",
            r"\bсметка\s+на\s+получателя\b",
            r"\bвид\s+валута\b",
            r"\bтакса\s+за\s+превод\b",
            r"\bреферентен\s+номер\b",
            r"\bвид\s+плащане\b",
            r"\bкод\s+за\s+вид\s+плащане\b",
            r"\bоснование\s+за\s+(?:плащане|превода|превод)\b",
            r"\bpurpose\s+of\s+money\s+transfer\b",
            r"\bзахранване\s+на\s+(?:собствена\s+)?сметка\b",
            r"\bпериод\s+на\s+извлечението\b",
            r"\bначално\s+салдо\b",
            r"\bкрайно\s+салдо\b",
            r"\bдебит\b",
            r"\bкредит\b",
        ],
    },
}


class DocumentClassifier:
    """Core classification engine for commercial and accounting documents."""

    def __init__(self, confidence_threshold: float = 0.40):
        self.confidence_threshold = confidence_threshold

    def classify_text(
        self,
        text: str,
        page_num: int = 1,
        inspect_obscuration: bool = False,
    ) -> ClassificationResult:
        cleaned_text = (text or "").lower()
        if not cleaned_text.strip():
            return ClassificationResult(
                category=DocumentCategory.NEKLASIFITSIRANI,
                confidence=0.0,
                matched_keywords=[],
                scores={cat.value: 0.0 for cat in DocumentCategory},
                page_count=1,
                extracted_text=text or "",
            )

        if inspect_obscuration:
            is_obs, obs_reason = self.inspect_obscuration(text, [text])
            if is_obs:
                return ClassificationResult(
                    category=DocumentCategory.NEKLASIFITSIRANI,
                    confidence=0.95,
                    matched_keywords=["закрит_номер", "прикрепен_бон", "нечетлив_колонтитул"],
                    scores={DocumentCategory.NEKLASIFITSIRANI.value: 10.0},
                    is_obscured=True,
                    obscured_reason=obs_reason,
                    page_count=1,
                    details=f"Документът изисква повторно сканиране: {obs_reason}",
                    extracted_text=text,
                )

        scores: dict[DocumentCategory, float] = {cat: 0.0 for cat in DocumentCategory}
        matches_found: dict[DocumentCategory, list[str]] = {cat: [] for cat in DocumentCategory}

        for cat, pat_group in PATTERNS.items():
            for pat in pat_group.get("strong", []):
                found = re.findall(pat, cleaned_text)
                if found:
                    scores[cat] += len(found) * 5.0
                    clean_pat = pat.replace(r"\b", "").replace(r"\s+", " ").replace("\\", "")
                    matches_found[cat].append(clean_pat)

            for pat in pat_group.get("supporting", []):
                found = re.findall(pat, cleaned_text)
                if found:
                    scores[cat] += len(found) * 1.5
                    clean_pat = pat.replace(r"\b", "").replace(r"\s+", " ").replace("\\", "")
                    matches_found[cat].append(clean_pat)

        # -------------------------------------------------------------------
        # Disambiguation & Context Boosts
        # -------------------------------------------------------------------

        # 1. Credit Notes rule over Invoices and subordinate Storno Fiscal Slips
        has_credit_or_storno = bool(re.search(
            r"\b(?:кредитно\s+известие|кред\.?\s*известие|дебитно\s+известие|сторно|ctopho|ctorno|storno|по\s+ки\b|ки\s*[:\s#№@]*\d+)\b",
            cleaned_text,
        ))
        if has_credit_or_storno and scores[DocumentCategory.KREDITNI_IZVESTIYA] >= 4.0:
            scores[DocumentCategory.KREDITNI_IZVESTIYA] += scores[DocumentCategory.FAKTURI] * 0.5
            scores[DocumentCategory.FAKTURI] *= 0.1
            # If an attached storno fiscal slip is present, the Credit Note is the primary statutory document
            if scores[DocumentCategory.FISKALNI_BONEVE] > 0:
                scores[DocumentCategory.KREDITNI_IZVESTIYA] += scores[DocumentCategory.FISKALNI_BONEVE] * 0.8
                scores[DocumentCategory.FISKALNI_BONEVE] *= 0.1

        # 2. Fiscal Receipt vs Invoice
        has_invoice_title = bool(re.search(r"\bфактура\b", cleaned_text))
        has_fiscal_title = bool(re.search(r"\bфискал[еа]н\s+бон\b|\bсист[еа]м[еа]н\s+бон\b|\bсист[еа]рнен\s+бон\b", cleaned_text))
        has_vat_tax_base = bool(re.search(r"\bданъчна\s+основа\b|\bначислен\s+ддс\b", cleaned_text))

        if has_fiscal_title and not has_credit_or_storno and not has_vat_tax_base and not (has_invoice_title and scores[DocumentCategory.FAKTURI] > 10):
            scores[DocumentCategory.FISKALNI_BONEVE] += 8.0
            scores[DocumentCategory.FAKTURI] *= 0.2
        elif has_invoice_title and has_vat_tax_base and not has_credit_or_storno:
            scores[DocumentCategory.FAKTURI] += 6.0
            scores[DocumentCategory.FISKALNI_BONEVE] *= 0.3

        # 3. Stock Receipt vs Invoice
        has_stock_title = bool(re.search(r"\bстокова\s+разписка\b|\bскладова\s+разписка\b", cleaned_text))
        if has_stock_title and not has_vat_tax_base:
            scores[DocumentCategory.STOKOVI_RAZPISKI] += 10.0
            scores[DocumentCategory.FAKTURI] *= 0.1

        # 4. Postal Money Transfer vs Fiscal vs Invoice
        has_ppp = bool(re.search(r"\bпощенски\s+паричен\s+превод\b|\bразписка\s+за\s+(?:прием|изплащане)\s+на\s+паричен\s+превод\b|\bналожен\s+платеж\b|\bтакса\s+наложен\s+платеж\b", cleaned_text))
        if has_ppp and not has_vat_tax_base:
            scores[DocumentCategory.POSHTENSKI_PARICHNI_PREVODI] += 10.0
            scores[DocumentCategory.FAKTURI] *= 0.1
            scores[DocumentCategory.FISKALNI_BONEVE] *= 0.3

        # 5. Payment Documents (MoneyGram, EasyPay, Bank, etc.)
        has_payment = bool(re.search(
            r"\b(?:вносна\s+бележка|платежно\s+нареждане|moneygram|[lm]oneygram|western\s+union|"
            r"easypay|изипей|платежна\s+услуга|payment\s+service|наредител|наредителя|sender|"
            r"основание\s+за\s+(?:плащане|превода|превод)|purpose\s+of\s+money\s+transfer|"
            r"захранване\s+на\s+(?:собствена\s+)?сметка)\b",
            cleaned_text,
        ))
        if has_payment and not (has_invoice_title and scores[DocumentCategory.FAKTURI] > 10.0):
            scores[DocumentCategory.PLATEZHNI_DOKUMENTI] += 12.0
            scores[DocumentCategory.FAKTURI] *= 0.1

        total_score = sum(scores.values())
        if total_score == 0:
            return ClassificationResult(
                category=DocumentCategory.NEKLASIFITSIRANI,
                confidence=0.0,
                matched_keywords=[],
                scores={cat.value: 0.0 for cat in DocumentCategory},
                page_count=1,
                extracted_text=text or "",
            )

        best_cat = max(scores, key=lambda c: scores[c])
        best_score = scores[best_cat]

        confidence = min(0.99, best_score / (best_score + 5.0))
        if best_score < 4.0 or confidence < self.confidence_threshold:
            best_cat = DocumentCategory.NEKLASIFITSIRANI
            confidence = max(0.1, confidence)

        matched_kws = matches_found.get(best_cat, [])[:8]
        raw_scores_str = {cat.value: scores[cat] for cat in DocumentCategory}

        return ClassificationResult(
            category=best_cat,
            confidence=confidence,
            matched_keywords=matched_kws,
            scores=raw_scores_str,
            page_count=1,
            extracted_text=text or "",
        )

    def inspect_obscuration(self, text: str, pages_text: list[str]) -> tuple[bool, Optional[str]]:
        combined = " ".join(pages_text) if pages_text else text
        cleaned = combined.lower()

        # Credit notes / storno documents have their own header and are not obscured invoices
        if bool(re.search(r"\b(?:кредитно\s+известие|кред\.?\s*известие|дебитно\s+известие|сторно)\b", cleaned)):
            return False, None

        # Check for explicit invoice header with an actual invoice number
        has_invoice_header = bool(re.search(r"\bфактура\s*(?:№|no|номер)?\s*[:.\s-]*\d{8,10}\b", cleaned)) or \
                             bool(re.search(r"\bфактура\s*[-–]\s*оригинал\b|\bоригинал\s*[-–]\s*фактура\b", cleaned))

        # Check for attached fiscal slip / tape
        has_fiscal_tape = bool(re.search(r"\b(?:фискален\s+бон|системен\s+бон|сист[еа]рнен\s+бон|форма\s+на\s+плащане\s*:\s*в\s+брой|в\s+брой\s+евро|фд\d+)\b", cleaned))

        # Check for commercial/telecom service lines with VAT (like Vivacom in 16.pdf)
        has_invoice_body = (
            bool(re.search(r"\b(?:предпл|абонамент|месечна\s+такса|виваком|vivacom|а1\s+българия|йетел|yettel)\b", cleaned))
            and bool(re.search(r"\b(?:ддс|данъчна\s+основа|сума\s+по\s+фактура|срок\s+за\s+плащане|план\b)\b", cleaned))
        ) or (
            bool(re.search(r"\b(?:ддс\s*20\s*%|данъчна\s+основа)\b", cleaned))
            and bool(re.search(r"\b(?:доставчик|получател|клиент)\b", cleaned))
        )

        # If it has invoice body and attached fiscal tape, but lacks visible invoice header/number
        if has_invoice_body and has_fiscal_tape and not has_invoice_header:
            return True, "Фактура със закрит горен колонтитул (прикрепен фискален бон закрива номера и датата на издаване)"

        return False, None

    def classify_document(
        self,
        pages_text: Sequence[str],
        file_path: Optional[Union[str, Path]] = None,
    ) -> ClassificationResult:
        page_count = len(pages_text) if pages_text else 1
        merged_text = "\n\n--- PAGE BREAK ---\n\n".join(pages_text) if pages_text else ""

        is_obscured, obscured_reason = self.inspect_obscuration(merged_text, list(pages_text))
        if is_obscured:
            return ClassificationResult(
                category=DocumentCategory.NEKLASIFITSIRANI,
                confidence=0.95,
                matched_keywords=["закрит_номер", "прикрепен_бон", "нечетлив_колонтитул"],
                scores={DocumentCategory.NEKLASIFITSIRANI.value: 10.0},
                is_obscured=True,
                obscured_reason=obscured_reason,
                page_count=page_count,
                details=f"Документът изисква повторно сканиране: {obscured_reason}",
            )

        page_classifications: list[tuple[DocumentCategory, float, list[str]]] = []
        for pno, ptext in enumerate(pages_text):
            pcat, pconf, pkw, _ = self.classify_text(ptext, page_num=pno + 1)
            page_classifications.append((pcat, pconf, pkw))

        has_subdocuments = False
        subdocuments: list[SubdocumentInfo] = []

        if page_count > 1 and len(page_classifications) > 1:
            curr_cat = page_classifications[0][0]
            curr_start = 0
            curr_conf = page_classifications[0][1]

            distinct_categories = set(pc[0] for pc in page_classifications)

            if len(distinct_categories) > 1:
                for idx in range(1, page_count):
                    cat_i, conf_i, _ = page_classifications[idx]
                    if cat_i != curr_cat:
                        subdocuments.append(SubdocumentInfo(
                            page_start=curr_start,
                            page_end=idx - 1,
                            category=curr_cat,
                            confidence=curr_conf,
                            title=f"{curr_cat.value} (стр. {curr_start+1}-{idx})",
                        ))
                        curr_cat = cat_i
                        curr_start = idx
                        curr_conf = conf_i
                    else:
                        curr_conf = max(curr_conf, conf_i)

                subdocuments.append(SubdocumentInfo(
                    page_start=curr_start,
                    page_end=page_count - 1,
                    category=curr_cat,
                    confidence=curr_conf,
                    title=f"{curr_cat.value} (стр. {curr_start+1}-{page_count})",
                ))

                if len(subdocuments) > 1:
                    has_subdocuments = True

        overall_cat, overall_conf, overall_kw, overall_scores = self.classify_text(merged_text)

        inv_match = re.search(r"(?:фактура|номер|№|invoice|no)[^\d\n]{0,10}(\d{6,10})", merged_text, re.I)
        inv_no = inv_match.group(1) if inv_match else None

        eik_match = re.search(r"\b(?:еик|булстат|eik)[^\d\n]{0,10}(\d{9,13})\b", merged_text, re.I)
        eik = eik_match.group(1) if eik_match else None

        supplier_match = re.search(r"(?:доставчик|издател)[^\w\n]{0,5}([„\"'][^\"'„”\n]+[”\"']|\b[А-ЯA-Z][а-яa-zА-ЯA-Z0-9\s]{3,30}\b\s*(?:еоод|оод|ад|еад))", merged_text, re.I)
        supplier_name = supplier_match.group(1).strip("„\"' ") if supplier_match else None

        details_msg = f"Класифициран като '{overall_cat.value}' с увереност {overall_conf*100:.1f}%."
        if has_subdocuments:
            details_msg += f" Открити са {len(subdocuments)} съставни документа (напр. отделни страници с различна категория)."

        return ClassificationResult(
            category=overall_cat,
            confidence=overall_conf,
            matched_keywords=overall_kw,
            scores=overall_scores,
            is_obscured=False,
            invoice_number=inv_no,
            supplier_eik=eik,
            supplier_name=supplier_name,
            has_subdocuments=has_subdocuments,
            subdocuments=subdocuments,
            page_count=page_count,
            details=details_msg,
            extracted_text=merged_text,
        )

    def classify_pages(
        self,
        pages_text: Sequence[str],
        file_path: Optional[Union[str, Path]] = None,
    ) -> ClassificationResult:
        return self.classify_document(pages_text=pages_text, file_path=file_path)

    def classify_file(
        self,
        file_path: Union[str, Path],
        file_name: Optional[str] = None,
    ) -> ClassificationResult:
        path = Path(file_path)
        if not path.exists():
            return ClassificationResult(
                category=DocumentCategory.NEKLASIFITSIRANI,
                confidence=0.0,
                details=f"File not found: {path}",
            )

        suffix = path.suffix.lower()
        pages_text: list[str] = []

        if suffix in (".pdf",):
            try:
                import fitz
                doc = fitz.open(str(path))
                for page in doc:
                    txt = page.get_text("text") or ""
                    pages_text.append(txt)
                doc.close()
            except Exception as e:
                logger.warning("PyMuPDF text extraction failed for %s: %s", path.name, e)

            # Check if text was empty or sparse (scanned PDF), run OCR
            total_chars = sum(len(t.strip()) for t in pages_text)
            if total_chars < 30:
                try:
                    from invoice_core.ingestion import load_document
                    import pytesseract
                    pages = load_document(path)
                    ocr_pages = []
                    for p in pages:
                        img = getattr(p, "image", None)
                        if img is not None:
                            ocr_txt = pytesseract.image_to_string(img, lang="bul+eng")
                            ocr_pages.append(ocr_txt)
                    if ocr_pages:
                        pages_text = ocr_pages
                except Exception as ocr_err:
                    logger.warning("Fallback OCR failed for scanned PDF %s: %s", path.name, ocr_err)
        else:
            # Image file (PNG, JPG, TIFF, etc.)
            try:
                from invoice_core.ingestion import load_document
                import pytesseract
                pages = load_document(path)
                ocr_pages = []
                for p in pages:
                    img = getattr(p, "image", None)
                    if img is not None:
                        ocr_txt = pytesseract.image_to_string(img, lang="bul+eng")
                        ocr_pages.append(ocr_txt)
                if ocr_pages:
                    pages_text = ocr_pages
            except Exception as img_err:
                logger.warning("OCR failed for image %s: %s", path.name, img_err)

        if not pages_text:
            pages_text = [""]

        # Filename clues boost for OCR disambiguation
        effective_name = (file_name or path.name).lower()
        if re.search(r"[\(_\s-]ки[\)_\s.\d-]|кредитн|сторно|credit", effective_name):
            pages_text = [p + "\nкредитно известие сторно" for p in pages_text]
        elif re.search(r"[\(_\s-]фактур|invoice", effective_name):
            pages_text = [p + "\nфактура" for p in pages_text]
        elif re.search(r"[\(_\s-]сток|разписк", effective_name):
            pages_text = [p + "\nстокова разписка" for p in pages_text]

        return self.classify_document(pages_text=pages_text, file_path=path)


_classifier = DocumentClassifier()


def get_document_classifier() -> DocumentClassifier:
    return _classifier


def classify_text(text: str) -> ClassificationResult:
    return _classifier.classify_text(text)


def classify_file(file_path: Union[str, Path], file_name: Optional[str] = None) -> ClassificationResult:
    return _classifier.classify_file(file_path=file_path, file_name=file_name)
