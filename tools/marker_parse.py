"""Runs inside Marker's own uv tool environment, not the project's (D7).

Usage: marker_parse.py PDF OUT_JSON

Writes Marker's JSON document (marker.renderers.json.JSONOutput, without
metadata) to OUT_JSON, as `marker_single --output_format json
--disable_image_extraction` renders it. Mirrors marker/scripts/convert_single.py;
the environment variables that script sets before importing are set by the
caller (MarkerParser).
"""

import sys

from marker.config.parser import ConfigParser
from marker.models import create_model_dict


def main() -> None:
    pdf, out_json = sys.argv[1:3]
    config = ConfigParser({"output_format": "json", "disable_image_extraction": True})
    converter = config.get_converter_cls()(
        config=config.generate_config_dict(),
        artifact_dict=create_model_dict(),
        processor_list=config.get_processors(),
        renderer=config.get_renderer(),
        llm_service=config.get_llm_service(),
    )
    rendered = converter(pdf)
    with open(out_json, "w") as f:
        f.write(rendered.model_dump_json(exclude={"metadata"}))


if __name__ == "__main__":
    main()
