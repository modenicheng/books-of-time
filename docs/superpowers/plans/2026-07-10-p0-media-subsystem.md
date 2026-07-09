# P0 Media Subsystem Implementation Plan

> **Execution mode:** Implement inline in this main session. Avoid opening subagents unless the user explicitly asks for them again.

**Goal:** Add a first-class media subsystem for comment images, starting with reference discovery and download-task enqueue.

**Architecture:** Extend comment parsing with `ParsedCommentMedia`, add media ORM tables and repositories, then call a `MediaService` after comment observations are inserted. Downloading, binary storage, and image metadata are separate follow-up tasks.

**Tech Stack:** Python 3.12, SQLAlchemy async ORM, argparse/worker task queue, pytest-asyncio, Ruff.

## Global Constraints

- Do not store image binaries in PostgreSQL.
- Do not download images during comment parsing.
- Do not introduce external S3/OSS.
- All future media downloads must use unified HTTP/rate-limit infrastructure.
- Preserve unrelated dirty changes in `books_of_time/http/client.py` and `books_of_time/http/rate_limiter.py`.

---

### Task 1: Media Schema And Parser Discovery

- [ ] Add `TaskKind.FETCH_MEDIA_ASSET` and `BilibiliRequestType.MEDIA_IMAGE`.
- [ ] Add `ParsedCommentMedia` and `ParsedComment.media`.
- [ ] Parse common Bilibili comment image fields into ordered media items.
- [ ] Add `media_sources`, `media_assets`, and `comment_observation_media` models.
- [ ] Add repository tests for parser media extraction and schema creation.
- [ ] Verify with `uv run pytest tests/test_comments_parser.py tests/test_comment_repositories.py -v`.
- [ ] Commit as `feat: add media reference schema`.

### Task 2: MediaService Registration

- [ ] Add `books_of_time/media/` package with `normalizer.py`.
- [ ] Implement `MediaService.register_comment_media(...)`.
- [ ] Upsert `media_sources` by `(platform, source_url_hash)`.
- [ ] Insert `comment_observation_media` rows by comment observation and media position.
- [ ] Enqueue `FETCH_MEDIA_ASSET` tasks for pending media sources.
- [ ] Verify a single multi-image comment creates multiple relation rows and one pending task per distinct URL.
- [ ] Commit as `feat: register comment media sources`.

### Task 3: Wire Comment Collection And Docs

- [ ] Call `MediaService` after `CommentRepository.upsert_page()` in hot/latest collectors.
- [ ] Update TODO checkboxes for Media-1 items.
- [ ] Run `uv run pytest`.
- [ ] Run `uv run ruff check .`.
- [ ] Commit as `feat: collect comment media references`.

### Follow-Up Tasks

- Media-2: downloader, local file store, blob hash dedupe.
- Media-3: MIME/dimensions/pixel hash/perceptual hashes.
- Media-4: similarity edges and clusters.
