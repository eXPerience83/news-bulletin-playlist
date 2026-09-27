from __future__ import annotations

from collections.abc import Collection
from html.parser import HTMLParser

_SUBMITTER_FORM_OVERRIDE_ATTRS = frozenset(
    {"formaction", "formmethod", "formenctype", "formtarget", "formnovalidate"}
)


def _is_same_origin_form_action(action: str) -> bool:
    return (
        action.startswith("/")
        and not action.startswith("//")
        and "\\" not in action
        and all(ord(character) >= 0x20 and ord(character) != 0x7F for character in action)
    )


def _is_effective_csrf_input(attributes: dict[str, str | None]) -> bool:
    return (
        (attributes.get("type") or "text").casefold() == "hidden"
        and bool(attributes.get("value"))
        and "disabled" not in attributes
        and "form" not in attributes
    )


class _AdminHTMLAudit(HTMLParser):
    def __init__(
        self,
        *,
        csrf_exempt_post_actions: Collection[str] = (),
    ) -> None:
        super().__init__(convert_charrefs=True)
        self.csrf_exempt_post_actions = frozenset(csrf_exempt_post_actions)
        self.errors: list[str] = []
        self.ids: set[str] = set()
        self.references: list[tuple[str, str]] = []
        self.form_stack: list[dict[str, object]] = []
        self.doctype = False
        self.html_lang: str | None = None
        self.viewport = False
        self.h1_count = 0

    def handle_decl(self, decl: str) -> None:
        if decl.casefold() == "doctype html":
            self.doctype = True

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {name: value for name, value in attrs}
        attr_names = [name for name, _value in attrs]
        if len(attr_names) != len(set(attr_names)):
            self.errors.append(f"<{tag}> contains duplicate attributes")

        element_id = attributes.get("id")
        if element_id:
            if element_id in self.ids:
                self.errors.append(f'duplicate id="{element_id}"')
            self.ids.add(element_id)

        if tag == "html":
            self.html_lang = attributes.get("lang")
        elif tag == "meta" and attributes.get("name") == "viewport":
            if attributes.get("content") == "width=device-width, initial-scale=1":
                self.viewport = True
        elif tag == "base":
            self.errors.append("<base> is not allowed in first-party rendered HTML")
        elif tag == "fieldset" and "disabled" in attributes:
            self.errors.append("disabled <fieldset> is not allowed in first-party rendered HTML")
        elif tag == "h1":
            self.h1_count += 1
        elif tag == "script":
            self.errors.append("<script> is not allowed in the server-rendered admin UI")
        elif tag == "th" and attributes.get("scope") not in {"col", "row"}:
            self.errors.append("every <th> must declare scope=col or scope=row")
        elif tag == "label" and attributes.get("for"):
            self.references.append(("label for", attributes["for"] or ""))

        labelled_by = attributes.get("aria-labelledby")
        if labelled_by:
            self.references.extend(("aria-labelledby", item) for item in labelled_by.split())

        for name, value in attrs:
            if name.startswith("on"):
                self.errors.append(f"inline event handler {name} is not allowed")
            if (
                name in {"action", "href", "src"}
                and value
                and value.lstrip().casefold().startswith("javascript:")
            ):
                self.errors.append(f"javascript: URL is not allowed in {name}")
            if name in _SUBMITTER_FORM_OVERRIDE_ATTRS:
                self.errors.append(f"{name} is not allowed in first-party rendered HTML")

        if tag == "a" and attributes.get("target") == "_blank":
            rel = set((attributes.get("rel") or "").split())
            if not {"noopener", "noreferrer"}.issubset(rel):
                self.errors.append('target="_blank" links must use rel="noopener noreferrer"')

        if tag == "form":
            if self.form_stack:
                self.errors.append("nested <form> elements are not allowed")
            self.form_stack.append(
                {
                    "method": (attributes.get("method") or "get").casefold(),
                    "action": attributes.get("action") or "",
                    "csrf": False,
                }
            )
        elif tag == "input" and self.form_stack and attributes.get("name") == "csrf_token":
            if not _is_effective_csrf_input(attributes):
                self.errors.append(
                    "csrf_token must be a non-empty enabled hidden input for its containing form"
                )
            elif self.form_stack[-1]["csrf"] is True:
                self.errors.append("form contains multiple csrf_token inputs")
            else:
                self.form_stack[-1]["csrf"] = True

    def handle_endtag(self, tag: str) -> None:
        if tag != "form":
            return
        if not self.form_stack:
            self.errors.append("closing </form> without a matching <form>")
            return
        form = self.form_stack.pop()
        method = form["method"]
        action = form["action"]
        if not isinstance(method, str) or not isinstance(action, str):
            raise AssertionError("invalid internal HTML audit state")
        if action and not _is_same_origin_form_action(action):
            self.errors.append(f"form action must remain same-origin: {action}")
        if (
            method == "post"
            and form["csrf"] is not True
            and action not in self.csrf_exempt_post_actions
        ):
            self.errors.append(f"POST form is missing csrf_token: {action}")


def assert_valid_document_html(
    document: str,
    *,
    csrf_exempt_post_actions: Collection[str] = (),
) -> None:
    """Enforce structural/security invariants on a fully rendered first-party page."""

    audit = _AdminHTMLAudit(csrf_exempt_post_actions=csrf_exempt_post_actions)
    audit.feed(document)
    audit.close()

    if audit.form_stack:
        audit.errors.append("unclosed <form> element")
    if not audit.doctype:
        audit.errors.append("missing <!doctype html>")
    if not audit.html_lang:
        audit.errors.append("<html> must declare lang")
    if not audit.viewport:
        audit.errors.append("missing mobile viewport metadata")
    if audit.h1_count != 1:
        audit.errors.append(f"expected exactly one <h1>, found {audit.h1_count}")

    for kind, target in audit.references:
        if target not in audit.ids:
            audit.errors.append(f'{kind} references missing id="{target}"')

    if audit.errors:
        raise AssertionError("Invalid rendered HTML:\n- " + "\n- ".join(audit.errors))


def assert_valid_admin_html(document: str) -> None:
    """Enforce the strict rendered-document contract for authenticated admin pages."""

    assert_valid_document_html(document)
