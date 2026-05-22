"""Extract (question_number, answer) pairs from an answer-key file using Mathpix + Gemini."""

import json
import logging
import re
from typing import List, Dict

import google.generativeai as genai
from django.conf import settings

from questions.services.agent_extraction_service import MathpixOCR
from questions.services.file_parser import FileParserService

logger = logging.getLogger('extraction')

_PROMPT = """You are parsing an answer key.

Return a JSON array. Each element must be:
  {"question_number": <integer>, "answer": <string>}

Rules:
- "answer" is the raw value as it appears in the document (e.g. "A", "B", "C, D", "24.5", "True").
- For multiple correct answers, join the labels with a comma (e.g. "A, C").
- Do not invent question text, options, or explanations.
- Skip rows where the answer is missing, unclear, or shown as a dash.
- Sort the array by question_number ascending.

Answer key text:
---
{text}
---

Output ONLY the JSON array. No prose, no markdown fences."""


def _read_text(file_path: str, content_type: str) -> str:
    """Return text content of the uploaded file. Prefers Mathpix for PDFs."""
    mathpix_id = getattr(settings, 'MATHPIX_APP_ID', '')
    mathpix_key = getattr(settings, 'MATHPIX_APP_KEY', '')
    is_pdf = (content_type == 'application/pdf') or file_path.lower().endswith('.pdf')

    if mathpix_id and mathpix_key and is_pdf:
        try:
            return MathpixOCR(mathpix_id, mathpix_key).process_pdf(file_path)
        except Exception as e:
            logger.warning(f"Mathpix failed for answer-key {file_path}, falling back to local parser: {e}")

    return FileParserService().parse_file(file_path, content_type)


def _parse_json_array(raw: str) -> list:
    """Extract a JSON array from a model response, tolerating ```json fences and surrounding prose."""
    match = re.search(r'\[.*\]', raw, re.DOTALL)
    if not match:
        raise ValueError(f"Model did not return a JSON array. First 200 chars: {raw[:200]!r}")
    return json.loads(match.group(0))


def extract_answer_key(file_path: str, content_type: str) -> List[Dict]:
    """Run OCR + Gemini on an answer-key file and return a list of normalized answer entries.

    Each entry is ``{"question_number": int, "answer": str}``. Entries with missing or
    unparseable values are dropped silently. Raises if no text can be read or if the
    model output cannot be parsed as JSON.
    """
    text = _read_text(file_path, content_type)
    if not text or not text.strip():
        raise ValueError("Could not extract text from the uploaded file")

    api_key = getattr(settings, 'GEMINI_API_KEY', '')
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not configured")

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(getattr(settings, 'GEMINI_MODEL', 'gemini-2.0-flash'))

    response = model.generate_content(_PROMPT.format(text=text))
    raw = (getattr(response, 'text', '') or '').strip()
    data = _parse_json_array(raw)

    entries: List[Dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            q_no = int(item.get('question_number'))
        except (TypeError, ValueError):
            continue
        answer = str(item.get('answer') or '').strip()
        if not answer:
            continue
        entries.append({'question_number': q_no, 'answer': answer})
    return entries
