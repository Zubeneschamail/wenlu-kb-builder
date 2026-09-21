import argparse
import json
import sys
from pathlib import Path

from .core import build, inspect_package, search
from .embedding import DEFAULT_MODEL_DIR, Encoder, prepare_model


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description='闻录独立知识库生成工具：本地 BGE 向量化，不上传资料。')
    parser.add_argument('--model-dir', type=Path, default=DEFAULT_MODEL_DIR)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('prepare-model', help='下载并校验固定版本的模型（约 24 MB）')
    builder = commands.add_parser('build', help='生成或增量重建 .wlkb 文件')
    builder.add_argument('--source', type=Path, required=True)
    builder.add_argument('--output', type=Path, required=True)
    builder.add_argument('--customer-id', required=True)
    builder.add_argument('--name', default='客户知识库')
    builder.add_argument('--chunk-tokens', type=int, default=320)
    builder.add_argument('--overlap', type=int, default=48)
    query = commands.add_parser('search', help='试搜知识包，输出原文和来源，不生成 AI 答案')
    query.add_argument('--package', type=Path, required=True)
    query.add_argument('--query', required=True)
    query.add_argument('--customer-id')
    query.add_argument('--top-k', type=int, default=5)
    query.add_argument('--mode', choices=['hybrid', 'semantic', 'keyword'], default='hybrid')
    info = commands.add_parser('inspect', help='检查知识包并查看清单')
    info.add_argument('--package', type=Path, required=True)
    commands.add_parser('gui', help='打开桌面操作窗口')
    args = parser.parse_args()
    log = lambda message: print(message, file=sys.stderr, flush=True)
    try:
        if args.command == 'gui':
            from .gui import launch
            launch(args.model_dir)
            return 0
        if args.command == 'prepare-model':
            prepare_model(args.model_dir, progress=log)
            result = {'ok': True, 'model_dir': str(args.model_dir)}
        elif args.command == 'inspect':
            result = inspect_package(args.package)
        else:
            log('加载本地模型…')
            encoder = Encoder(args.model_dir)
            if args.command == 'build':
                result = build(args.source, args.output, args.customer_id, encoder, name=args.name,
                               chunk_tokens=args.chunk_tokens, overlap=args.overlap, progress=log)
            else:
                result = search(args.package, args.query, encoder, args.top_k, args.mode, args.customer_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except KeyboardInterrupt:
        print('已中断。', file=sys.stderr)
        return 130
    except Exception as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
