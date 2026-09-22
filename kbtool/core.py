"""Parsers, atomic SQLite packages and hybrid retrieval."""
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import uuid
import zipfile
from xml.etree import ElementTree as ET

import numpy as np

from .embedding import check_cancel, sha256

SUPPORTED = {'.txt', '.md', '.rst', '.docx', '.pdf'}
IGNORED = {'.git', '.venv', 'node_modules', '__pycache__', 'models', 'output',
           'build', 'dist', 'evaluation', 'tests'}
PRIVATE = {'credentials', 'secrets', 'credentials.txt', 'secrets.txt'}
FORMAT = 'wenlu-kb-sqlite-v1'
MAX_CHUNKS = 50000


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
            raise ValueError('支持 TXT、Markdown、RST、DOCX 和可提取文字的 PDF。')
        return source.parent, [source], []
    files, skipped = [], []
    for current, directories, names in os.walk(source, followlinks=False):
        check_cancel(cancel)
        directories[:] = sorted(d for d in directories if d.lower() not in IGNORED
                                and not d.startswith('.') and not (Path(current) / d).is_symlink()
                                and not (Path(current) / d).is_junction())
        for name in sorted(names):
            path = Path(current) / name
            if path.is_symlink() or name.startswith('.') or name.lower() in PRIVATE:
                skipped.append({'path': path.relative_to(source).as_posix(), 'reason': '隐藏、私密或链接文件'})
            elif path.suffix.lower() in SUPPORTED:
                files.append(path)
                if len(files) > 1000:
                    raise ValueError('单个知识包最多 1000 个文件，请按客户或项目拆分。')
            else:
                skipped.append({'path': path.relative_to(source).as_posix(), 'reason': '不支持的文件类型'})
    if not files:
        raise ValueError('没有找到支持的资料文件。evaluation、tests 等目录默认不入库。')
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
    connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute('PRAGMA query_only=ON')
        manifest = json.loads(connection.execute("SELECT value FROM metadata WHERE key='manifest'").fetchone()[0])
        if manifest.get('format') != FORMAT:
            raise ValueError('不支持的知识包格式。')
        return connection, manifest
    except Exception:
        connection.close()
        raise


def inspect_package(path):
    connection, manifest = open_package(path)
    try:
        if connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('知识包完整性检查失败。')
        count = connection.execute('SELECT count(*) FROM chunks').fetchone()[0]
        if count != manifest['chunk_count']:
            raise ValueError('片段数量与清单不一致。')
        return manifest
    finally:
        connection.close()


def build(source, output, customer_id, encoder, name='客户知识库', chunk_tokens=320,
          overlap=48, cancel=None, progress=print, urls=None):
    from .web_sources import fetch_documents, normalize_urls
    urls = normalize_urls(urls)
    if not source and not urls:
        raise ValueError('请选择本地资料或填写网页链接。')
    if not customer_id.strip() or not name.strip():
        raise ValueError('客户 ID 和知识库名称不能为空。')
    if not 64 <= chunk_tokens <= 440 or not 0 <= overlap < chunk_tokens // 2:
        raise ValueError('切块参数无效。')
    output = Path(output).resolve()
    if output.suffix.lower() != '.wlkb':
        raise ValueError('输出文件扩展名必须为 .wlkb。')
    output.parent.mkdir(parents=True, exist_ok=True)
    lock = output.with_suffix('.wlkb.lock')
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise ValueError('该输出正在构建；若上次进程异常退出，请确认无构建任务后移除对应 .lock 文件。') from exc
    os.close(descriptor)
    temporary = None
    try:
        base, files, skipped = discover(source, cancel) if source else (None, [], [])
        documents, chunks, errors = [], [], []
        for index, path in enumerate(files, 1):
            check_cancel(cancel)
            relative = path.relative_to(base).as_posix()
            progress(f'正在解析：{relative}')
            try:
                before = sha256(path)
                parsed = parse_document(path, cancel)
                if sha256(path) != before:
                    raise ValueError('读取过程中资料发生变化，请重试。')
                parts = make_chunks(parsed, relative, encoder, chunk_tokens, overlap)
                if not parts:
                    raise ValueError('文件没有可索引正文。')
                documents.append({'source': relative, 'sha256': before, 'chunks': len(parts),
                                  'location_kind': 'page_extracted_lines' if path.suffix.lower() == '.pdf'
                                  else 'extracted_lines' if path.suffix.lower() == '.docx' else 'source_lines'})
                chunks.extend(parts)
                if len(chunks) > MAX_CHUNKS:
                    raise ValueError(f'片段总数超过 {MAX_CHUNKS}，请拆分。')
                progress(f'解析完成 {index}/{len(files)}：{relative}')
            except (OSError, ValueError, UnicodeError, zipfile.BadZipFile, KeyError, ET.ParseError) as exc:
                errors.append({'source': relative, 'error': str(exc)})
        if errors:
            raise ValueError('存在无法完整处理的资料，未发布知识包：\n' + json_text(errors))
        sources = {doc['source'] for doc in documents}
        for page in fetch_documents(urls, cancel, progress) if urls else ():
            if page.source in sources:
                raise ValueError('网页来源与本地文件重名，请重命名本地 web 目录后重试。')
            parts = make_chunks(text_blocks(page.text, page.metadata['title']), page.source,
                                encoder, chunk_tokens, overlap)
            if not parts:
                raise ValueError('网页没有可索引正文：' + page.metadata['source_url'])
            documents.append(dict(page.metadata, source=page.source, sha256=digest(page.text), chunks=len(parts)))
            chunks.extend(parts)
            if len(chunks) > MAX_CHUNKS:
                raise ValueError(f'片段总数超过 {MAX_CHUNKS}，请拆分。')
        check_cancel(cancel)
        # Capture time describes this snapshot, but is not a content/configuration change.
        stable_documents = [{k: v for k, v in doc.items() if k != 'fetched_at'} for doc in documents]
        fingerprint = digest(json_text({'documents': stable_documents, 'model': encoder.signature,
                                       'chunk_tokens': chunk_tokens, 'overlap': overlap,
                                       'name': name, 'customer_id': customer_id, 'pipeline_version': 1}))
        previous, cached = None, {}
        if output.exists():
            connection, previous = open_package(output)
            try:
                if previous['customer_id'] != customer_id:
                    raise ValueError('该输出属于其他客户，拒绝覆盖。请选择新的输出文件。')
                if previous['fingerprint'] == fingerprint:
                    progress('输入与构建配置未变化，保留现有知识包。')
                    return dict(previous, unchanged=True)
                if previous['embedding'] == encoder.signature:
                    for row in connection.execute('SELECT content_hash, vector FROM chunks'):
                        vector = np.frombuffer(row['vector'], dtype='<f4').copy()
                        if vector.shape == (encoder.dimension,) and np.isfinite(vector).all():
                            cached[row['content_hash']] = vector
            finally:
                connection.close()
        missing = {item['content_hash']: item['embedding_text'] for item in chunks
                   if item['content_hash'] not in cached}
        progress(f'共 {len(chunks)} 个片段；需生成 {len(missing)} 个不同向量。')
        vectors = encoder.encode(list(missing.values()), cancel=cancel, progress=progress)
        cached.update(zip(missing, vectors))
        manifest = {
            'format': FORMAT, 'package_id': previous['package_id'] if previous else str(uuid.uuid4()),
            'version': previous['version'] + 1 if previous else 1,
            'customer_id': customer_id, 'name': name, 'fingerprint': fingerprint,
            'built_at': datetime.now(timezone.utc).isoformat(), 'embedding': encoder.signature,
            'chunking': {'tokens': chunk_tokens, 'overlap': overlap, 'pipeline_version': 1},
            'documents': documents, 'skipped': skipped, 'chunk_count': len(chunks),
            'new_vectors': len(missing), 'reused_chunks': sum(c['content_hash'] not in missing for c in chunks),
            'keyword_tokenizer': 'chinese-bigram-ascii-v1',
            'limitations': ['无 OCR；含无文字页的 PDF 拒绝发布', 'DOCX 行号为提取正文行号',
                            '原文和向量未加密，校验不等于发行签名', '不自动生成个人经历或回答',
                            '需使用支持 .wlkb 知识包的闻录版本'],
        }
        if urls:
            manifest['limitations'].append('网页为抓取时的正文快照；动态页面可通过后台浏览器加载，不登录、不自动更新；行号为提取正文行号')
        progress('写入知识包…')
        handle, temp_name = tempfile.mkstemp(prefix=output.name + '.', suffix='.tmp', dir=output.parent)
        os.close(handle)
        temporary = Path(temp_name)
        connection = sqlite3.connect(temporary)
        try:
            connection.executescript('''
                CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE chunks(id TEXT PRIMARY KEY, source TEXT, section TEXT, page INTEGER,
                    line_start INTEGER, line_end INTEGER, text TEXT, content_hash TEXT, vector BLOB);
                CREATE VIRTUAL TABLE keywords USING fts5(terms, tokenize='unicode61');
            ''')
            connection.execute('INSERT INTO metadata VALUES (?,?)', ('manifest', json_text(manifest)))
            for item in chunks:
                check_cancel(cancel)
                row = connection.execute('INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?,?)',
                    (item['id'], item['source'], item['section'], item['page'], item['line_start'],
                     item['line_end'], item['text'], item['content_hash'],
                     np.asarray(cached[item['content_hash']], dtype='<f4').tobytes()))
                connection.execute('INSERT INTO keywords(rowid,terms) VALUES (?,?)',
                                   (row.lastrowid, ' '.join(terms(item['embedding_text']))))
            connection.commit()
        finally:
            connection.close()
        inspect_package(temporary)
        check_cancel(cancel)
        temporary.replace(output)
        temporary = None
        # Report is derived from the package; package publication is the authoritative result.
        report = dict(manifest, package_sha256=sha256(output))
        try:
            output.with_suffix('.report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        except OSError as exc:
            progress(f'知识包已生成，但外部报告写入失败：{exc}；可以用 inspect 查看包内报告。')
        progress(f'构建完成：{output.name}，版本 {manifest["version"]}')
        return manifest
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        lock.unlink(missing_ok=True)


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
        rows = connection.execute('SELECT rowid,* FROM chunks ORDER BY rowid').fetchall()
        if not rows or len(rows) > MAX_CHUNKS:
            raise ValueError('知识包为空或超出片段限制。')
        rowmap = {row['rowid']: row for row in rows}
        semantic, keyword, similarities = [], [], {}
        if mode != 'keyword':
            matrix = np.stack([np.frombuffer(row['vector'], dtype='<f4') for row in rows])
            if matrix.shape != (len(rows), encoder.dimension) or not np.isfinite(matrix).all():
                raise ValueError('知识包向量损坏。')
            query_vector = encoder.encode([query], query=True, cancel=cancel)[0]
            scores = matrix @ query_vector
            order = np.argsort(-scores, kind='stable')[:max(20, top_k)]
            semantic = [rows[i]['rowid'] for i in order]
            similarities = {rows[i]['rowid']: float(scores[i]) for i in order}
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
            row = rowmap[row_id]
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
