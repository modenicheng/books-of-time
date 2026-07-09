# P0 Media Subsystem Design

## Context

Books of Time must preserve image evidence from Bilibili comment sections.
Recent image-heavy comment wars make images part of the historical record, not
an optional attachment. Images should not be embedded inside
`comment_observations`; they need a first-class media subsystem.

## Goal

Add an independent `media` subsystem that can:

1. discover image references while parsing comments;
2. register sources and comment-media relationships without downloading inline;
3. enqueue independent media download tasks;
4. store downloaded image files locally;
5. deduplicate exact binary duplicates by `blob_sha256`;
6. preserve fields needed for later pixel hash and similarity analysis.

## Data Model

Three primary layers:

- `media_sources`: the platform URL/reference observed in a comment payload.
- `media_assets`: the downloaded and deduplicated binary image entity.
- `comment_observation_media`: n-n relation between comment observations and
  media sources/assets, including image position in the comment.

Similarity analysis is separate:

- `media_similarity_edges`
- `media_clusters`
- `media_cluster_members`

These tables are reserved for later offline analysis. The crawler must not run
similarity clustering on the hot collection path.

## Storage

All media files are stored locally under:

```text
data/media/sha256/ab/cd/<blob_sha256>.<ext>
```

No external S3/OSS is introduced for this project phase. `storage_uri` uses a
`file://` URI.

## Task Flow

Comment collection:

1. comment parser extracts `ParsedCommentMedia` entries;
2. `CommentRepository.upsert_page()` creates comment observations;
3. `MediaService.register_comment_media()` upserts media sources, inserts
   `comment_observation_media`, and enqueues `FETCH_MEDIA_ASSET` for pending
   sources.

Media download:

1. worker leases `FETCH_MEDIA_ASSET`;
2. downloader fetches the image through the unified HTTP layer;
3. hasher computes `blob_sha256`;
4. storage writes the local file only if the asset is new;
5. repositories attach `media_source` and backfill comment-media rows.

## Phase Slicing

Media-1:

- parser media extraction;
- `media_sources`;
- `comment_observation_media`;
- `FETCH_MEDIA_ASSET` task enqueue.

Media-2:

- local media storage;
- blob hash dedupe;
- media download collector/worker;
- source and comment-media asset backfill.

Media-3:

- MIME/type/size/dimensions;
- `pixel_sha256`;
- optional perceptual hashes.

Media-4:

- offline similarity edges and clusters.

## Constraints

- Image downloads must use the unified request/rate-limit path.
- Comment parsing only discovers references; it does not download images.
- Exact duplicate enforcement is `media_assets.blob_sha256`.
- `pixel_sha256` is not a uniqueness constraint.
- Source URL normalization can reduce duplicate fetch candidates but is not
  proof of image equality.
- User/public fields remain available for operator verification.

## Acceptance Criteria For Media-1

- A single comment with multiple images creates multiple
  `comment_observation_media` rows with stable positions.
- Repeated references to the same URL reuse one `media_source`.
- Pending media sources enqueue `FETCH_MEDIA_ASSET` tasks.
- Media download is not performed during comment parsing or comment repository
  writes.
- `uv run pytest` and `uv run ruff check .` pass.
