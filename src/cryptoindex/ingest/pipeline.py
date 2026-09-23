from collections.abc import Mapping

from cryptoindex.core.model import Stage
from cryptoindex.ingest.embedding import embed_stage
from cryptoindex.ingest.parse import parse_stage
from cryptoindex.ingest.segment import segment_stage
from cryptoindex.ingest.stages import StageFn

DEFAULT_STAGES: Mapping[Stage, StageFn] = {
    Stage.PARSE: parse_stage,
    Stage.SEGMENT: segment_stage,
    Stage.EMBED: embed_stage,
}
