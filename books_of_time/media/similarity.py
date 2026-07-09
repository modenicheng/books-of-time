from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from books_of_time.coverage import CoverageDraft
from books_of_time.db.models import (
    MediaAsset,
    MediaCluster,
    MediaClusterMember,
    MediaSimilarityEdge,
)
from books_of_time.domain.enums import TaskKind

_MASK_64 = (1 << 64) - 1


@dataclass(frozen=True)
class MediaSimilarityResult:
    edges_created: int
    clusters_created: int


class MediaSimilarityAnalyzer:
    algorithm = "phash_hamming"
    algorithm_version = "v1"

    async def analyze_phash(
        self,
        session: AsyncSession,
        *,
        threshold: int = 5,
    ) -> MediaSimilarityResult:
        assets = list(
            await session.scalars(
                select(MediaAsset)
                .where(MediaAsset.phash.is_not(None))
                .order_by(MediaAsset.id.asc())
            )
        )
        edges: list[tuple[int, int, int]] = []
        edges_created = 0
        for left_index, left in enumerate(assets):
            for right in assets[left_index + 1 :]:
                if left.phash is None or right.phash is None:
                    continue
                distance = hamming_distance(left.phash, right.phash)
                if distance > threshold:
                    continue
                edges.append((left.id, right.id, distance))
                created = await self._insert_edge(
                    session,
                    left_id=left.id,
                    right_id=right.id,
                    distance=distance,
                )
                edges_created += int(created)

        clusters_created = await self._create_clusters(session, assets, edges)
        await session.flush()
        return MediaSimilarityResult(
            edges_created=edges_created,
            clusters_created=clusters_created,
        )

    async def _insert_edge(
        self,
        session: AsyncSession,
        *,
        left_id: int,
        right_id: int,
        distance: int,
    ) -> bool:
        existing = await session.scalar(
            select(MediaSimilarityEdge).where(
                MediaSimilarityEdge.media_asset_id_a == left_id,
                MediaSimilarityEdge.media_asset_id_b == right_id,
                MediaSimilarityEdge.similarity_type == "phash_hamming",
                MediaSimilarityEdge.algorithm == self.algorithm,
                MediaSimilarityEdge.algorithm_version == self.algorithm_version,
            )
        )
        if existing is not None:
            return False

        edge = MediaSimilarityEdge(
            media_asset_id_a=left_id,
            media_asset_id_b=right_id,
            similarity_type="phash_hamming",
            distance=float(distance),
            confidence=max(0.0, 1.0 - (distance / 64.0)),
            algorithm=self.algorithm,
            algorithm_version=self.algorithm_version,
        )
        session.add(edge)
        await session.flush()
        return True

    async def _create_clusters(
        self,
        session: AsyncSession,
        assets: list[MediaAsset],
        edges: list[tuple[int, int, int]],
    ) -> int:
        asset_ids = [asset.id for asset in assets]
        parent = {asset_id: asset_id for asset_id in asset_ids}

        def find(asset_id: int) -> int:
            while parent[asset_id] != asset_id:
                parent[asset_id] = parent[parent[asset_id]]
                asset_id = parent[asset_id]
            return asset_id

        def union(left_id: int, right_id: int) -> None:
            left_root = find(left_id)
            right_root = find(right_id)
            if left_root != right_root:
                parent[right_root] = left_root

        for left_id, right_id, _distance in edges:
            union(left_id, right_id)

        components: dict[int, set[int]] = {}
        for asset_id in asset_ids:
            components.setdefault(find(asset_id), set()).add(asset_id)

        distances = {
            (left_id, right_id): distance for left_id, right_id, distance in edges
        } | {(right_id, left_id): distance for left_id, right_id, distance in edges}

        clusters_created = 0
        for members in components.values():
            if len(members) < 2:
                continue
            representative_id = min(members)
            cluster = MediaCluster(
                cluster_type="phash",
                algorithm=self.algorithm,
                algorithm_version=self.algorithm_version,
                representative_asset_id=representative_id,
            )
            session.add(cluster)
            await session.flush()
            for member_id in sorted(members):
                distance = (
                    0
                    if member_id == representative_id
                    else distances.get((representative_id, member_id))
                )
                session.add(
                    MediaClusterMember(
                        cluster_id=cluster.id,
                        media_asset_id=member_id,
                        distance_to_representative=float(distance)
                        if distance is not None
                        else None,
                        confidence=1.0
                        if distance is None
                        else max(0.0, 1.0 - (distance / 64.0)),
                    )
                )
            clusters_created += 1
        return clusters_created


def hamming_distance(left: int, right: int) -> int:
    return ((left & _MASK_64) ^ (right & _MASK_64)).bit_count()


class MediaSimilarityCollector:
    def __init__(self, analyzer: MediaSimilarityAnalyzer | None = None) -> None:
        self.analyzer = analyzer or MediaSimilarityAnalyzer()

    async def collect(self, task, session: AsyncSession) -> CoverageDraft:
        threshold = int(task.payload.get("threshold", 5))
        result = await self.analyzer.analyze_phash(session, threshold=threshold)
        return CoverageDraft(
            task_kind=TaskKind.ANALYZE_SIMILAR_MEDIA,
            target_type=task.target_type,
            target_id=task.target_id,
            pages_requested=1,
            pages_succeeded=1,
            items_observed=result.edges_created,
            raw_payloads_saved=0,
            truncated=False,
            reason="complete",
            extra={
                "clusters_created": result.clusters_created,
                "threshold": threshold,
            },
        )
