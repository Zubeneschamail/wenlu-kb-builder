"""Disk-spooled atomic builder; memory does not grow with the complete vector corpus."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import uuid
import zipfile
from xml.etree import ElementTree as ET

import numpy as np

from .embedding import check_cancel, sha256
from .storage import MAX_CHUNKS, MAX_SIZE, MAX_MANIFEST

ENCODE_BATCH = 64


def build(source, output, customer_id, encoder, name='客户知识库', chunk_tokens=320,
          overlap=48, cancel=None, progress=print, urls=None):
    from .core import (FORMAT, digest, discover, inspect_package, json_text, make_chunks,
                       open_package, parse_document, terms, text_blocks)
    from .web_sources import fetch_documents, normalize_urls
    urls = normalize_urls(urls)
    if not source and not urls:
        raise ValueError('请选择本地资料或填写网页链接。')
    if not 1 <= len(customer_id.strip()) <= 200 or not 1 <= len(name.strip()) <= 200:
        raise ValueError('客户 ID 和知识库名称应为 1—200 字。')
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
    temporary = work_file = connection = previous_connection = None
    try:
        check_cancel(cancel)
        previous = None
        if output.exists():
            previous_connection, previous = open_package(output)
            if previous['customer_id'] != customer_id:
                raise ValueError('该输出属于其他客户，拒绝覆盖。请选择新的输出文件。')
        for kind in ('package', 'work'):
            handle, path = tempfile.mkstemp(prefix=output.name + '.' + kind, suffix='.tmp', dir=output.parent)
            os.close(handle)
            if kind == 'package':
                temporary = Path(path)
            else:
                work_file = Path(path)
        connection = sqlite3.connect(temporary)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA cache_size=-8192')
        connection.execute('PRAGMA journal_mode=OFF')
        connection.execute('PRAGMA temp_store=FILE')
        connection.execute('ATTACH DATABASE ? AS work', (str(work_file),))
        connection.execute('PRAGMA work.journal_mode=OFF')
        connection.execute('PRAGMA work.cache_size=-4096')
        connection.executescript('''
            CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE chunks(id TEXT PRIMARY KEY, source TEXT, section TEXT, page INTEGER,
                line_start INTEGER, line_end INTEGER, text TEXT, content_hash TEXT, vector BLOB);
            CREATE INDEX chunks_content_hash ON chunks(content_hash);
            CREATE VIRTUAL TABLE keywords USING fts5(terms, tokenize='unicode61');
            CREATE TABLE work.pending(hash TEXT PRIMARY KEY, text TEXT NOT NULL, ready INTEGER DEFAULT 0);
        ''')
        documents, count = [], 0

        def store_document(blocks, relative, metadata):
            nonlocal count
            parts = make_chunks(blocks, relative, encoder, chunk_tokens, overlap)
            if not parts:
                documents.append(dict(metadata, source=relative, chunks=0,
                                      skipped_reason='没有可索引正文'))
                progress('跳过空文档：' + relative)
                return
            if count + len(parts) > MAX_CHUNKS:
                raise ValueError('片段总数超过 100 万，请按项目拆分。')
            for item in parts:
                check_cancel(cancel)
                row = connection.execute('INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?,NULL)',
                    (item['id'], item['source'], item['section'], item['page'], item['line_start'],
                     item['line_end'], item['text'], item['content_hash']))
                connection.execute('INSERT INTO keywords(rowid,terms) VALUES (?,?)',
                                   (row.lastrowid, ' '.join(terms(item['embedding_text']))))
                connection.execute('INSERT OR IGNORE INTO work.pending(hash,text) VALUES (?,?)',
                                   (item['content_hash'], item['embedding_text']))
            count += len(parts)
            documents.append(dict(metadata, source=relative, chunks=len(parts)))

        base, files, skipped = discover(source, cancel) if source else (None, [], [])
        for index, path in enumerate(files, 1):
            check_cancel(cancel)
            relative = path.relative_to(base).as_posix()
            progress(f'正在解析：{relative}')
            try:
                before = sha256(path)
                parsed = parse_document(path, cancel)
                if sha256(path) != before:
                    raise ValueError('读取过程中资料发生变化，请重试。')
                store_document(parsed, relative, {'sha256': before,
                    'location_kind': 'page_extracted_lines' if path.suffix.lower() == '.pdf'
                    else 'extracted_lines' if path.suffix.lower() == '.docx' else 'source_lines'})
            except (OSError, ValueError, zipfile.BadZipFile, KeyError, ET.ParseError) as exc:
                raise ValueError(f'存在无法完整处理的资料，未发布知识包：{relative}\n{exc}') from exc
            progress(f'解析完成 {index}/{len(files)}：{relative}')
        sources = {doc['source'] for doc in documents}
        for page in fetch_documents(urls, cancel, progress) if urls else ():
            if page.source in sources:
                raise ValueError('网页来源与本地文件重名，请重命名本地 web 目录后重试。')
            store_document(text_blocks(page.text, page.metadata['title']), page.source,
                           dict(page.metadata, sha256=digest(page.text)))
        check_cancel(cancel)
        if not count:
            raise ValueError('资料中没有可索引正文，未发布知识包。')
        stable_documents = [{k: v for k, v in doc.items() if k != 'fetched_at'} for doc in documents]
        fingerprint = digest(json_text({'documents': stable_documents, 'model': encoder.signature,
                                       'chunk_tokens': chunk_tokens, 'overlap': overlap,
                                       'name': name, 'customer_id': customer_id, 'pipeline_version': 1}))
        if previous and previous['fingerprint'] == fingerprint:
            progress('输入与构建配置未变化，保留现有知识包。')
            return dict(previous, unchanged=True)
        reused = 0
        if previous and previous['embedding'] == encoder.signature:
            progress('正在复用旧包向量…')
            for row in previous_connection.execute('SELECT content_hash,vector FROM chunks'):
                check_cancel(cancel)
                if not isinstance(row['vector'], bytes) or len(row['vector']) != 2048:
                    continue
                vector = np.frombuffer(row['vector'], dtype='<f4')
                if not np.isfinite(vector).all() or not np.isclose(np.linalg.norm(vector), 1, atol=.01):
                    continue
                updated = connection.execute('UPDATE work.pending SET ready=1 WHERE hash=? AND ready=0', (row['content_hash'],))
                if updated.rowcount:
                    reused += connection.execute('UPDATE chunks SET vector=? WHERE content_hash=?',
                                                 (row['vector'], row['content_hash'])).rowcount
        if previous_connection is not None:
            previous_connection.close()
            previous_connection = None
        missing = connection.execute('SELECT count(*) FROM work.pending WHERE ready=0').fetchone()[0]
        progress(f'共 {count} 个片段；需生成 {missing} 个不同向量。')
        done = 0
        # Keyset pagination avoids both an unbounded fetchall and repeatedly scanning ready rows.
        last = ''
        while True:
            check_cancel(cancel)
            batch = connection.execute('SELECT hash,text FROM work.pending WHERE hash>? AND ready=0 ORDER BY hash LIMIT ?',
                                       (last, ENCODE_BATCH)).fetchall()
            if not batch:
                break
            vectors = encoder.encode([row['text'] for row in batch], cancel=cancel,
                                     progress=lambda _: None)
            for row, vector in zip(batch, vectors):
                check_cancel(cancel)
                connection.execute('UPDATE chunks SET vector=? WHERE content_hash=?',
                                   (np.asarray(vector, dtype='<f4').tobytes(), row['hash']))
            done += len(batch)
            last = batch[-1]['hash']
            progress(f'向量化：{done}/{missing} 个片段')
        manifest = {
            'format': FORMAT, 'package_id': previous['package_id'] if previous else str(uuid.uuid4()),
            'version': previous['version'] + 1 if previous else 1, 'customer_id': customer_id,
            'name': name, 'fingerprint': fingerprint, 'built_at': datetime.now(timezone.utc).isoformat(),
            'embedding': encoder.signature, 'chunking': {'tokens': chunk_tokens, 'overlap': overlap, 'pipeline_version': 1},
            'documents': documents, 'skipped': skipped, 'chunk_count': count,
            'new_vectors': missing, 'reused_chunks': reused, 'keyword_tokenizer': 'chinese-bigram-ascii-v1',
            'limitations': ['无 OCR；含无文字页的 PDF 拒绝发布', 'DOCX 行号为提取正文行号',
                            '原文和向量未加密，校验不等于发行签名', '不自动生成个人经历或回答',
                            '大于旧版容量限制的知识包需更新闻录后使用'],
        }
        if urls:
            manifest['limitations'].append('网页为正文快照；不登录、不自动更新；行号为提取正文行号')
        encoded = json_text(manifest)
        if len(encoded.encode('utf-8')) > MAX_MANIFEST:
            raise ValueError('知识包清单超过 64 MiB，请按项目拆分。')
        progress('写入知识包…')
        connection.execute('INSERT INTO metadata VALUES (?,?)', ('manifest', encoded))
        connection.commit()
        connection.close()
        connection = None
        if temporary.stat().st_size > MAX_SIZE:
            raise ValueError('知识包超过 8 GiB，请按项目拆分。')
        inspect_package(temporary, cancel=cancel)
        check_cancel(cancel)
        temporary.replace(output)
        temporary = None
        try:
            report = dict(manifest, package_sha256=sha256(output))
            output.with_suffix('.report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        except OSError as exc:
            progress(f'知识包已生成，但外部报告写入失败：{exc}；可以用 inspect 查看包内报告。')
        progress(f'构建完成：{output.name}，版本 {manifest["version"]}')
        return manifest
    finally:
        if connection is not None:
            connection.close()
        if previous_connection is not None:
            previous_connection.close()
        for path in (temporary, work_file):
            if path is not None:
                path.unlink(missing_ok=True)
        lock.unlink(missing_ok=True)
