# Paperless Assistant - an AI companion for Paperless-NGX.
# Copyright (C) 2026 BP Technology Advisors LLC
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""MetadataExtractor - strict structured-output metadata extraction with
reuse-first taxonomy (I5) + new-taxonomy flagging (I4).

Extracted from: `METADATA_TOOL`, `build_prompt`, `claude_metadata`,
`apply_metadata` (stage2).

The Anthropic call is isolated behind `_extract()` (a thin internal seam) so
Phase 2 can introduce the provider abstraction. The abstraction itself is NOT
built here - Anthropic is called directly with forced tool use.
"""
from __future__ import annotations

from . import config
from .providers.base import extract_structured_validated


# The engine owns the schema (plan §5.3). Byte-identical to the POC tool.
METADATA_TOOL = {
    "name": "document_metadata",
    "description": "Return structured metadata for a document.",
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Concise, human-readable title (<= 12 words). No file extension.",
            },
            "correspondent": {
                "type": "string",
                "description": "The organization or person the document is from/to. Empty string if unclear.",
            },
            "document_type": {
                "type": "string",
                "description": "e.g. Invoice, Statement, Letter, Receipt, Contract, Report. Empty if unclear.",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "1-5 topical tags.",
            },
            "correspondent_is_new": {
                "type": "boolean",
                "description": "true if correspondent is NOT in the provided existing list.",
            },
            "document_type_is_new": {
                "type": "boolean",
                "description": "true if document_type is NOT in the provided existing list.",
            },
            "new_tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Subset of tags that are NOT in the provided existing list.",
            },
        },
        "required": [
            "title",
            "correspondent",
            "document_type",
            "tags",
            "correspondent_is_new",
            "document_type_is_new",
            "new_tags",
        ],
    },
}


# The engine-owned JSON Schema for structured validation. Adapters translate it
# into their native mechanism; the engine re-validates every result against THIS.
METADATA_SCHEMA = METADATA_TOOL["input_schema"]


def build_prompt(doc, existing_tags, existing_corr, existing_types, *,
                 instruction=None, content_head=None, content_tail=None):
    """Assemble the metadata prompt: the (customizable) natural-language
    INSTRUCTION, then the FIXED taxonomy lists + document-text assembly.

    `instruction` is the resolved instruction text (default -> override -> +extra);
    when omitted it falls back to `prompts.METADATA_INSTRUCTION_DEFAULT`, so a
    caller that passes nothing reproduces the pre-010 prompt BYTE-IDENTICALLY. Only
    the leading instruction is customizable; the EXISTING-* taxonomy lists and the
    document text below are engine-owned and unchanged (they carry the data the
    fixed JSON schema is filled from)."""
    from .prompts import METADATA_INSTRUCTION_DEFAULT

    if instruction is None:
        instruction = METADATA_INSTRUCTION_DEFAULT
    # Prompt 011: the content window is configurable (defaults byte-identical to
    # CONTENT_HEAD/CONTENT_TAIL).
    head = config.CONTENT_HEAD if content_head is None else content_head
    tail = config.CONTENT_TAIL if content_tail is None else content_tail
    content = doc.get("content") or ""
    if len(content) > head + tail:
        content = content[:head] + "\n...\n" + content[-tail:]
    return (
        f"{instruction}\n\n"
        f"EXISTING CORRESPONDENTS:\n{', '.join(existing_corr) or '(none)'}\n\n"
        f"EXISTING DOCUMENT TYPES:\n{', '.join(existing_types) or '(none)'}\n\n"
        f"EXISTING TAGS:\n{', '.join(existing_tags) or '(none)'}\n\n"
        f"CURRENT TITLE: {doc.get('title') or '(none)'}\n\n"
        f"DOCUMENT TEXT:\n{content}"
    )


class MetadataExtractor:
    def __init__(self, client, resolver, taxonomy, safety, spend, *, api_key=None,
                 new_tax_tag_id, provider=None, instruction=None,
                 content_head=None, content_tail=None):
        self.client = client
        self.resolver = resolver
        self.taxonomy = taxonomy
        self.safety = safety
        self.spend = spend
        self.api_key = api_key
        self.new_tax_tag_id = new_tax_tag_id
        # The resolved metadata INSTRUCTION (default -> override -> +extra). None
        # keeps the built-in default, so existing callers/tests are byte-identical.
        self.instruction = instruction
        # Prompt 011: configurable content window (None -> byte-identical defaults).
        self.content_head = content_head
        self.content_tail = content_tail
        # Phase 2: the structured seam delegates to an AIProvider. Default to the
        # Anthropic adapter built from api_key so existing behavior is preserved.
        if provider is None:
            from .providers.anthropic import AnthropicProvider

            provider = AnthropicProvider(
                api_key=api_key,
                ocr_model=config.OCR_MODEL,
                metadata_model=config.METADATA_MODEL,
                max_ocr_tokens=config.MAX_OCR_TOKENS,
            )
        self.provider = provider

    # -- structured seam: provider + engine-side schema validation ---------
    def _extract(self, doc):
        """Structured extraction via the configured provider, RE-VALIDATED by the
        engine against METADATA_SCHEMA (plan §5.3). Return (metadata_dict, cost),
        shape unchanged.

        The engine owns the schema; the provider only translates it. Every
        returned dict is validated against METADATA_SCHEMA here; an off-schema
        result is retried and, if it never validates, raises
        SchemaValidationError - so no bad structured result can ever be applied
        to Paperless."""
        etags, ecorr, etypes = self.taxonomy.existing_lists()
        prompt = build_prompt(
            doc, etags, ecorr, etypes, instruction=self.instruction,
            content_head=self.content_head, content_tail=self.content_tail,
        )

        try:
            result = extract_structured_validated(
                self.provider, prompt, METADATA_SCHEMA
            )
        except Exception as e:
            # Account for any spend incurred across the failed attempts (I3).
            spent = getattr(e, "spent", 0.0)
            if spent:
                self.spend.add(spent)
            raise
        self.spend.add(result.cost)
        return result.data, result.cost

    # -- write-back with reuse-first + new-taxonomy flagging (I4/I5) -------
    def apply_metadata(self, doc, meta):
        """Merge-not-clobber tags; reuse existing taxonomy; flag new; advance
        ai_stage=metadata_done. Returns whether any taxonomy was created.
        Mirrors stage2.apply_metadata exactly."""
        r = self.resolver
        tax = self.taxonomy
        created_anything = False
        body = {}

        title = (meta.get("title") or "").strip()
        if title:
            body["title"] = title

        corr_name = (meta.get("correspondent") or "").strip()
        if corr_name:
            cid, created = tax.correspondent_id(corr_name)
            created_anything |= created
            if cid:
                body["correspondent"] = cid

        type_name = (meta.get("document_type") or "").strip()
        if type_name:
            tid, created = tax.doc_type_id(type_name)
            created_anything |= created
            if tid:
                body["document_type"] = tid

        tag_ids = set(doc.get("tags") or [])
        for tname in meta.get("tags") or []:
            gid, created = tax.tag_id(tname)
            created_anything |= created
            if gid:
                tag_ids.add(gid)
        if created_anything:
            tag_ids.add(self.new_tax_tag_id)
        body["tags"] = sorted(tag_ids)

        keep = [
            cf
            for cf in (doc.get("custom_fields") or [])
            if cf["field"] != r.stage_field_id()
        ]
        keep.append(
            {
                "field": r.stage_field_id(),
                "value": r.stage_option_for_role(config.STAGE_METADATA_DONE),
            }
        )
        body["custom_fields"] = keep

        self.client.request(
            "PATCH", f"{self.client.base}/api/documents/{doc['id']}/", json=body
        )
        return created_anything

    # -- per-document (mirrors stage2.process_one) -------------------------
    def process_one(self, doc, *, dry_run, on_record=None):
        """Extract + apply (or, in dry-run, propose) metadata for one doc.

        `on_record` (prompt 013, OPTIONAL) is a best-effort observational hook:
        when provided it is called with the structured proposed/applied metadata
        (`kind`, before-doc, `meta`, `dry_run`, `created`) so a caller can record a
        field-level activity diff. It is invoked inside a try/except so a recorder
        failure can NEVER affect processing; when omitted, behavior is
        byte-identical to before. It is NEVER called for a spend-cap skip/no-op."""
        if self.spend.should_abort():
            return ("spend_cap", doc["id"], "skipped (cap)", 0.0)

        self.safety.snapshot(doc)
        # Capture the BEFORE-state (title/correspondent/document_type/tags/
        # custom_fields) up front — apply_metadata mutates the live doc dict, so
        # the observational recorder must see the pre-write values (prompt 013).
        before_doc = {
            "id": doc.get("id"),
            "title": doc.get("title"),
            "correspondent": doc.get("correspondent"),
            "document_type": doc.get("document_type"),
            "tags": list(doc.get("tags") or []),
            "custom_fields": [dict(cf) for cf in (doc.get("custom_fields") or [])],
        }
        meta, cost = self._extract(doc)

        if dry_run:
            self._maybe_record(on_record, before_doc, meta, dry_run=True, created=None)
            new_bits = []
            if meta.get("correspondent_is_new"):
                new_bits.append(f"NEW corr={meta['correspondent']}")
            if meta.get("document_type_is_new"):
                new_bits.append(f"NEW type={meta['document_type']}")
            if meta.get("new_tags"):
                new_bits.append(f"NEW tags={meta['new_tags']}")
            flag = ("  [" + "; ".join(new_bits) + "]") if new_bits else ""
            return (
                "dry",
                doc["id"],
                f"title='{meta.get('title')}' corr='{meta.get('correspondent')}' "
                f"type='{meta.get('document_type')}' tags={meta.get('tags')}{flag}",
                cost,
            )

        created = self.apply_metadata(doc, meta)
        self._maybe_record(on_record, before_doc, meta, dry_run=False, created=created)
        return (
            "done" + ("_new_tax" if created else ""),
            doc["id"],
            f"title='{meta.get('title')}'",
            cost,
        )

    @staticmethod
    def _maybe_record(on_record, doc, meta, *, dry_run, created):
        """Invoke the optional activity hook, best-effort. Never raises."""
        if on_record is None:
            return
        try:
            on_record(doc=doc, meta=meta, dry_run=dry_run, created=created)
        except Exception:  # noqa: BLE001 — observational; never affect processing
            pass
