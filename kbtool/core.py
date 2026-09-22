"""Parsers, atomic SQLite packages and hybrid retrieval."""
from dataclasses import dataclass, asdict
from pathlib import Path
import hashlib
import json
import os
import re
import sqlite3
import zipfile
from xml.etree import ElementTree as ET

from .embedding import check_cancel, sha256
from .storage import MAX_CHUNKS, MAX_SIZE, MAX_MANIFEST, semantic_top

DOCUMENT_EXTENSIONS = {'.txt', '.md', '.rst', '.docx', '.pdf'}
# Source files are indexed as text.  No language-specific parser is needed
# for retrieval; retaining the original path and line ranges is more useful
# than trying to reduce code to a generic document representation.
CODE_EXTENSIONS = {
    '.vue', '.js', '.jsx', '.mjs', '.cjs', '.ts', '.tsx',
    '.html', '.htm', '.css', '.scss', '.less',
    '.c', '.h', '.cc', '.cpp', '.cxx', '.hh', '.hpp', '.hxx',
    '.cs', '.java', '.kt', '.kts', '.go', '.rs', '.py', '.rb', '.php',
    '.swift', '.dart', '.lua', '.sh', '.bash', '.zsh', '.fish',
    '.ps1', '.bat', '.cmd', '.sql', '.proto', '.graphql', '.gql',
    '.xml', '.yaml', '.yml', '.toml', '.ini', '.cfg',
}
SUPPORTED = DOCUMENT_EXTENSIONS | CODE_EXTENSIONS
IGNORED = {'.git', '.venv', 'node_modules', '__pycache__', 'models', 'output',
           'build', 'dist', 'bin', 'obj', 'coverage', 'target', 'evaluation', 'tests'}
PRIVATE = {'credentials', 'secrets', 'credentials.txt', 'secrets.txt'}
FORMAT = 'wenlu-kb-sqlite-v1'
MAX_FILES = 100_000
MAX_SKIPPED_DETAILS = 1000


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def json_text(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


@dataclass
class Block:
    text: str
    section: str
    line_start: int
    line_end: int
    page: int | None = None


def text_blocks(text, title, page=None):
    """Keep paragraphs intact and propagate the complete Markdown heading path."""
    lines = text.splitlines()
    headings, pending = [], []
    start = 1
    blocks = []

    def flush(end):
        if pending:
            blocks.append(Block('\n'.join(pending), ' / '.join(h[1] for h in headings) or title,
                                start, end, page))
            pending.clear()

    for number, line in enumerate(lines, 1):
        heading = re.match(r'^(#{1,6})\s+(.+)', line)
        if heading:
            flush(number - 1)
            level, name = len(heading[1]), heading[2].strip()
            headings[:] = [h for h in headings if h[0] < level]
            headings.append((level, name))
        elif not line.strip():
            flush(number - 1)
        else:
            if not pending:
                start = number
            pending.append(line)
    flush(len(lines))
    return blocks


def parse_document(path, cancel=None):
    path = Path(path)
    check_cancel(cancel)
    if path.stat().st_size > 25_000_000:
        raise ValueError('文件超过 25 MB，请拆分后导入。')
    if path.suffix.lower() == '.pdf':
        from pypdf import PdfReader
        reader = PdfReader(path)
        if reader.is_encrypted:
            raise ValueError('PDF 已加密。')
        if len(reader.pages) > 200:
            raise ValueError('PDF 超过 200 页，请拆分。')
        blocks, total = [], 0
        for page_number, page in enumerate(reader.pages, 1):
            check_cancel(cancel)
            text = page.extract_text() or ''
            if not text.strip():
                raise ValueError(f'PDF 第 {page_number} 页无可提取文字；请确认空白页或先做 OCR。')
            total += len(text)
            if total > 2_000_000:
                raise ValueError('提取文字超过 200 万字符。')
            blocks.extend(text_blocks(text, path.stem, page_number))
        return blocks
    if path.suffix.lower() == '.docx':
        with zipfile.ZipFile(path) as archive:
            info = archive.getinfo('word/document.xml')
            if info.file_size > 10_000_000:
                raise ValueError('DOCX 解压正文超过 10 MB。')
            tree = ET.fromstring(archive.read(info))
        ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
        paragraphs = []
        for p in tree.findall('.//w:body//w:p', ns):
            text = ''.join(t.text or '' for t in p.findall('.//w:t', ns))
            style = p.find('./w:pPr/w:pStyle', ns)
            style_value = style.get('{' + ns['w'] + '}val', '') if style is not None else ''
            match = re.search(r'(?:Heading|标题)([1-6])', style_value, re.I)
            paragraphs.append('#' * int(match[1]) + ' ' + text if match else text)
        text = '\n\n'.join(paragraphs)
    else:
        raw = path.read_bytes()
        if raw.startswith((b'\xff\xfe', b'\xfe\xff')):
            text = raw.decode('utf-16')
        else:
            if b'\0' in raw:
                raise ValueError('文本文件包含二进制内容。')
            try:
                text = raw.decode('utf-8-sig')
            except UnicodeDecodeError:
                text = raw.decode('gb18030')
    if len(text) > 2_000_000:
        raise ValueError('提取文字超过 200 万字符。')
    return text_blocks(text, path.stem)


def discover(source, cancel=None):
    source = Path(source).resolve(strict=True)
    if source.is_file():
        if source.suffix.lower() not in SUPPORTED:
            raise ValueError('支持 TXT、Markdown、RST、DOCX、PDF 和常见源码文件。')
        return source.parent, [source], []
    files, skipped = [], []
    skipped_count = 0
    def skip(path, reason):
        nonlocal skipped_count
        skipped_count += 1
        if len(skipped) < MAX_SKIPPED_DETAILS:
            skipped.append({'path': path.relative_to(source).as_posix(), 'reason': reason})
    for current, directories, names in os.walk(source, followlinks=False):
        check_cancel(cancel)
        directories[:] = sorted(d for d in directories if d.lower() not in IGNORED
                                and not d.startswith('.') and not (Path(current) / d).is_symlink()
                                and not (Path(current) / d).is_junction())
        for name in sorted(names):
            path = Path(current) / name
            if path.is_symlink() or name.startswith('.') or name.lower() in PRIVATE:
                skip(path, '隐藏、私密或链接文件')
            elif path.suffix.lower() in SUPPORTED:
                files.append(path)
                if len(files) > MAX_FILES:
                    raise ValueError('单个知识包最多 10 万个文件，请按客户或项目拆分。')
            else:
                skip(path, '不支持的文件类型')
    if not files:
        raise ValueError('没有找到支持的资料文件。evaluation、tests 等目录默认不入库。')
    if skipped_count > len(skipped):
        skipped.append({'path': '', 'reason': '其余跳过文件仅记录数量，避免清单膨胀',
                        'omitted_count': skipped_count - len(skipped)})
    return source, files, skipped


def make_chunks(blocks, relative_path, encoder, chunk_tokens=320, overlap=48):
    if not 64 <= chunk_tokens <= 440 or not 0 <= overlap < chunk_tokens // 2:
        raise ValueError('片段长度应为 64—440 token，重叠应小于片段长度的一半。')
    groups = []
    # Pack adjacent paragraphs within the same section/page, avoiding tiny fragments.
    for block in blocks:
        if groups and groups[-1].section == block.section and groups[-1].page == block.page:
            combined = groups[-1].text + '\n\n' + block.text
            if encoder.token_count(combined) <= chunk_tokens:
                groups[-1].text = combined
                groups[-1].line_end = block.line_end
                continue
        groups.append(Block(**asdict(block)))
    chunks = []
    for group in groups:
        # Token-bounded title prevents long headings from overflowing the model.
        title = encoder.windows(group.section, 64, 0)[0][0] if group.section else ''
        prefix = title + '\n'
        budget = min(chunk_tokens, encoder.max_tokens - 2 - encoder.token_count(prefix) - 4)
        for content, start, stop in encoder.windows(group.text, budget, overlap):
            text = prefix + content
            # The embedding input is the exact searchable text, never an LLM summary.
            if encoder.token_count(text) + 2 > encoder.max_tokens:
                raise ValueError('切块超过模型长度，构建已停止。')
            # Paragraph packing can change blank-line counts; cite the containing block range.
            item = {'source': relative_path, 'section': group.section, 'page': group.page,
                    'line_start': group.line_start, 'line_end': group.line_end,
                    'text': content, 'embedding_text': text, 'content_hash': digest(text)}
            item['id'] = digest(json_text([relative_path, group.section, group.page,
                                          group.line_start, start, stop, content]))
            chunks.append(item)
    return chunks


def terms(text):
    """Deterministic Chinese bigrams + words/identifiers; same rules for query and index."""
    result = []
    for token in re.findall(r'[\u3400-\u9fff]+|[a-zA-Z0-9_]+', text.lower()):
        if '\u3400' <= token[0] <= '\u9fff':
            result.extend(token[i:i + 2] for i in range(len(token) - 1))
            if len(token) == 1:
                result.append(token)
        else:
            result.append(token)
    return list(dict.fromkeys(result))


def open_package(path):
    path = Path(path).resolve(strict=True)
    if path.stat().st_size > MAX_SIZE:
        raise ValueError('知识包超过 8 GiB，请按项目拆分。')
    connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute('PRAGMA query_only=ON')
        connection.execute('PRAGMA cache_size=-8192')
        connection.execute('PRAGMA temp_store=FILE')
        row = connection.execute("SELECT value FROM metadata WHERE key='manifest' AND length(CAST(value AS BLOB))<=?", (MAX_MANIFEST,)).fetchone()
        if not row:
            raise ValueError('知识包清单缺失或超过 64 MiB。')
        manifest = json.loads(row[0])
        if manifest.get('format') != FORMAT:
            raise ValueError('不支持的知识包格式。')
        return connection, manifest
    except Exception:
        connection.close()
        raise


def inspect_package(path, cancel=None):
    connection, manifest = open_package(path)
    try:
        connection.set_progress_handler(lambda: int(bool(cancel and cancel.is_set())), 1000)
        check_cancel(cancel)
        if connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('知识包完整性检查失败。')
        count = connection.execute('SELECT count(*) FROM chunks').fetchone()[0]
        if count != manifest['chunk_count'] or not 1 <= count <= MAX_CHUNKS:
            raise ValueError('片段数量与清单不一致。')
        return manifest
    except sqlite3.OperationalError:
        check_cancel(cancel)
        raise
    finally:
        connection.close()


def build(source, output, customer_id, encoder, name='客户知识库', chunk_tokens=320,
          overlap=48, cancel=None, progress=print, urls=None):
    from .build_pipeline import build as streamed_build
    return streamed_build(source, output, customer_id, encoder, name, chunk_tokens,
                          overlap, cancel, progress, urls)


def search(package, query, encoder, top_k=5, mode='hybrid', customer_id=None, cancel=None):
    if not query.strip() or len(query) > 2000 or not 1 <= top_k <= 20:
        raise ValueError('请输入不超过 2000 字的问题，返回数量应为 1—20。')
    if mode not in {'hybrid', 'semantic', 'keyword'}:
        raise ValueError('未知检索模式。')
    connection, manifest = open_package(package)
    try:
        if customer_id is not None and manifest['customer_id'] != customer_id:
            raise ValueError('客户 ID 与知识包不匹配。')
        if manifest['embedding'] != encoder.signature:
            raise ValueError('查询模型与知识包模型不匹配，请使用同一模型版本。')
        if not 1 <= manifest['chunk_count'] <= MAX_CHUNKS:
            raise ValueError('知识包为空或超出片段限制。')
        semantic, keyword, similarities = [], [], {}
        if mode != 'keyword':
            query_vector = encoder.encode([query], query=True, cancel=cancel)[0]
            semantic, similarities = semantic_top(connection, query_vector, max(20, top_k), lambda: check_cancel(cancel))
        if mode != 'semantic':
            tokens = terms(query)[:96]
            if tokens:
                expression = ' OR '.join('"' + token.replace('"', '""') + '"' for token in tokens)
                keyword = [row[0] for row in connection.execute(
                    'SELECT rowid FROM keywords WHERE keywords MATCH ? ORDER BY bm25(keywords),rowid LIMIT 20',
                    (expression,))]
        fused = {}
        for ranking in (semantic, keyword):
            for rank, row_id in enumerate(ranking, 1):
                fused[row_id] = fused.get(row_id, 0) + 1 / (60 + rank)
        order = sorted(fused, key=lambda row_id: (-fused[row_id], row_id))[:top_k]
        check_cancel(cancel)
        results = []
        web_sources = {doc['source']: doc for doc in manifest.get('documents', []) if doc.get('source_url')}
        for row_id in order:
            row = connection.execute('SELECT * FROM chunks WHERE rowid=?', (row_id,)).fetchone()
            item = {key: row[key] for key in ('id', 'source', 'section', 'page', 'line_start', 'line_end', 'text')}
            if row['source'] in web_sources:
                doc = web_sources[row['source']]
                item.update({key: doc[key] for key in ('source_url', 'final_url', 'title', 'fetched_at')})
            item.update(score=fused[row_id], cosine=similarities.get(row_id),
                        semantic_rank=semantic.index(row_id) + 1 if row_id in semantic else None,
                        keyword_rank=keyword.index(row_id) + 1 if row_id in keyword else None)
            results.append(item)
        return {'customer_id': manifest['customer_id'], 'query': query, 'mode': mode, 'matches': results,
                'note': '检索排名不代表事实正确或存在答案；无答案问题也可能返回相关片段。'}
    finally:
        connection.close()
