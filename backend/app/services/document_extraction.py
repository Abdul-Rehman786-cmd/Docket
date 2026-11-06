"""Bounded text extraction. Binary parsers run in disposable, memory-limited processes."""

import asyncio
from io import BytesIO
import json
import logging
from pathlib import PurePosixPath
import subprocess
import sys
import xml.etree.ElementTree as ET
from zipfile import ZipFile

from app.core.config import BACKEND_DIR

MAX_UPLOAD = 8 * 1024 * 1024
MAX_TEXT = 262144  # Matches the existing database source_text byte constraint.
MAX_EXPANDED = 32 * 1024 * 1024
MAX_PART = 4 * 1024 * 1024
MAX_PAGES = 100
MAX_SLIDES = 200
FORMATS = {'.md': 'markdown', '.txt': 'text', '.pdf': 'pdf', '.docx': 'docx', '.pptx': 'pptx'}
_slots = asyncio.Semaphore(2)


class ExtractionError(ValueError):
    """A fixed diagnostic category, never parser exception text."""


def checked_text(text):
    if not text.strip():
        raise ExtractionError('no_searchable_text')
    if '\x00' in text:
        raise ExtractionError('invalid_text')
    if len(text) > MAX_TEXT or len(text.encode('utf-8')) > MAX_TEXT:
        raise ExtractionError('extracted_text_too_large')
    return text


def xml(raw):
    # Restrict XML to UTF-8, reject DTD/entities before ElementTree sees them.
    text = raw.decode('utf-8-sig')
    if '<!DOCTYPE' in text.upper() or '<!ENTITY' in text.upper():
        raise ExtractionError('unsafe_document')
    return ET.fromstring(text)


def office_text(raw, suffix):
    if raw.startswith(b'\xd0\xcf\x11\xe0'):
        raise ExtractionError('encrypted_document')
    if not raw.startswith(b'PK\x03\x04'):
        raise ExtractionError('invalid_document')
    with ZipFile(BytesIO(raw)) as archive:
        parts = archive.infolist()
        if len(parts) > 1024 or sum(p.file_size for p in parts) > MAX_EXPANDED:
            raise ExtractionError('archive_expansion_limit')
        names = [p.filename for p in parts]
        if len(set(names)) != len(names):
            raise ExtractionError('invalid_document')
        for part in parts:
            name = part.filename
            if part.flag_bits & 1:
                raise ExtractionError('encrypted_document')
            if part.file_size>MAX_PART:
                raise ExtractionError('archive_expansion_limit')
            if (part.compress_type not in (0, 8)
                or name.startswith('/') or '\\' in name or '..' in PurePosixPath(name).parts
                or ':' in name or (part.external_attr >> 16) & 0o170000 == 0o120000):
                raise ExtractionError('unsafe_document')
            if 'vbaproject' in name.lower() or '/embeddings/' in name.lower() or '/activex/' in name.lower():
                raise ExtractionError('unsafe_document')
        # Read/CRC-check every part with a hard per-member bound; never extract to disk.
        files = {}
        for part in parts:
            with archive.open(part) as stream:
                data = stream.read(MAX_PART + 1)
            if len(data) != part.file_size or len(data) > MAX_PART:
                raise ExtractionError('archive_expansion_limit')
            if part.filename.endswith(('.xml', '.rels')) or part.filename == '[Content_Types].xml':
                files[part.filename] = xml(data)
        types = files.get('[Content_Types].xml')
        root_rels = files.get('_rels/.rels')
        if (types is None or root_rels is None
            or types.tag!='{http://schemas.openxmlformats.org/package/2006/content-types}Types'
            or root_rels.tag!='{http://schemas.openxmlformats.org/package/2006/relationships}Relationships'):
            raise ExtractionError('invalid_document')
        required = 'word/document.xml' if suffix == '.docx' else 'ppt/presentation.xml'
        content_type = ('application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml'
                        if suffix == '.docx' else 'application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml')
        if not any(e.get('PartName') == '/' + required and e.get('ContentType') == content_type for e in types):
            raise ExtractionError('invalid_document')
        if not any(e.get('Type', '').endswith('/officeDocument') and e.get('Target', '').lstrip('/') == required
                   and e.get('TargetMode') != 'External' for e in root_rels):
            raise ExtractionError('invalid_document')
        for tree in files.values():
            for node in tree.iter():
                if node.tag.rsplit('}',1)[-1] in ('documentProtection','writeProtection','modifyVerifier'):
                    raise ExtractionError('encrypted_document')
                if 'macroEnabled' in node.get('ContentType', ''):
                    raise ExtractionError('unsafe_document')
                # External hyperlinks may be present: they are neither visited nor resolved.
                if node.tag.endswith('}Relationship') and node.get('TargetMode') == 'External' and not node.get('Type', '').endswith('/hyperlink'):
                    raise ExtractionError('unsafe_document')
        if suffix == '.docx':
            tree = files[required]
            w = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
            if tree.tag != w + 'document':
                raise ExtractionError('invalid_document')
            if any(n.tag in (w + 'altChunk', w + 'object') for n in tree.iter()):
                raise ExtractionError('unsafe_document')
            if sum(1 for _ in tree.iter(w + 'p')) > 10000:
                raise ExtractionError('page_slide_limit')
            return checked_text('\n'.join(''.join((n.text or '') if n.tag == w + 't' else '\t' if n.tag == w + 'tab' else '\n'
                for n in paragraph.iter() if n.tag in (w + 't', w + 'tab', w + 'br'))
                for paragraph in tree.iter(w + 'p')))
        p = '{http://schemas.openxmlformats.org/presentationml/2006/main}'
        a = '{http://schemas.openxmlformats.org/drawingml/2006/main}'
        rel = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id'
        tree = files[required]
        if tree.tag != p + 'presentation':
            raise ExtractionError('invalid_document')
        slides = list(tree.iter(p + 'sldId'))
        if len(slides) > MAX_SLIDES:
            raise ExtractionError('page_slide_limit')
        relationships = files.get('ppt/_rels/presentation.xml.rels')
        if relationships is None:
            raise ExtractionError('invalid_document')
        targets = {r.get('Id'): r.get('Target') for r in relationships
                   if r.get('Type', '').endswith('/slide') and r.get('TargetMode') != 'External'}
        texts = []
        for slide in slides:
            target = targets.get(slide.get(rel), '')
            if not target.startswith('slides/') or '..' in PurePosixPath(target).parts:
                raise ExtractionError('invalid_document')
            content = files.get('ppt/' + target)
            if content is None or content.tag != p + 'sld':
                raise ExtractionError('invalid_document')
            texts.append('\n'.join(''.join(n.text or '' for n in paragraph.iter(a + 't')) for paragraph in content.iter(a + 'p')))
            if len('\n'.join(texts).encode('utf-8')) > MAX_TEXT:
                raise ExtractionError('extracted_text_too_large')
        return checked_text('\n'.join(texts))


def extract_sync(raw, suffix):
    if len(raw) > MAX_UPLOAD:
        raise ExtractionError('attachment_too_large')
    if suffix not in FORMATS:
        raise ExtractionError('unsupported_format')
    try:
        if suffix in ('.md', '.txt'):
            if raw.lstrip().startswith((b'%PDF-', b'PK\x03\x04', b'\xd0\xcf\x11\xe0')):
                raise ExtractionError('invalid_document')
            return checked_text(raw.decode('utf-8'))
        if suffix in ('.docx', '.pptx'):
            return office_text(raw, suffix)
        if not raw.startswith(b'%PDF-'):
            raise ExtractionError('invalid_document')
        from pypdf import PdfReader, apply_configuration
        with apply_configuration(zlib_maximum_output_length=MAX_PART, maximum_declared_stream_length=MAX_PART,
                array_based_stream_maximum_output_length=MAX_PART, lzw_maximum_output_length=MAX_PART,
                run_length_maximum_output_length=MAX_PART, brotli_maximum_output_length=MAX_PART,
                jbig2_maximum_output_length=MAX_PART, jbig2dec_binary=None):
            from pypdf.errors import FileNotDecryptedError, DependencyError
            try:
                reader = PdfReader(BytesIO(raw), strict=True)
            except (FileNotDecryptedError, DependencyError):
                raise ExtractionError('encrypted_document') from None
            if reader.is_encrypted:
                raise ExtractionError('encrypted_document')
            if len(reader.pages) > MAX_PAGES:
                raise ExtractionError('page_slide_limit')
            texts = []
            for page in reader.pages:
                content = page.get_contents()
                if content is not None and len(content.get_data()) > MAX_PART:
                    raise ExtractionError('archive_expansion_limit')
                texts.append(page.extract_text() or '')
                if len('\n'.join(texts).encode('utf-8')) > MAX_TEXT:
                    raise ExtractionError('extracted_text_too_large')
            return checked_text('\n'.join(texts))
    except ExtractionError:
        raise
    except ImportError:
        raise ExtractionError('parser_dependencies_missing') from None
    except MemoryError:
        raise ExtractionError('parser_resource_limit') from None
    except Exception:
        raise ExtractionError('invalid_document') from None


def memory_limit():
    """Hard address-space/process-commit bound, including parser allocations."""
    limit = 512 * 1024 * 1024
    if sys.platform != 'win32':
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
        return None
    import ctypes as c
    from ctypes import wintypes as w
    class Basic(c.Structure):
        _fields_ = [('times', c.c_longlong * 2), ('flags', w.DWORD), ('min_ws', c.c_size_t),
                    ('max_ws', c.c_size_t), ('active', w.DWORD), ('affinity', c.c_size_t),
                    ('priority', w.DWORD), ('scheduling', w.DWORD)]
    class Extended(c.Structure):
        _fields_ = [('basic', Basic), ('io', c.c_ulonglong * 6), ('process_memory', c.c_size_t),
                    ('job_memory', c.c_size_t), ('peak_process', c.c_size_t), ('peak_job', c.c_size_t)]
    kernel = c.WinDLL('kernel32', use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [c.c_void_p, w.LPCWSTR]
    kernel.CreateJobObjectW.restype = w.HANDLE
    kernel.SetInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
    job = kernel.CreateJobObjectW(None, None)
    info = Extended()
    info.basic.flags = 0x100  # JOB_OBJECT_LIMIT_PROCESS_MEMORY
    info.process_memory = limit
    if not job or not kernel.SetInformationJobObject(job, 9, c.byref(info), c.sizeof(info)) or not kernel.AssignProcessToJobObject(job, w.HANDLE(-1)):
        raise ExtractionError('parser_resource_unavailable')
    return job  # Retain the handle until process exit.


def _run_parser(raw,suffix):
    """Popen works on Windows Selector loops too (Uvicorn's reload mode)."""
    try:
        completed=subprocess.run([sys.executable,'-m','app.services.document_extraction',suffix],
            cwd=BACKEND_DIR,input=raw,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=20)
    except subprocess.TimeoutExpired:
        # subprocess.run kills and reaps the child before raising.
        raise ExtractionError('parser_timeout') from None
    except OSError:
        raise ExtractionError('parser_resource_unavailable') from None
    if completed.returncode or len(completed.stdout)>MAX_TEXT*6+1024:
        raise ExtractionError('parser_resource_limit')
    try:
        result=json.loads(completed.stdout)
        if 'error' in result:
            raise ExtractionError(result['error'])
        return checked_text(result['text'])
    except (KeyError,TypeError,json.JSONDecodeError):
        raise ExtractionError('invalid_document') from None


async def extract_text(raw, filename):
    suffix=PurePosixPath(filename).suffix.lower()
    if suffix not in FORMATS:
        raise ExtractionError('unsupported_format')
    if len(raw)>MAX_UPLOAD:
        raise ExtractionError('attachment_too_large')
    if suffix in ('.md','.txt'):
        return extract_sync(raw,suffix)
    async with _slots:
        task=asyncio.create_task(asyncio.to_thread(_run_parser,raw,suffix))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # Do not free the slot while the process still runs. Completion/timeout
            # reaps it within 20 seconds; a cancelled import never proceeds to storage.
            try:
                await task
            except Exception:
                pass
            raise


if __name__ == '__main__':
    logging.disable(logging.CRITICAL)
    try:
        job_handle = memory_limit()
        result = {'text': extract_sync(sys.stdin.buffer.read(MAX_UPLOAD + 1), sys.argv[1])}
    except ExtractionError as error:
        result = {'error': str(error)}
    except Exception:
        result = {'error': 'invalid_document'}
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode('utf-8'))
