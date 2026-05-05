"""create guideline_chunks table for the Retrieval Augmented Generation
(RAG) corpus

Revision ID: 20260504_0003
Revises: 20260504_0002
Create Date: 2026-05-04

The corpus index that backs hybrid retrieval. One row per chunk, with:

- ``text_tsv`` — generated tsvector for sparse (lexical) BM25-style
  retrieval via Postgres full-text search.
- ``embedding`` — pgvector column for dense (semantic) retrieval.
  Dimension 1024 matches the Matryoshka-truncated
  ``text-embedding-3-large`` output.

Indexes:

- ``guideline_tsv_idx`` — Generalized Inverted Index (GIN) on the
  generated tsvector column for fast lexical match.
- ``guideline_trgm_idx`` — Trigram GIN on the raw text for fuzzy
  substring search; used by the query rewriter when the user's term is
  a partial match.
- ``guideline_hnsw_idx`` — Hierarchical Navigable Small World (HNSW)
  index on the embedding for sub-millisecond nearest-neighbor search.
- ``guideline_domain_idx`` — GIN on the ``domain_tags`` array for the
  retriever's domain filter.

The ``embedding_model`` column records the model name used to produce
the embedding, so a re-embedding migration can be audited end-to-end.

Extensions required: ``vector`` (pgvector), ``pg_trgm`` (trigram
operator class). Both are bundled with Postgres 16+ when pgvector is
installed.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "20260504_0003"
down_revision: Union[str, None] = "20260504_0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector;")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm;")
    op.execute(
        """
        CREATE TABLE guideline_chunks (
            chunk_id        TEXT PRIMARY KEY,
            source_id       TEXT NOT NULL,
            section_path    TEXT NOT NULL,
            anchor_url      TEXT NOT NULL,
            domain_tags     TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
            text            TEXT NOT NULL,
            text_tsv        TSVECTOR GENERATED ALWAYS AS (
                                to_tsvector('english', text)
                            ) STORED,
            embedding       VECTOR(1024),
            embedding_model TEXT NOT NULL,
            indexed_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            license_url     TEXT NOT NULL
        );
        """
    )
    op.execute("CREATE INDEX guideline_tsv_idx ON guideline_chunks USING GIN (text_tsv);")
    op.execute(
        "CREATE INDEX guideline_trgm_idx ON guideline_chunks "
        "USING GIN (text gin_trgm_ops);"
    )
    op.execute(
        """
        CREATE INDEX guideline_hnsw_idx
            ON guideline_chunks
            USING hnsw (embedding vector_cosine_ops)
            WITH (m=16, ef_construction=64);
        """
    )
    op.execute(
        "CREATE INDEX guideline_domain_idx "
        "ON guideline_chunks USING GIN (domain_tags);"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS guideline_domain_idx;")
    op.execute("DROP INDEX IF EXISTS guideline_hnsw_idx;")
    op.execute("DROP INDEX IF EXISTS guideline_trgm_idx;")
    op.execute("DROP INDEX IF EXISTS guideline_tsv_idx;")
    op.execute("DROP TABLE IF EXISTS guideline_chunks;")
