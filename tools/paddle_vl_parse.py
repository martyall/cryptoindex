"""Runs inside PaddleOCR's own uv tool environment, not the project's (D7).

Usage: paddle_vl_parse.py PDF OUT_JSON SERVER_URL MODEL

Writes {"pages": [...]} to OUT_JSON: each element is one page's result as
returned by the pipeline's public `json` property (page_index, width, height,
parsing_res_list of labelled blocks with content and bbox). Only JSON is
written: the CLI's save_all also draws visualizations, which downloads a font
from a CDN.
"""

import json
import sys

from paddleocr import PaddleOCRVL


def main() -> None:
    pdf, out_json, server_url, model = sys.argv[1:5]
    pipeline = PaddleOCRVL(
        vl_rec_backend="mlx-vlm-server",
        vl_rec_server_url=server_url,
        vl_rec_api_model_name=model,
    )
    pages = [result.json["res"] for result in pipeline.predict(pdf)]
    with open(out_json, "w") as f:
        json.dump({"pages": pages}, f)


if __name__ == "__main__":
    main()
