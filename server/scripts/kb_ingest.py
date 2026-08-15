#!/usr/bin/env python3
"""知识库离线入库（CLAUDE.md §4.5）。

读写分离：这个脚本写，worker 只读。每次入库生成带时间戳的新目录，
入库完成后调 POST /admin/kb/reload 原子切换，避免 Chroma 多进程并发写。

需要：pip install -e ".[kb]"

用法：
    python scripts/kb_ingest.py --src docs/kb_source --out data/kb
    curl -XPOST localhost:8000/admin/kb/reload \
         -H 'content-type: application/json' \
         -d '{"persist_dir":"data/kb/20260729-1200"}'
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.kb.store import COLLECTION  # noqa: E402

CHUNK_CHARS = 500  # 与 CLAUDE.md §12 的切片长度一致
CHUNK_OVERLAP = 60


def split(text: str, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """按段落边界优先切分，段落内超长再硬切。避免把一条路标释义切成两半。"""
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    buffer = ""

    for para in paragraphs:
        if len(buffer) + len(para) + 2 <= size:
            buffer = f"{buffer}\n\n{para}" if buffer else para
            continue
        if buffer:
            chunks.append(buffer)
        if len(para) <= size:
            buffer = para
            continue
        step = max(1, size - overlap)
        for i in range(0, len(para), step):
            piece = para[i : i + size]
            if piece.strip():
                chunks.append(piece.strip())
        buffer = ""

    if buffer:
        chunks.append(buffer)
    return chunks


def collect(src: Path) -> list[tuple[Path, str]]:
    docs = []
    for path in sorted(src.rglob("*")):
        if path.suffix.lower() in (".txt", ".md") and path.is_file():
            docs.append((path, path.read_text(encoding="utf-8")))
    return docs


def main() -> None:
    parser = argparse.ArgumentParser(description="知识库入库")
    parser.add_argument("--src", required=True, help="源文档目录（txt/md）")
    parser.add_argument("--out", default="data/kb", help="persist 目录的父目录")
    parser.add_argument("--model", default="shibing624/text2vec-base-chinese")
    parser.add_argument("--dry-run", action="store_true", help="只切片不写库")
    args = parser.parse_args()

    src = Path(args.src)
    if not src.is_dir():
        sys.exit(f"源目录不存在: {src}")

    docs = collect(src)
    if not docs:
        sys.exit(f"{src} 下没有 txt/md 文档")

    manifest = {}
    all_chunks: list[str] = []
    all_ids: list[str] = []
    all_meta: list[dict] = []

    for path, text in docs:
        chunks = split(text)
        manifest[str(path.relative_to(src))] = len(chunks)
        for i, chunk in enumerate(chunks):
            all_chunks.append(chunk)
            all_ids.append(f"{path.relative_to(src)}#{i}")
            all_meta.append({"source": str(path.relative_to(src)), "chunk": i})

    print(f"文档 {len(docs)} 篇 -> 切片 {len(all_chunks)} 条")
    for name, count in manifest.items():
        print(f"  {name}: {count}")

    if args.dry_run:
        print("\n--dry-run，未写库。首条切片预览：")
        print(all_chunks[0][:300])
        return

    version = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    persist_dir = Path(args.out) / version
    if persist_dir.exists():
        shutil.rmtree(persist_dir)
    persist_dir.mkdir(parents=True)

    import chromadb
    from sentence_transformers import SentenceTransformer

    print(f"\n加载向量模型 {args.model} ...")
    encoder = SentenceTransformer(args.model)
    vectors = encoder.encode(
        all_chunks, normalize_embeddings=True, show_progress_bar=True, batch_size=32
    ).tolist()

    client = chromadb.PersistentClient(path=str(persist_dir))
    collection = client.create_collection(COLLECTION, metadata={"hnsw:space": "cosine"})
    collection.add(
        ids=all_ids, documents=all_chunks, embeddings=vectors, metadatas=all_meta
    )

    meta_path = persist_dir / "manifest.json"
    meta_path.write_text(
        json.dumps(
            {
                "version": version,
                "model": args.model,
                "doc_count": len(docs),
                "chunk_count": len(all_chunks),
                "documents": manifest,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"\n完成: {persist_dir}  （{len(all_chunks)} 条切片）")
    print("切换生效：")
    print(
        f"  curl -XPOST localhost:8000/admin/kb/reload "
        f"-H 'content-type: application/json' -d '{{\"persist_dir\":\"{persist_dir}\"}}'"
    )


if __name__ == "__main__":
    main()
