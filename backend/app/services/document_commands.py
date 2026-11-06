"""Complete deterministic consent phrases; shared by listener and backend."""
import re


def document_command(body):
    if not isinstance(body,str):
        return None
    text=body.strip().rstrip('.!?,;:').strip()
    lower=text.casefold().replace("’", "'")
    if lower in {"yes","save","save it","yes please"}:
        return ("save",None)
    if lower in {"no","skip","cancel","don't save"}:
        return ("skip",None)
    # Exact filename qualification, not a word match inside an unrelated sentence.
    match=re.fullmatch(r"(save|skip|cancel|don't save) ([^\r\n]+\.(?:pdf|docx|pptx|md|txt))",text,re.IGNORECASE)
    if match:
        return ("save" if match[1].casefold()=="save" else "skip",match[2].casefold())
    # Compatibility for an old prompt already delivered before this upgrade.
    match=re.fullmatch(r"(save|skip) ([0-9a-f]{12})",text,re.IGNORECASE)
    return (match[1].lower(),match[2].lower()) if match else None
